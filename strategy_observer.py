"""Passive strategy recorder. No network client, order sender, or strategy changes.

Hooks copy observations to a bounded queue. A separate writer persists a hash chain.
Receipt by an execution relay is explicitly NOT acknowledgement/fill by a broker.
"""
from __future__ import annotations

import atexit
import csv
import datetime as dt
import functools
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
import uuid

VERSION = "observer-1.0"
STREAMS = ["A/B", "A/B-shallow", "M15 A/B", "M15 A/B-shallow"]
_recorder = None
_PRIVATE = re.compile(r"secret|password|token|authorization|api.?key|webhook|cookie", re.I)
_URL = re.compile(r"https?://[^\s\"'<>]+")
CONFIG_KEYS = """BUFFER_BARS M15_SHADOW_ENABLED M15_SHADOW_TAIL DET_FILE ENTRY_OFFSET_PTS
PRICE_OFFSET EXEC_TICK EXEC_TICKER CONTRACT EXEC_MAX_QTY EXEC_QTY EXEC_TIF EXEC_MODE
AUTO_SUBMIT SETUP_GROUP_RISK_USD SETUP_GROUP_RT_COST_USD AB_SHALLOW_ENABLED
AB_SHALLOW_FRACTION AB_SHALLOW_RR AB_SHALLOW_MIN_SL_PTS AB_SHALLOW_MAX_SL_PTS
ACCOUNT POINT_VALUE RISK_PCT FILL_WIN_MIN EXEC_CANCEL_AFTER_SEC EXEC_REJECT_AFTER_SEC
PARTIAL_AT_1R PARTIAL_ACCT_PCT MONDAY_MODE SKIP_SESSIONS SESSION_SIZE_MULT
MAX_TRADES_DAY DAY_LOSS_N DAY_LOSS_USD DAY_TARGET_USD LOSS_STREAK_N RAMP_TRADES
NEWS_STRICT NO_TRADE_SUPPRESS MONITOR_BIAS_GATE MONITOR_BIAS_MIN_CONF
REGIME_GATE REGIME_SIZE_GATE REGIME_SKIP_CHOP GAP_REPRIME_MIN FRESH_MIN MAX_RETEST
ORPHAN_FVG ORPHAN_LIFE ORPHAN_MAX_BARS DISPWIN ATRMULT LOOKBACK TOL REJ_FRAC
RETWIN BOSWIN SL_MAX_PTS DD_FLOOR DD_PROJECTED_RISK DD_PROXIMITY_MODE""".split()


def now_ms():
    return time.time_ns() // 1_000_000


def clean(value, depth=0):
    if depth > 20:
        return "[depth limit]"
    if isinstance(value, dict):
        return {str(k): ("[redacted]" if _PRIVATE.search(str(k)) or str(k) in
                        ("_alert_txt", "text", "resp", "raw", "body") else clean(v, depth + 1))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v, depth + 1) for v in value]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        return clean(value.item(), depth + 1)
    return _URL.sub("[url redacted]", str(value))[:4000]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def signal_id(signal):
    fields = [signal.get(k) for k in ("date", "model", "dir", "bos_ms", "entry", "SL")]
    return "obs_" + hashlib.sha256(canonical(clean(fields)).encode()).hexdigest()[:24]


def timestamp_ms(value):
    if isinstance(value, (float, int)):
        return int(value)
    stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return int(stamp.timestamp() * 1000)


