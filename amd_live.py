#!/usr/bin/env python3
"""
amd_live.py — LIVE signal generator for the AMD strategy
(Accumulation → Manipulation → Distribution; NY-PM short, accumulation-gated).

SAFE BY DESIGN — does NOT touch the live A/B agent or any other strategy service:
  * Runs as its OWN Railway service / process, own volume, own buffer.
  * Runs the *validated* research engine unchanged (bundled ./ict + config/amd.yaml),
    so the forward test is a faithful test of the backtested config.
  * Own dedup file (SENT_AMD_FILE), own Telegram webhook (STRAT_AMD_WEBHOOK),
    own TradersPost strategy (EXEC_WEBHOOK_AMD → strategy "STRATEGY_AMD").
  * With STRAT_AMD_ENABLED unset it detects + journals but SENDS NOTHING.

THE STRATEGY (config/amd.yaml, backtest: +0.44R, PF 2.39, t=3.21, ~21 trades/yr,
maxDD −5.2R, 5/5 positive years):
  Accumulation  morning (08:00–12:00 ET) range ≤ 1.2× its own 20-day mean
  Manipulation  PM sweep of a significant-swing / session / prev-day HIGH
                (equal-highs pools excluded)
  Distribution  aligned HTF FVG → 1m IFVG → CISD → SHORT, 2R target, BE@1R

WHAT IT DOES each poll (1-min cron or --loop):
  1. Read this service's own bar buffer (AMD_BUF), build the ict MarketData.
  2. Run the AMD engine to the latest bar; emit a signal only if a FRESH short
     confirmation printed on the last bar AND it passes the session/accumulation gate.
  3. Alert (distinct wording) + optionally stage a TradersPost MARKET bracket.
  4. Journal every alert → resolve win/loss/BE on later bars → Gate-0 counter.

ENV (all optional; nothing is SENT unless STRAT_AMD_ENABLED=1 and a webhook is set):
  STRAT_AMD_ENABLED=1
  AMD_BUF            (def /home/claude/buffer_AMD.csv)   own bar buffer on the volume
  AMD_BUFFER_BARS   (def 45000 ≈ 30 trading days — the accumulation gate needs ~20d lookback)
  STRAT_AMD_WEBHOOK / WEBHOOK_URL     Telegram /webhook for AMD alerts
  EXEC_WEBHOOK_AMD                    TradersPost relay (SEPARATE strategy recommended)
  EXEC_TICKER_AMD (def EXEC_TICKER/CONTRACT/'MNQ1!') · EXEC_MAX_QTY_AMD · PRICE_OFFSET
  SENT_AMD_FILE (def /home/claude/sent_signals_AMD.json) · AMD_TRADES_FILE
  STRAT_AMD_FRESH_MIN (def 10)   only alert a confirmation printed within N min of now
  ACCOUNT (def 100000) · RISK_PCT (def 0.5)   for the Gate-0 $ counter
"""
import os, sys, json, time, csv, datetime as dt
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pandas as pd
import numpy as np

from ict.utils import load_config, hhmm_to_min, atr as _atr
from ict.data import (trading_day_ids, prev_day_levels, resample_htf,
                      session_labels, accumulation_flags, MarketData)
from ict.liquidity import PoolManager, build_levels
from ict.fvg import HTFFVGManager
from ict.strategy import SetupEngine
try:
    import requests
except Exception:
    requests = None
try:
    import live_emit                       # reuse size_for / post_webhook if present in the repo
except Exception:
    live_emit = None

TZ = "America/New_York"
CFG_PATH = os.environ.get("AMD_CONFIG", os.path.join(HERE, "config", "amd.yaml"))
CFG = load_config(CFG_PATH)

