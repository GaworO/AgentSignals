"""TradingView 1s OHLCV, delivered once per completed minute. Storage only.

Never forwards seconds to the M1 detector, interprets broker fills, or sends
orders. Missing seconds stay missing. Uses only stdlib plus existing Flask.
"""
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import hmac
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import threading

SCHEMA = "tv_1s_batch_v1"
FIELDS = ("open", "high", "low", "close", "volume")
MAX_BYTES = 32768
_BATCH_CALLBACK = None
_STORE_LOCK = threading.RLock()


def now_ms():
    return int(time.time() * 1000)


def db_path():
    return Path(os.environ.get("TV_1S_DB") or
                str(Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent)) / "tv_1s.sqlite3"))


def expected_symbol():
    return os.environ.get("TV_1S_SYMBOL", "").strip()


@contextmanager
def connect():
    # Serialize local initialization and short DB transactions. Concurrent first
    # requests otherwise race on PRAGMA journal_mode before the tables exist.
    with _STORE_LOCK:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(path, timeout=.75)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA busy_timeout=750")
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
              CREATE TABLE IF NOT EXISTS seconds(
                symbol TEXT,ts_ms INTEGER,open REAL,high REAL,low REAL,close REAL,volume REAL,
                received_ms INTEGER NOT NULL,PRIMARY KEY(symbol,ts_ms));
              CREATE TABLE IF NOT EXISTS minutes(
                symbol TEXT,ts_ms INTEGER,minute_json TEXT NOT NULL,
                first_received_ms INTEGER NOT NULL,last_received_ms INTEGER NOT NULL,
                observed_seconds INTEGER NOT NULL,quality TEXT NOT NULL,
                PRIMARY KEY(symbol,ts_ms));
              CREATE TABLE IF NOT EXISTS counters(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS bridge_status(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()


def _stamp(v, alignment):
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0 or v % alignment:
        raise ValueError("timestamp must be aligned integer UTC milliseconds")
    return v


def _bar(raw, alignment):
    if not isinstance(raw, dict):
        raise ValueError("bar must be an object")
    stamp = _stamp(raw.get("ts_ms"), alignment)
    values = []
    for k in FIELDS:
        v = raw.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError("finite numeric OHLCV required")
        values.append(float(v))
    o, h, lo, cl, vol = values
    if not 0 < lo <= min(o, cl) <= max(o, cl) <= h or vol < 0:
        raise ValueError("invalid OHLCV geometry")
    return dict(ts_ms=stamp, **dict(zip(FIELDS, values)))


def _quality(rows, minute):
    if not rows:
        return "EMPTY"
    if len(rows) != 60:
        return "INCOMPLETE"
    actual = (rows[0]["open"], max(r["high"] for r in rows),
              min(r["low"] for r in rows), rows[-1]["close"],
              math.fsum(r["volume"] for r in rows))
    return "COMPLETE_MATCH" if all(
        math.isclose(v, minute[k], rel_tol=0, abs_tol=1e-6 if k == "volume" else 1e-8)
        for k, v in zip(FIELDS, actual)) else "M1_MISMATCH"


def ingest(body, current=None):
    current = now_ms() if current is None else int(current)
    if not isinstance(body, dict) or body.get("schema") != SCHEMA or body.get("tf") != "1S":
        raise ValueError("invalid schema or timeframe")
    symbol = expected_symbol()
    if not symbol or body.get("symbol") != symbol:
        raise ValueError("symbol mismatch")
    if body.get("source") != "TradingView":
        raise ValueError("source mismatch")
    minute = _bar(body.get("minute"), 60000)
    start = minute["ts_ms"]
    age = current - start - 60000
    max_age = int(os.environ.get("TV_1S_MAX_DELAY_SEC", "180")) * 1000
    if max_age < 0 or age < 0 or age > max_age:
        raise ValueError("unclosed or late minute")
    raw = body.get("bars")
    if not isinstance(raw, list) or len(raw) > 60:
        raise ValueError("expected 0..60 observed seconds")
    parsed = [_bar(b, 1000) for b in raw]
    stamps = [b["ts_ms"] for b in parsed]
    if stamps != sorted(set(stamps)):
        raise ValueError("seconds must be unique and sorted")
    if any(not start <= at < start + 60000 for at in stamps):
        raise ValueError("second outside declared minute")
    encoded = json.dumps(minute, sort_keys=True, separators=(",", ":"), allow_nan=False)
    added = 0
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        old = c.execute("SELECT minute_json FROM minutes WHERE symbol=? AND ts_ms=?", (symbol, start)).fetchone()
        if old and old[0] != encoded:
            raise ValueError("conflicting immutable M1 reference")
        for b in parsed:
            old_bar = c.execute("SELECT * FROM seconds WHERE symbol=? AND ts_ms=?", (symbol, b["ts_ms"])).fetchone()
            if old_bar and any(old_bar[k] != b[k] for k in FIELDS):
                raise ValueError("conflicting immutable second")
            if not old_bar:
                c.execute("INSERT INTO seconds VALUES(?,?,?,?,?,?,?,?)",
                          (symbol, b["ts_ms"], *(b[k] for k in FIELDS), current))
                added += 1
        rows = c.execute("SELECT * FROM seconds WHERE symbol=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms",
                         (symbol, start, start + 60000)).fetchall()
        quality = _quality(rows, minute)
        c.execute("""INSERT INTO minutes VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(symbol,ts_ms) DO UPDATE SET last_received_ms=excluded.last_received_ms,
            observed_seconds=excluded.observed_seconds,quality=excluded.quality""",
                  (symbol, start, encoded, current, current, len(rows), quality))
        c.execute("""INSERT INTO counters VALUES('seconds',?)
            ON CONFLICT(key) DO UPDATE SET value=value+excluded.value""", (added,))
    return dict(ok=True, accepted=added, duplicates=len(parsed)-added,
                observed_seconds=len(rows), missing_seconds=60-len(rows), quality=quality,
                minute_start_ms=start, manager_connected=False)


def status(current=None):
    current = now_ms() if current is None else int(current)
    symbol = expected_symbol()
    blockers = []
    if os.environ.get("TV_1S_ENABLED", "0") != "1": blockers.append("feed_disabled")
    if not os.environ.get("TV_1S_TOKEN"): blockers.append("token_missing")
    if not symbol: blockers.append("symbol_missing")
    with connect() as c:
        counter = c.execute("SELECT value FROM counters WHERE key='seconds'").fetchone()
        recent = [dict(r) for r in c.execute(
            """SELECT ts_ms,observed_seconds,quality,first_received_ms,last_received_ms FROM minutes
            WHERE symbol=? ORDER BY ts_ms DESC LIMIT 30""", (symbol,))]
    latest = recent[0] if recent else None
    age = None if latest is None else (current-latest["ts_ms"]-60000)/1000
    if not latest: blockers.append("no_batches_received")
    elif age > 90: blockers.append("batch_stale")
    elif latest["quality"] != "COMPLETE_MATCH": blockers.append(latest["quality"])
    with connect() as c:
        bridge = c.execute("SELECT value FROM bridge_status WHERE key='last_result'").fetchone()
    return dict(source="TradingView", tf="1S", delivery_mode="MINUTE_BATCH",
                symbol=symbol or None, total_stored_seconds=counter[0] if counter else 0,
                latest_minute_age_seconds=age, recent_minutes=recent, blockers=blockers,
                state="MISSING" if not latest else "STALE" if age > 90 else latest["quality"],
                manager_connected=False, broker_feedback_connected=False,
                v3_bridge=json.loads(bridge[0]) if bridge else None,
                continuous_symbol=bool(symbol and "!" in symbol),
                note="Storage only. 1s resolution; minute-batch delivery, not second-by-second LIVE.")


def export_csv(body):
    start = _stamp(body.get("start_ms"), 1000)
    end = _stamp(body.get("end_ms"), 1000)
    if not 0 < end-start <= 24*60*60*1000:
        raise ValueError("export range must be <= 24 hours")
    out = io.StringIO(newline="")
    w = csv.writer(out)
    w.writerow(("ts_event", *FIELDS, "symbol", "source", "received_utc"))
    with connect() as c:
        for r in c.execute("SELECT * FROM seconds WHERE symbol=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms",
                           (expected_symbol(), start, end)):
            utc = lambda ms: datetime.fromtimestamp(ms/1000, timezone.utc).isoformat()
            w.writerow((utc(r["ts_ms"]), *(r[k] for k in FIELDS), r["symbol"], "TradingView", utc(r["received_ms"])))
    return out.getvalue()


PAGE = r'''<!doctype html><html lang="pl"><meta charset="utf-8"><title>TradingView 1s feed</title>
<style>body{background:#101722;color:#e6edf3;font:15px system-ui;margin:30px;max-width:1050px}h1{font-size:24px}pre{white-space:pre-wrap}table{border-collapse:collapse;width:100%}td,th{padding:9px;border-bottom:1px solid #344155;text-align:left}.mut{color:#aab8cc}</style>
<h1>TradingView 1s — paczki co minutę</h1><p class="mut">Tylko zbieranie danych. Nie otwiera ani nie zamyka pozycji. Brakujące sekundy nie są uzupełniane.</p>
<pre id="summary">Ładowanie…</pre><table><thead><tr><th>Minuta UTC</th><th>Sekundy / 60</th><th>Jakość</th><th>Opóźnienie pierwszego odbioru</th></tr></thead><tbody id="rows"></tbody></table>
<script>const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){try{const r=await fetch('/feed/1s/data',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);const s=await r.json();
document.getElementById('summary').textContent='Symbol: '+(s.symbol||'nie ustawiono')+'\nStan: '+s.state+'\nZapisanych świec: '+s.total_stored_seconds+'\nBlokady: '+s.blockers.join(', ')+'\nManager: NIE PODŁĄCZONY'+(s.continuous_symbol?'\nUWAGA: symbol ciągły — nie jest tożsamością kontraktu brokera':'');
document.getElementById('rows').innerHTML=s.recent_minutes.map(x=>'<tr><td>'+esc(new Date(x.ts_ms).toISOString())+'</td><td>'+x.observed_seconds+'</td><td>'+esc(x.quality)+'</td><td>'+((x.first_received_ms-x.ts_ms-60000)/1000).toFixed(2)+' s</td></tr>').join('');}
catch(e){document.getElementById('summary').textContent='Błąd odczytu — status nieaktualny: '+e.message;}}
load();setInterval(load,15000);</script></html>'''


def register(app, on_batch=None):
    from flask import Response, jsonify, request
    global _BATCH_CALLBACK
    _BATCH_CALLBACK = on_batch

    def checked_body():
        if os.environ.get("TV_1S_ENABLED", "0") != "1":
            return None, (jsonify(error="feed_disabled"), 503)
        wanted = os.environ.get("TV_1S_TOKEN", "")
        if not wanted or not expected_symbol():
            return None, (jsonify(error="feed_not_configured"), 503)
        if request.content_length is None or request.content_length > MAX_BYTES:
            return None, (jsonify(error="payload_size_invalid"), 413)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return None, (jsonify(error="JSON_object_required"), 400)
        supplied = request.headers.get("X-TV-1S-Token", "") or body.get("feed_token", "")
        if not isinstance(supplied, str) or not hmac.compare_digest(wanted.encode(), supplied.encode()):
            return None, (jsonify(error="unauthorized"), 401)
        return body, None

    def intake():
        body, error = checked_body()
        if error is not None: return error
        try:
            result = ingest(body)
            if _BATCH_CALLBACK:
                try: bridge = _BATCH_CALLBACK(body)
                except Exception as exc: bridge = dict(v3_data_bridge='ERROR',error_type=type(exc).__name__)
                with connect() as c:
                    c.execute("INSERT OR REPLACE INTO bridge_status VALUES('last_result',?)",(json.dumps(bridge),))
                result.update(bridge)
            return jsonify(result)
        except (ValueError, TypeError, KeyError, OverflowError):
            return jsonify(error="invalid_or_conflicting_batch"), 400
        except sqlite3.OperationalError:
            return jsonify(error="storage_unavailable"), 503

    def data():
        try: return jsonify(status())
        except (sqlite3.Error, OSError): return jsonify(error="storage_unavailable"), 503

    def export():
        body, error = checked_body()
        if error is not None: return error
        try:
            return Response(export_csv(body), mimetype="text/csv",
                headers={"Content-Disposition": 'attachment; filename="tv_1s.csv"'})
        except (ValueError, TypeError, KeyError, OverflowError):
            return jsonify(error="invalid_export_range"), 400
        except sqlite3.OperationalError:
            return jsonify(error="storage_unavailable"), 503

    app.add_url_rule("/bars/1s", "tv_1s_intake", intake, methods=["POST"])
    app.add_url_rule("/feed/1s", "tv_1s_page", lambda: Response(PAGE, mimetype="text/html"))
    app.add_url_rule("/feed/1s/data", "tv_1s_data", data)
    app.add_url_rule("/feed/1s/export", "tv_1s_export", export, methods=["POST"])