class Recorder:
    def __init__(self, root, source, agent_version, capacity=4096):
        self.root = Path(root) / "observer"
        self.root.mkdir(parents=True, exist_ok=True)
        self.spool = self.root / "spool"
        self.spool.mkdir(exist_ok=True)
        self.path = self.root / "events.sqlite3"
        self.source = Path(source)
        self.agent_version = agent_version
        self.session = uuid.uuid4().hex
        self.q = queue.Queue(maxsize=capacity)
        self.dropped = 0
        self.errors = 0
        self.last_error = None
        self.last_write_ms = None
        self.stopping = threading.Event()
        self.thread = None
        self.artifacts = {}
        self.artifact_hashes = {}
        with self.connect() as con:
            con.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL, session TEXT NOT NULL,
                    observed_ms INTEGER NOT NULL, persisted_ms INTEGER NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL,
                    prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS workers (
                    session TEXT PRIMARY KEY, updated_ms INTEGER, dropped INTEGER, errors INTEGER);
            """)
            con.execute("INSERT OR IGNORE INTO meta VALUES ('store_id', ?)", (uuid.uuid4().hex,))
            self.store_id = con.execute("SELECT value FROM meta WHERE key='store_id'").fetchone()[0]

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True, name="strategy-observer")
        self.thread.start()

    def submit(self, kind, payload, snapshot=None):
        link = None
        try:
            packet = dict(event_id=uuid.uuid4().hex, session=self.session,
                          observed_ms=now_ms(), kind=str(kind), payload=clean(payload))
            if snapshot:
                try:
                    link = self.spool / (uuid.uuid4().hex + ".snapshot")
                    # buffer/trace writers replace files atomically; retain the old inode.
                    os.link(snapshot, link)
                except Exception as exc:
                    packet["payload"]["snapshot_error"] = type(exc).__name__
                    self.errors += 1
                    link = None
            self.q.put_nowait((packet, link))
            return packet["event_id"]
        except Exception as exc:
            self.dropped += 1
            self.last_error = type(exc).__name__
            if link:
                link.unlink(missing_ok=True)
            return None

    def _persist(self, packet):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            previous = con.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            previous = previous[0] if previous else "0" * 64
            persisted = now_ms()
            body = canonical(packet["payload"])
            signed = {**packet, "payload": body, "persisted_ms": persisted, "prev_hash": previous}
            digest = hashlib.sha256(canonical(signed).encode()).hexdigest()
            con.execute("INSERT INTO events(event_id,session,observed_ms,persisted_ms,kind,payload,prev_hash,hash) "
                        "VALUES(?,?,?,?,?,?,?,?)", (packet["event_id"], packet["session"],
                        packet["observed_ms"], persisted, packet["kind"], body, previous, digest))
            self._health(con)
            self.last_write_ms = persisted

    def _health(self, con):
        con.execute("INSERT OR REPLACE INTO workers VALUES(?,?,?,?)",
                    (self.session, now_ms(), self.dropped, self.errors))

    def _enrich(self, packet, snapshot):
        p = packet["payload"]
        if snapshot and packet["kind"] in ("history_snapshot", "ab_opportunity"):
            data = snapshot.read_bytes()
            rows = list(csv.DictReader(data.decode("utf-8-sig").splitlines()))
            p["history"] = {"rows": len(rows), "first": rows[0].get("ts_event") if rows else None,
                            "last": rows[-1].get("ts_event") if rows else None,
                            "sha256": hashlib.sha256(data).hexdigest()}
            if packet["kind"] == "ab_opportunity":
                signal = p["signal"]
                # Select by UTC timestamp, never by an index into a rolling file.
                wanted = timestamp_ms(signal.get("bos_ms") if signal.get("bos_ms") is not None else signal.get("bos_iso"))
                if signal.get("bos_ms") is not None and signal.get("bos_iso"):
                    p["bos_iso_disagrees_with_epoch"] = timestamp_ms(signal["bos_iso"]) != wanted
                p["bos_utc_ms"] = wanted
                matches = [r for r in rows if timestamp_ms(r["ts_event"]) == wanted]
                if len(matches) == 1:
                    p["signal_close"] = float(matches[0]["close"])
                else:
                    p["signal_close_error"] = "BOS timestamp missing or duplicated"
                # Preserve actual candles around the anchors for graphical reconstruction.
                indices = [int(signal[k]) for k in ("s", "u", "origin_bar", "fvg_bar", "bos_bar")
                           if signal.get(k) is not None]
                lo = max(0, min(indices, default=max(0, len(rows) - 180)) - 5)
                hi = min(len(rows), max(indices, default=len(rows) - 1) + 6)
                p["anchor_index_rows"] = [{"index": i, **rows[i]} for i in range(lo, hi)]
                p["anchor_indices_require_detector_buffer_parity"] = True
                p["recent_candles"] = rows[-180:]
        elif snapshot and packet["kind"] in ("candidate_trace", "m15_state_snapshot", "artifact_state"):
            rows = json.loads(snapshot.read_text())
            # Complete snapshot of this detector run, stored only when its hash changes.
            p["rows"] = rows
        if packet["kind"] == "boot":
            paths = list(self.source.glob("*.py")) + list((self.source / "detcore").glob("*.py"))
            p["source_hashes"] = {str(f.relative_to(self.source)): hashlib.sha256(f.read_bytes()).hexdigest()
                                   for f in paths if f.is_file()}
        return packet

    def _loop(self):
        trace_hashes = {}
        trace_rows = {}
        last_artifacts = 0
        while not self.stopping.is_set() or not self.q.empty():
            if not self.stopping.is_set() and time.monotonic() - last_artifacts >= 60:
                last_artifacts = time.monotonic()
                for name, path in list(self.artifacts.items()):
                    try:
                        raw = Path(path).read_bytes()
                        digest = hashlib.sha256(raw).hexdigest()
                        if self.artifact_hashes.get(name) == digest:
                            continue
                        packet = dict(event_id=uuid.uuid4().hex, session=self.session,
                                      observed_ms=now_ms(), kind="artifact_state",
                                      payload={"name": name, "rows": clean(json.loads(raw)),
                                               "sha256": digest, "source_mtime_ns": Path(path).stat().st_mtime_ns,
                                               "capture": "periodic_state_not_decision_time"})
                        self._persist(packet)
                        self.artifact_hashes[name] = digest
                    except FileNotFoundError:
                        pass
                    except Exception as exc:
                        self.errors += 1
                        self.last_error = type(exc).__name__
            try:
                packet, snapshot = self.q.get(timeout=1)
            except queue.Empty:
                try:
                    with self.connect() as con:
                        self._health(con)
                except Exception:
                    self.errors += 1
                continue
            try:
                if snapshot and packet["kind"] == "candidate_trace":
                    digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
                    key = packet["payload"].get("stream")
                    if trace_hashes.get(key) == digest:
                        continue
                    packet["payload"]["snapshot_sha256"] = digest
                enriched = self._enrich(packet, snapshot)
                if packet["kind"] == "candidate_trace" and snapshot:
                    key = packet["payload"].get("stream")
                    rows = enriched["payload"].get("rows", [])
                    previous = trace_rows.get(key, set())
                    indexed = {hashlib.sha256(canonical(clean(r)).encode()).hexdigest(): clean(r) for r in rows}
                    enriched["payload"].update(rows=[v for k, v in indexed.items() if k not in previous],
                                               snapshot_row_count=len(rows), capture="changed_trace_rows",
                                               initial_snapshot=key not in trace_rows,
                                               removed_row_hashes=sorted(previous - set(indexed)))
                self._persist(enriched)
                if packet["kind"] == "candidate_trace" and snapshot:
                    trace_hashes[key] = digest
                    trace_rows[key] = set(indexed)
            except Exception as exc:
                self.errors += 1
                self.last_error = type(exc).__name__
                try:
                    self._persist(dict(event_id=uuid.uuid4().hex, session=self.session,
                                       observed_ms=now_ms(), kind="recorder_error",
                                       payload={"failed_kind": packet["kind"], "error_type": type(exc).__name__}))
                except Exception:
                    pass
            finally:
                if snapshot:
                    snapshot.unlink(missing_ok=True)
                self.q.task_done()

    def status(self):
        with self.connect() as con:
            count, latest = con.execute("SELECT COUNT(*), MAX(observed_ms) FROM events").fetchone()
            dropped, errors = con.execute("SELECT COALESCE(SUM(dropped),0),COALESCE(SUM(errors),0) FROM workers").fetchone()
        return dict(version=VERSION, store_id=self.store_id, enabled=True, streams=STREAMS,
                    events=count, last_event_ms=latest, queue_depth=self.q.qsize(),
                    dropped_events=max(dropped, self.dropped), recorder_errors=max(errors, self.errors),
                    writer_alive=bool(self.thread and self.thread.is_alive()),
                    broker_fill_connection="not_connected", execution_capable=False)

    def stop(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=3)


def event(kind, payload, snapshot=None):
    # Instrumentation must never propagate into a strategy decision or order path.
    try:
        if _recorder:
            return _recorder.submit(kind, payload, snapshot)
    except Exception:
        pass
    return None


def exec_start(signal, payload, leg):
    try:
        return event("relay_request", {"signal_id": signal_id(signal), "stream": signal.get("_strat", "A/B"),
                                      "group_id": signal.get("_setup_group_id"), "leg": leg,
                                      "signal": signal, "request": payload, "broker_fill": False})
    except Exception:
        return None


def exec_end(request_id, status, response=None, error=None):
    try:
        data = response.json() if response is not None else {}
        receipt = {k: data.get(k) for k in ("id", "logId", "success", "messageCode") if k in data}
    except Exception:
        receipt = {}
    event("relay_response", {"request_event_id": request_id, "http_status": status,
                             "receipt": receipt, "error_type": type(error).__name__ if error else None,
                             "broker_fill": False})


def opportunity(signal, context, buffer_path):
    try:
        event("ab_opportunity", {"signal_id": signal_id(signal), "stream": "A/B",
                                 "signal": signal, "context": context,
                                 "classification": "before_live_account_gates"}, snapshot=buffer_path)
    except Exception:
        pass


def detector_event(kind, detector, buffer_path, returncode=None):
    try:
        event(kind, {"detector": detector, "buffer_stat": Path(buffer_path).stat().st_mtime_ns,
                     "returncode": returncode})
    except Exception:
        pass


def _wrap(module, name, kind):
    original = getattr(module, name, None)
    if original is None or getattr(original, "_observer_wrapped", False):
        return
    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        if _recorder is None:
            return original(*args, **kwargs)
        # Copy before the function can mutate the signal/state; no raw exception text.
        try:
            before = (dict(signal_count=len(args[0]), asof_ms=args[3]) if name == "process_scan_result"
                      and len(args) >= 4 else clean({"args": args, "kwargs": kwargs}))
        except Exception:
            before = {"capture_error": True}
        started = now_ms()
        try:
            result = original(*args, **kwargs)
        except Exception as exc:
            event(kind, {"function": name, "started_ms": started, "input": before,
                         "error_type": type(exc).__name__})
            raise
        event(kind, {"function": name, "started_ms": started, "input": before,
                     "result": result, "completed_ms": now_ms(),
                     "after_args": args if name == "_finish_leg" else None})
        if name == "process_scan_result":
            try:
                event("candidate_trace", {"stream": "M15 A/B"}, snapshot=module.TRACE_PATH)
            except Exception:
                pass
        return result
    wrapped._observer_wrapped = True
    setattr(module, name, wrapped)


def install(app, namespace):
    """Called after ordinary route registration, before the server starts accepting bars."""
    global _recorder
    from flask import jsonify, request
    enabled = os.environ.get("OBSERVER_ENABLED", "0") == "1"
    token = os.environ.get("OBSERVER_READ_TOKEN", "")
    installation_error = None
    if enabled:
        try:
            if len(token) < 32:
                raise ValueError("OBSERVER_READ_TOKEN must contain at least 32 characters")
            _recorder = Recorder(namespace["DATA_DIR"], namespace["HERE"], namespace["VERSION"])
            _recorder.artifacts = {"guard_log": namespace["guardrails"].GLOG,
                                   "guard_state": namespace["guardrails"].GSTATE,
                                   "ab_model_log": namespace["shadow"].LOG,
                                   "m15_state": namespace["m15_shadow_strategy"].GROUPS_PATH,
                                   "m15_status": namespace["m15_shadow_strategy"].STATUS_PATH}
            _recorder.start()
            atexit.register(_recorder.stop)
            # These wrappers preserve every return value and exception from the original functions.
            for name in ("guard_ok", "manual_ok", "note", "begin_sibling_batch", "finish_sibling_batch",
                         "rollback_sibling_batch", "flatten_cancel_only"):
                _wrap(namespace["guardrails"], name, "guard_event")
            m15 = namespace["m15_shadow_strategy"]
            _wrap(m15, "process_scan_result", "m15_scan")
            _wrap(m15, "build_group", "m15_group")
            _wrap(m15, "_finish_leg", "m15_leg_finish")
            # Persist the state after each advancing bar, including pending->filled transitions.
            original = m15.advance_bar
            @functools.wraps(original)
            def advance(bar):
                result = original(bar)
                if result:
                    event("m15_state_snapshot", {"bar": bar, "advanced": result}, snapshot=m15.GROUPS_PATH)
                return result
            m15.advance_bar = advance
            event("boot", {"observer_version": VERSION, "agent_version": namespace["VERSION"],
                           "explicit_environment": {k: os.environ[k] for k in CONFIG_KEYS if k in os.environ},
                           "ab_allocation": namespace["ab_shallow"].allocation(),
                           "buffer_bars": namespace["BUFFER_BARS"], "m15_enabled": m15.ENABLED,
                           "m15_group_risk_usd": m15.GROUP_RISK_USD, "m15_version": m15.VERSION,
                           "ab_clock": str(namespace["NY"]), "m15_clock": str(m15.NY),
                           "persistent_directory": str(namespace["DATA_DIR"]),
                           "unprocessed_snapshot_files_on_start": len(list(_recorder.spool.glob('*.snapshot'))),
                           "broker_fill_connection": "not_connected"})
            event("history_snapshot", {"stream": "A/B", "classification": "startup_history_not_forward"},
                  snapshot=namespace["BUF"])
        except Exception as exc:
            installation_error = type(exc).__name__ + ": " + str(exc)
            print("[observer] disabled:", installation_error, flush=True)
            if _recorder:
                _recorder.stop()
            _recorder = None

    def authorized():
        supplied = request.headers.get("Authorization", "")
        return len(token) >= 32 and hmac.compare_digest(supplied, "Bearer " + token)

    def health():
        return jsonify(version=VERSION, enabled=bool(_recorder), execution_capable=False,
                       error=installation_error, streams=STREAMS)

    def status():
        if not authorized():
            return jsonify(error="unauthorized"), 401
        if not _recorder:
            return jsonify(enabled=False, error=installation_error), 503
        return jsonify(_recorder.status())

    def events():
        if not authorized():
            return jsonify(error="unauthorized"), 401
        if not _recorder:
            return jsonify(enabled=False), 503
        try:
            after = max(0, int(request.args.get("after", "0")))
            limit = max(1, min(200, int(request.args.get("limit", "100"))))
        except ValueError:
            return jsonify(error="invalid cursor"), 400
        with _recorder.connect() as con:
            con.row_factory = sqlite3.Row
            rows = [dict(r) for r in con.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?",
                                               (after, limit))]
        response = jsonify(store_id=_recorder.store_id, events=rows,
                           next_cursor=rows[-1]["seq"] if rows else after)
        response.headers["Cache-Control"] = "no-store"
        return response

    app.add_url_rule("/observer/health", "observer_health", health, methods=["GET"])
    app.add_url_rule("/observer/status", "observer_status", status, methods=["GET"])
    app.add_url_rule("/observer/events", "observer_events", events, methods=["GET"])