AMD_BUF      = os.environ.get("AMD_BUF", "/home/claude/buffer_AMD.csv")
AMD_BUFFER_BARS = int(os.environ.get("AMD_BUFFER_BARS", "45000"))
WEBHOOK_AMD  = os.environ.get("STRAT_AMD_WEBHOOK") or os.environ.get("WEBHOOK_URL", "")
EXEC_AMD     = os.environ.get("EXEC_WEBHOOK_AMD", "")
SENT_AMD     = os.environ.get("SENT_AMD_FILE", "/home/claude/sent_signals_AMD.json")
AMD_TRADES   = os.environ.get("AMD_TRADES_FILE") or os.path.join(os.path.dirname(SENT_AMD) or ".", "amd_trades.json")
FRESH_MIN    = int(os.environ.get("STRAT_AMD_FRESH_MIN", "10"))
OFFSET       = float(os.environ.get("PRICE_OFFSET", "0"))
SLIP         = CFG["costs"]["slippage_ticks_per_side"] * CFG["data"]["tick_size"]
FLATTEN_MIN  = hhmm_to_min(CFG["sessions"]["flatten_at"])   # 16:55 ET — AMD is intraday
TIME_STOP    = CFG["exit"]["time_stop_min"]                 # 240 min
from zoneinfo import ZoneInfo
AMD_COLS     = ["ts_event", "open", "high", "low", "close", "volume"]
_state = {"version": "AMD-v1", "last_alert": None, "alerts": 0, "bars": 0, "last_poll": None}


# ───────────────────────── data → MarketData (no feather cache; live buffer) ─────────────────────────
def _read_buffer(buf=AMD_BUF) -> pd.DataFrame:
    df = pd.read_csv(buf, usecols=AMD_COLS)
    df["ts_event"] = pd.to_datetime(df["ts_event"], utc=True).dt.tz_convert(TZ)
    df = df.set_index("ts_event").sort_index()
    return df[~df.index.duplicated(keep="first")]


def _build_md(df: pd.DataFrame) -> MarketData:
    d = CFG["data"]
    o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
    v = df["volume"].to_numpy(float)
    mod = (df.index.hour * 60 + df.index.minute).to_numpy()
    tday = trading_day_ids(df.index, d["trading_day_roll_hour"])
    pdh, pdl = prev_day_levels(h, l, tday)
    vol_avg = pd.Series(v).rolling(20, min_periods=5).mean().to_numpy()
    htf = {tf: resample_htf(df, tf) for tf in CFG["htf_fvg"]["timeframes"]}
    accum_ok = accumulation_flags(df, tday, CFG)
    return MarketData(df=df, ts=df.index.asi8, o=o, h=h, l=l, c=c, v=v,
                      atr1m=_atr(h, l, c, 20), vol_avg=vol_avg, minute_of_day=mod,
                      tday=tday, session=session_labels(mod, CFG["sessions"]),
                      pdh=pdh, pdl=pdl, htf=htf, accum_ok=accum_ok)


# ───────────────────────── detection (the validated engine, live) ─────────────────────────
def amd_detect(buf=AMD_BUF):
    """Run the AMD engine over the buffer; return the freshest short confirmation(s)
    as order dicts (same fields the journal/alert use). Faithful to config/amd.yaml."""
    df = _read_buffer(buf)
    if len(df) < 1500:
        return []
    md = _build_md(df)
    n = len(md.c)
    pools = PoolManager(md, CFG, build_levels(md, CFG))
    fvgs = HTFFVGManager(md, CFG)
    engine = SetupEngine(md, CFG, fvgs)

    allowed = set(CFG["sessions"]["allowed"])
    dir_filter = CFG["entry"].get("direction_filter", "both")
    accum_on = CFG.get("accumulation", {}).get("enabled", False)
    no_entry_after = hhmm_to_min(CFG["sessions"]["no_new_entries_after"])
    tick = CFG["data"]["tick_size"]; buf_t = CFG["stop"]["buffer_ticks"] * tick
    smin, smax = CFG["stop"]["min_stop_points"], CFG["stop"]["max_stop_points"]
    rr = CFG["exit"]["fixed_r"]
    last_ms = int(md.ts[n - 1] // 1_000_000)
    out = []
    for i in range(n):
        fvgs.step(i)
        sweeps = pools.step(i)
        engine.on_sweeps(i, sweeps)
        sig = engine.step(i)
        if sig is None:
            continue
        sess = md.session[i]; mod = int(md.minute_of_day[i])
        if dir_filter != "both" and sig.direction != dir_filter:
            continue
        if accum_on and not md.accum_ok[i]:
            continue
        if sess not in allowed or (mod >= no_entry_after and mod < 1080):
            continue
        # only the freshest confirmations matter for a live alert
        bar_ms = int(md.ts[i] // 1_000_000)
        if (last_ms - bar_ms) > FRESH_MIN * 60_000:
            continue
        anchor = sig.sweep.extreme
        stop = anchor + buf_t if sig.direction == "short" else anchor - buf_t
        entry = float(md.c[i])                       # market at next open ≈ current price
        risk = (stop - entry) if sig.direction == "short" else (entry - stop)
        if not (smin <= risk <= smax):
            continue
        tp = entry - rr * risk if sig.direction == "short" else entry + rr * risk
        out.append(dict(
            date=str(md.df.index[i].date()),
            dir=("SHORT" if sig.direction == "short" else "LONG"),
            entry=round(entry, 2), SL=round(stop, 2), TP=round(tp, 2), risk=round(risk, 2),
            sweep_kind=sig.sweep.level.kind, sweep_level=round(float(sig.sweep.level.price), 2),
            sess=str(sess), signal_ts=str(md.df.index[i]), bar_ms=bar_ms,
            fvg_tf=getattr(sig.htf_fvg, "tf", "—"),
            status="live", strategy="AMD"))
    # de-dupe to one per (date,dir)
    seen = {}
    for x in out:
        seen[(x["date"], x["dir"])] = x
    return list(seen.values())


def _hhmm_et(ms):
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000, ZoneInfo(TZ)).strftime("%H:%M")
    except Exception:
        return ""


def amd_candidates(buf=AMD_BUF):
    """Live PRE-TRADE funnel for the current trading day, so the setup is visible
    before it fires: accumulation-day flag → manipulation (PM sweep) → awaiting
    IFVG → awaiting CISD. Mirrors the multi-stage candidate views of the other
    strategies. Returns newest-first rows: {stage, dir, note, time}."""
    df = _read_buffer(buf)
    if len(df) < 1500:
        return []
    md = _build_md(df)
    n = len(md.c)
    pools = PoolManager(md, CFG, build_levels(md, CFG))
    fvgs = HTFFVGManager(md, CFG)
    engine = SetupEngine(md, CFG, fvgs)
    dir_filter = CFG["entry"].get("direction_filter", "both")
    accum_on = CFG.get("accumulation", {}).get("enabled", False)
    cur_day = int(md.tday[n - 1])
    sweeps_today = []
    for i in range(n):
        fvgs.step(i)
        sw = pools.step(i)
        engine.on_sweeps(i, sw)
        engine.step(i)
        if int(md.tday[i]) == cur_day and md.session[i] == "ny_pm" and (not accum_on or md.accum_ok[i]):
            for e in sw:
                if dir_filter == "both" or e.direction == dir_filter:
                    sweeps_today.append((int(md.ts[i] // 1_000_000), e.level.kind,
                                         round(float(e.level.price), 2),
                                         "SHORT" if e.direction == "short" else "LONG"))
    day = str(md.df.index[n - 1].date())
    accum = (not accum_on) or bool(md.accum_ok[n - 1])
    in_pm = md.session[n - 1] == "ny_pm"
    out = [dict(strat="AMD", day=day, time=_hhmm_et(int(md.ts[n - 1] // 1_000_000)), dir="",
                stage="1·accumulation", note=("✓ compressed morning" if accum else "✗ morning not compressed — no trades today")
                + " · NY-PM " + ("open" if in_pm else "closed"))]
    st = engine.setup
    if st is not None:                                   # a setup is actively working right now
        stage = "4·awaiting CISD" if st.ifvg_confirmed_idx is not None else "3·awaiting IFVG"
        note = ("IFVG inverted — waiting for the short confirmation"
                if st.ifvg_confirmed_idx is not None
                else "sweep + aligned HTF FVG — waiting for 1m IFVG")
        out.append(dict(strat="AMD", day=day, time=_hhmm_et(int(md.ts[st.started_idx] // 1_000_000)),
                        dir=("SHORT" if st.direction == "short" else "LONG"), stage=stage,
                        note="%s · sweep %s @ %.2f" % (note, st.sweep.level.kind, st.sweep.level.price)))
    seen = set()
    for ms, kind, price, d in reversed(sweeps_today[-8:]):
        if (kind, price) in seen:
            continue
        seen.add((kind, price))
        out.append(dict(strat="AMD", day=day, time=_hhmm_et(ms), dir=d, stage="2·manipulation",
                        note="swept %s @ %.2f — awaiting distribution" % (kind, price)))
    return out


def key_amd(x):
    return f"AMD|{x['date']}|{x['dir']}|{x['signal_ts']}"   # own namespace — never merges with A/B


# ───────────────────────── alert + execution (own namespace) ─────────────────────────
def to_alert_amd(x):
    rp = round(x["risk"], 1)
    return (f"🅰🅼🅳 STRATEGY AMD · Accumulation→Manipulation→Distribution · 🔴 {x['dir']} · NY-PM"
            f"\n📋 SELL MARKET ~{round(x['entry']+OFFSET,1)} — wejście TERAZ (potwierdzony CISD short)"
            f"\n🛑 SL {round(x['SL']+OFFSET,1)} · ryzyko {rp} pkt ({rp*4:.0f} ticks) · BE po +{rp} (1R)"
            f"\n🎯 TP {round(x['TP']+OFFSET,1)} · +{round(2*x['risk'],1)} pkt (2R)"
            f"\n🧩 manipulacja: sweep {x['sweep_kind']} @ {x['sweep_level']} · dystrybucja w NY-PM"
            f"\n🌅 dzień akumulacyjny (poranek skompresowany) · HTF FVG {x['fvg_tf']}"
            f"\n⚠ Strategy AMD — OSOBNY strumień, NIE myl z A/B / C / F / ORB. Tylko short w NY-PM.")


def _size(entry, sl):
    if live_emit and hasattr(live_emit, "size_for"):
        try:
            return live_emit.size_for(entry, sl)
        except Exception:
            return None
    return None


def _td_payload(x, action="enter"):
    e = float(x["entry"]); sl = float(x["SL"]); R = abs(e - sl); tp = e - 2 * R   # short
    _sf = _size(e, sl); qty = int(_sf[0]) if _sf else 1
    cap = os.environ.get("EXEC_MAX_QTY_AMD", "").strip()
    if cap.isdigit() and int(cap) > 0:
        qty = min(qty, int(cap))
    qty = max(1, qty)
    return {"ticker": os.environ.get("EXEC_TICKER_AMD", os.environ.get("EXEC_TICKER", os.environ.get("CONTRACT", "MNQ1!"))),
            "action": "sell" if action == "enter" else "exit",
            "orderType": "market", "quantity": qty,
            "takeProfit": {"limitPrice": round(tp + OFFSET, 2)},
            "stopLoss": {"type": "stop", "stopPrice": round(sl + OFFSET, 2)},
            "timeInForce": "gtc", "strategy": "STRATEGY_AMD"}


def exec_amd(x, text=None, action="enter"):
    if not EXEC_AMD or requests is None:
        return "no-exec"
    p = _td_payload(x, action)
    if text:
        p["text"] = text
    try:
        r = requests.post(EXEC_AMD, json=p, timeout=10)
        print("EXEC_AMD", getattr(r, "status_code", None), flush=True)
        return "exec"
    except Exception as ex:
        print("EXEC_AMD err", ex, flush=True)
        return f"ERR {ex}"


def _post_webhook(text):
    if live_emit and hasattr(live_emit, "post_webhook") and WEBHOOK_AMD:
        try:
            return live_emit.post_webhook(text, WEBHOOK_AMD)
        except Exception:
            pass
    if WEBHOOK_AMD and requests is not None:
        try:
            requests.post(WEBHOOK_AMD, json={"text": text}, timeout=10); return "sent"
        except Exception as ex:
            return f"ERR {ex}"
    return "no-url"


# ───────────────────────── Gate-0 journal (own file on the volume) ─────────────────────────
def _ensure_dir(p):
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)

def _load(p):
    try:
        return json.load(open(p))
    except Exception:
        return {}

def _save(p, d):
    _ensure_dir(p); json.dump(d, open(p, "w"))


def _journal_add(x):
    j = _load(AMD_TRADES); k = key_amd(x)
    if k not in j:
        j[k] = dict(date=x["date"], dir=x["dir"], entry=x["entry"], SL=x["SL"], TP=x["TP"],
                    risk=x["risk"], sweep_kind=x["sweep_kind"], sweep_level=x["sweep_level"],
                    be=False, status="alerted", bar_ms=x["bar_ms"],
                    alert_ts=dt.datetime.utcnow().isoformat(timespec="seconds"))
        _save(AMD_TRADES, j)


def _journal_update(b):
    """Per-bar resolver mirroring the backtest EXACTLY: market fill on the first
    bar after the signal, then SL-first / TP=2R / time-stop(240m) / flatten@16:55
    ET / BE@1R. Short-only, INTRADAY — never held overnight."""
    try:
        hi = float(b["high"]); lo = float(b["low"]); op = float(b["open"]); cl = float(b["close"])
    except Exception:
        return
    ts = str(b.get("ts_event", ""))
    try:
        _t = ts if ("+" in ts or "Z" in ts) else ts + "+00:00"
        bdt = dt.datetime.fromisoformat(_t.replace("Z", "+00:00"))
        bar_ms = int(bdt.timestamp() * 1000)
        et = bdt.astimezone(ZoneInfo(TZ)); mod = et.hour * 60 + et.minute
    except Exception:
        bar_ms = 0; mod = -1
    j = _load(AMD_TRADES); changed = False
    for k, t in j.items():
        if t["status"] in ("win", "loss", "be", "flatten", "time"):
            continue
        if t["status"] == "alerted":
            if bar_ms and bar_ms > t.get("bar_ms", 0):        # first bar AFTER the signal → market fill
                e = op + SLIP if t["dir"] == "LONG" else op - SLIP   # slipped against you
                t["entry"] = round(e, 2)
                t["risk"] = round(abs(t["SL"] - e), 2)
                t["TP"] = round(e - 2 * t["risk"] if t["dir"] == "SHORT" else e + 2 * t["risk"], 2)
                t["status"] = "filled"; t["fill_ms"] = bar_ms; t["fill_ts"] = ts; changed = True
        if t["status"] == "filled":
            short = t["dir"] == "SHORT"; e = t["entry"]; sl = t["SL"]; tp = t["TP"]; risk = t["risk"]
            be = t.get("be", False); cur = e if be else sl
            oneR = e - risk if short else e + risk
            hit_sl = (hi >= cur) if short else (lo <= cur)
            hit_tp = (lo <= tp) if short else (hi >= tp)
            if hit_sl:
                t["status"] = "be" if be else "loss"; t["R"] = 0.0 if be else -1.0
                t["close_ts"] = ts; changed = True; continue
            if hit_tp:
                t["status"] = "win"; t["R"] = 2.0; t["close_ts"] = ts; changed = True; continue
            mins = (bar_ms - t.get("fill_ms", bar_ms)) / 60000.0
            if mins >= TIME_STOP or (FLATTEN_MIN <= mod < 1080):   # time-stop or EOD flatten
                pts = (e - cl) if short else (cl - e)
                t["status"] = "time" if mins >= TIME_STOP else "flatten"
                t["R"] = round(pts / risk - 0.02, 2); t["close_ts"] = ts; changed = True; continue
            if (not be) and ((lo <= oneR) if short else (hi >= oneR)):
                t["be"] = True; changed = True
    if changed:
        _save(AMD_TRADES, j)


def _journal_stats():
    from collections import Counter
    vals = list(_load(AMD_TRADES).values())
    _CLOSED = ("win", "loss", "be", "flatten", "time")
    closed = [t for t in vals if t["status"] in _CLOSED]
    n = len(closed); wins = sum(1 for t in closed if t.get("R", 0.0) > 0.05)
    totR = round(sum(t.get("R", 0.0) for t in closed), 2)
    exp = round(totR / n, 3) if n else 0.0
    riskusd = float(os.environ.get("ACCOUNT", "100000")) * float(os.environ.get("RISK_PCT", "0.5")) / 100.0
    gate = exp >= 0.15 and n >= 20
    return dict(alerts=len(vals),
                filled=sum(1 for t in vals if t["status"] in ("filled",) + _CLOSED),
                closed=n, wins=wins, winpct=round(100 * wins / n, 1) if n else 0.0,
                totR=totR, exp=exp, dollars=round(totR * riskusd),
                gate0=gate, gate0_target=0.15,
                by_status=dict(Counter(t["status"] for t in vals)),
                trades=sorted(vals, key=lambda z: z.get("alert_ts", ""), reverse=True)[:60])


# ───────────────────────── poll ─────────────────────────
def process_amd(buf=AMD_BUF, now_ms=None):
    now_ms = now_ms or int(time.time() * 1000)
    _state["last_poll"] = dt.datetime.utcnow().isoformat(timespec="seconds")
    try:
        sigs = amd_detect(buf)
    except Exception as ex:
        print("AMD detect err", ex, flush=True); return {"error": str(ex)}
    state = _load(SENT_AMD); fired = 0
    fresh_ms = FRESH_MIN * 60 * 1000
    enabled = os.environ.get("STRAT_AMD_ENABLED") == "1"
    for x in sigs:
        k = key_amd(x)
        if k in state:
            continue
        _journal_add(x)                                # always journal (Gate-0 tracks even in silent mode)
        if (now_ms - x["bar_ms"]) <= fresh_ms:
            txt = to_alert_amd(x)
            if enabled:
                code = exec_amd(x, txt, "enter") if EXEC_AMD else "no-exec"
                _post_webhook(txt)
                print("AMD-ALERT", code, k, flush=True)
            else:
                print("AMD-ALERT [SILENT]", k, flush=True)
            _state["alerts"] += 1; _state["last_alert"] = k; fired += 1
        state[k] = "alerted"
    _save(SENT_AMD, state)
    return {"amd_alerts": fired, "amd_signals": len(sigs), "enabled": enabled}


# ───────────────────────── bar intake (own buffer on the volume) ─────────────────────────
def _append_bar_amd(b):
    _ensure_dir(AMD_BUF)
    ts = str(b["ts_event"]).strip()
    if "+" not in ts and "Z" not in ts:
        ts = ts + "+00:00"
    row = [ts, b["open"], b["high"], b["low"], b["close"], b.get("volume", 0)]
    new = not os.path.exists(AMD_BUF)
    with open(AMD_BUF, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(AMD_COLS)
        w.writerow(row)
    with open(AMD_BUF) as f:
        rows = f.readlines()
    if len(rows) > AMD_BUFFER_BARS + 1:
        with open(AMD_BUF, "w") as f:
            f.write(rows[0] + "".join(rows[-AMD_BUFFER_BARS:]))
    _state["bars"] += 1
    _journal_update(b)                                 # resolve open journal trades on every closed bar


# ───────────────────────── Flask service (deploy: gunicorn amd_live:app) ─────────────────────────
def register_routes(app, prefix="/amd"):
    from flask import request, jsonify

    @app.route(prefix or "/", methods=["GET"])
    def _page():
        st = _journal_stats()
        gate = "✅ ≥ +0.15R (Gate 0)" if st["gate0"] else f"⏳ {st['exp']:+.3f}R / {st['closed']} closed (need ≥+0.15R over ≥20)"
        rows = "".join(
            f"<tr><td>{t.get('alert_ts','')[:16]}</td><td>{t['dir']}</td><td>{t.get('entry')}</td>"
            f"<td>{t.get('SL')}</td><td>{t.get('TP')}</td><td>{t['status']}</td>"
            f"<td>{t.get('R','')}</td></tr>" for t in st["trades"])
        try:
            cands = amd_candidates()
        except Exception:
            cands = []
        crows = "".join(
            f"<tr><td>{c['time']}</td><td>{c['stage']}</td><td>{c['dir']}</td><td>{c['note']}</td></tr>"
            for c in cands) or "<tr><td colspan=4 style='color:#888'>no live funnel (outside NY-PM or non-accumulation day)</td></tr>"
        html = f"""<!doctype html><meta charset=utf-8><title>Strategy AMD</title>
<style>body{{font:14px system-ui;margin:24px;max-width:900px}}h1{{font-size:20px}}h2{{font-size:15px;margin-top:22px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{border-bottom:1px solid #eee;padding:4px 8px;text-align:left}}
.g{{padding:8px 12px;border-radius:8px;background:#f4f6f8;display:inline-block;margin:4px 0}}</style>
<h1>🅰🅼🅳 Strategy AMD — NY-PM short (accumulation-gated)</h1>
<div class=g><b>Gate 0:</b> {gate}</div>
<div class=g>alerts {st['alerts']} · filled {st['filled']} · closed {st['closed']} · win {st['winpct']}% · totR {st['totR']} · ${st['dollars']}</div>
<div class=g>enabled: {os.environ.get('STRAT_AMD_ENABLED')=='1'} · bars {_state['bars']} · last poll {_state['last_poll']}</div>
<p style="color:#666">Backtest: +0.44R, PF 2.39, t=3.21, ~21 trades/yr, 5/5 positive years. Live = the real test.</p>
<h2>Live funnel — Accumulation → Manipulation → Distribution</h2>
<table><tr><th>time</th><th>stage</th><th>dir</th><th>note</th></tr>{crows}</table>
<h2>Journal (Gate-0)</h2>
<table><tr><th>alert</th><th>dir</th><th>entry</th><th>SL</th><th>TP</th><th>status</th><th>R</th></tr>{rows}</table>"""
        return html

    @app.route("/bars", methods=["POST"])
    def _bars():
        b = request.get_json(force=True, silent=True) or {}
        bars = b.get("bars") or [b]
        for one in bars:
            if "ts_event" in one and "close" in one:
                _append_bar_amd(one)
        r = process_amd()
        return jsonify({"ok": True, **r, "bars_total": _state["bars"]})

    @app.route("/stats", methods=["GET"])
    def _stats():
        return jsonify(_journal_stats())

    @app.route("/candidates", methods=["GET"])
    def _cands():
        try:
            return jsonify(candidates=amd_candidates())
        except Exception as ex:
            return jsonify(candidates=[], error=str(ex))

    @app.route("/poll", methods=["GET", "POST"])
    def _poll():
        return jsonify(process_amd())

    @app.route("/health", methods=["GET"])
    def _health():
        return jsonify({"ok": True, **_state})


def _make_app():
    from flask import Flask
    a = Flask(__name__)
    register_routes(a, "")     # serve the page at root for its own service
    return a


app = None
def _ensure_app():
    global app
    if app is None:
        app = _make_app()
    return app


# gunicorn entrypoint: `gunicorn amd_live:app`
app = _make_app()


if __name__ == "__main__":
    if "--loop" in sys.argv:
        while True:
            print(process_amd(), flush=True)
            time.sleep(60)
    elif "--serve" in sys.argv:
        _make_app().run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
    else:
        print(json.dumps(process_amd(), indent=2))
