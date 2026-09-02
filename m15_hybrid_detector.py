#!/usr/bin/env python3
"""Isolated M15 trigger / M5 BOS detector used by the shadow-only strategy.

The production A/B detector remains untouched.  This module deliberately runs in a
separate process so replacing detcore's final ``emit`` step cannot leak into the M1
strategy.  All setup geometry is formed from closed M15 bars; confirmation is a close
through structure on a closed M5 bar.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

import detcore.emit as emit_mod
from detcore.config import Config
from detcore.data import load
from detcore.entries import get_entry_v10
from detcore.exits import exceeds_risk_cap
from detcore.confirmation import bias_for
from detcore.pipeline import detect


@dataclass(frozen=True)
class Settings:
    """Frozen v1 research settings selected in the M15 -> M5 walk-forward study."""

    timeframe_setup: str = "15m"
    timeframe_bos: str = "5m"
    dispwin: int = 4                 # four M15 bars = about one hour
    atrmult: float = 0.75
    minimp: int = 3
    maximp: int = 3
    lookback: int = 6
    fvg_tolerance_points: float = 1.0
    rejection_hold_fraction: float = 0.90
    retest_window_m15: int = 20
    bos_window_m5: int = 60          # five hours
    max_stop_points: float = 40.0
    stop_cap_points: float = 30.0
    shallow_fraction: float = 0.25
    min_leg_stop_points: float = 5.0
    fill_window_m1_bars: int = 20
    setup_group_risk_usd: float = 500.0
    version: str = "m15-m5-ab-shallow-v1"


SETTINGS = Settings()

DETECTOR_ENV = {
    # The historical M15 prototype accidentally allowed a handful of +ORPH records to
    # confirm on M15.  v1 disables that path so the contract "every BOS is M5" is true.
    "ORPHAN_FVG": "0",
    "SWING_TP": "1",
    "SWING_TP_K": "5",
    "SWING_TP_MIN_R": "1",
    "SWING_TP_MAX_R": "3",
    "SL_STRUCT_MAX_R": "30",
    "SL_ANCHOR_BUF": "0.25",
}


def _column_map(frame: pd.DataFrame) -> Dict[str, str]:
    return {str(c).strip().lower().lstrip("\ufeff"): c for c in frame.columns}


def load_one_minute(path: str, tail_rows: int = 0, source_tz: str = "UTC") -> pd.DataFrame:
    """Load either the live ``ts_event`` schema or the downloaded Date/Time schema."""
    frame = pd.read_csv(path)
    cols = _column_map(frame)
    if "ts_event" in cols:
        stamp = pd.to_datetime(frame[cols["ts_event"]], utc=True, errors="coerce", format="ISO8601")
    elif "date" in cols and "time" in cols:
        naive = pd.to_datetime(
            frame[cols["date"]].astype(str) + " " + frame[cols["time"]].astype(str),
            errors="coerce",
        )
        stamp = pd.DatetimeIndex(naive).tz_localize(
            source_tz, ambiguous="NaT", nonexistent="NaT"
        ).tz_convert("UTC")
    else:
        raise ValueError("CSV needs ts_event or Date + Time columns")

    required = {}
    for name in ("open", "high", "low", "close"):
        if name not in cols:
            raise ValueError("CSV is missing %s" % name)
        required[name] = pd.to_numeric(frame[cols[name]], errors="coerce")
    volume = pd.to_numeric(frame[cols["volume"]], errors="coerce") if "volume" in cols else 0.0
    out = pd.DataFrame({"ts_event": stamp, **required, "volume": volume}).dropna(
        subset=["ts_event", "open", "high", "low", "close"]
    )
    out = out.sort_values("ts_event").drop_duplicates("ts_event", keep="last")
    if tail_rows and len(out) > tail_rows:
        out = out.tail(int(tail_rows))
    return out.reset_index(drop=True)


def resample_closed(frame: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate only buckets known to be closed by the final one-minute bar."""
    if frame.empty:
        return pd.DataFrame(columns=["ts_event", "open", "high", "low", "close", "volume"])
    indexed = frame.set_index("ts_event")
    bars = indexed.resample(
        "%dmin" % minutes, label="left", closed="left", origin="epoch"
    ).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"), source_minutes=("close", "count"),
    ).dropna(subset=["open", "high", "low", "close"])
    known_through = frame["ts_event"].iloc[-1] + pd.Timedelta(minutes=1)
    bars = bars[(bars.index + pd.Timedelta(minutes=minutes)) <= known_through]
    return bars.reset_index()


def prepare_timeframes(input_csv: str, output_dir: str, tail_rows: int = 0) -> Tuple[str, str, Dict[str, Any]]:
    one = load_one_minute(input_csv, tail_rows=tail_rows)
    m5 = resample_closed(one, 5)
    m15 = resample_closed(one, 15)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    m5_path, m15_path = out / "m15_shadow_5m.csv", out / "m15_shadow_15m.csv"
    m5.drop(columns=["source_minutes"], errors="ignore").to_csv(m5_path, index=False)
    m15.drop(columns=["source_minutes"], errors="ignore").to_csv(m15_path, index=False)
    status = {
        "input_rows": int(len(one)), "m5_rows": int(len(m5)), "m15_rows": int(len(m15)),
        "first_utc": one["ts_event"].iloc[0].isoformat() if len(one) else None,
        "last_utc": one["ts_event"].iloc[-1].isoformat() if len(one) else None,
    }
    return str(m5_path), str(m15_path), status


def detector_config(m15_path: str, settings: Settings = SETTINGS) -> Config:
    return Config(
        atrmult=settings.atrmult, dispwin=settings.dispwin, minimp=settings.minimp,
        lookback=settings.lookback, tol=settings.fvg_tolerance_points,
        retwin=settings.retest_window_m15, boswin=30,
        rej_frac=settings.rejection_hold_fraction,
        mode="confirm", cap_days=10, eod_intraday=False,
        disp_mode="chain", maximp=settings.maximp, maxext=40,
        max_stop_r=settings.max_stop_points, stop_cap=settings.stop_cap_points,
        stop_cap_trigger=0.0, entry_primary="fibo", causal=True, cutoff="",
        debug_trace=True, data_csv=m15_path, out_pkl="/tmp/m15_shadow_unused.pkl",
    )


def _candidate(ctx: Any, trigger: int, model: str, name: str, direction: str,
               stage: str, disp: Dict[str, Any], details: Optional[Dict[str, Any]] = None) -> None:
    bar = ctx.df.dt.iloc[int(trigger)]
    row = {
        "cat": name, "model": model, "dir": direction,
        "trig": bar.strftime("%Y-%m-%d %H:%M"),
        "trig_ms": int(bar.timestamp() * 1000), "stage": stage,
        "setup_timeframe": "M15", "bos_timeframe": "M5",
        "disp_start": ctx.df.dt.iloc[int(disp["s"])].strftime("%Y-%m-%d %H:%M"),
        "disp_end": ctx.df.dt.iloc[int(disp["u"])].strftime("%Y-%m-%d %H:%M"),
        "disp_bars": int(disp["u"]) - int(disp["s"]) + 1,
        "fvg": [round(float(disp["fvg"][0]), 2), round(float(disp["fvg"][1]), 2)],
    }
    if details:
        row.update(details)
    ctx._TRC.append(row)


def _m15_rejection(ctx: Any, disp: Dict[str, Any], direction: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    bull = direction == "LONG"
    fl, fh = map(float, disp["fvg"])
    fb = int(disp["fvg_bar"])
    threshold = round(
        fh - ctx.cfg.rej_frac * (fh - fl) if bull else fl + ctx.cfg.rej_frac * (fh - fl), 2
    )
    origin, origin_bar, tests = None, None, []
    end = min(fb + 1 + int(ctx.cfg.retwin), ctx.n)
    for j in range(fb + 1, end):
        broken = ctx.cl[j] < threshold if bull else ctx.cl[j] > threshold
        if broken:
            return None, {"stage": "M15_INVALIDATED", "reason": "body_through_90pct_hold",
                          "invalidated_ms": int(ctx.T[j]) * 1000, "hold_level": threshold}
        wick = ctx.lo[j] <= fh if bull else ctx.hi[j] >= fl
        body_holds = ctx.cl[j] >= threshold if bull else ctx.cl[j] <= threshold
        if wick and body_holds:
            extreme = float(ctx.lo[j] if bull else ctx.hi[j])
            tests.append(j)
            if origin is None or (extreme < origin if bull else extreme > origin):
                origin, origin_bar = extreme, j
    if origin is None:
        still_open = end >= ctx.n
        return None, {
            "stage": "WAIT_M15_REJECTION" if still_open else "M15_REJECTION_EXPIRED",
            "reason": "waiting_for_fvg_retest" if still_open else "no_valid_retest_in_window",
            "hold_level": threshold,
        }
    return {
        "origin": round(float(origin), 2), "origin_bar": int(origin_bar),
        "tests": tests, "ce": round((fl + fh) / 2.0, 2), "hold_level": threshold,
    }, {"stage": "M15_REJECTION_OK", "hold_level": threshold}


def hybrid_setup(ctx15: Any, ctx5: Any, disp: Dict[str, Any], direction: str,
                 m5_bos_window: int) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    rejection, diag = _m15_rejection(ctx15, disp, direction)
    if rejection is None:
        return None, diag

    bull = direction == "LONG"
    origin_bar = int(rejection["origin_bar"])
    start_sec = int(ctx15.T[origin_bar]) + 15 * 60
    q0 = int(np.searchsorted(ctx5.T, start_sec, side="left"))
    q1 = min(q0 + int(m5_bos_window), ctx5.n)
    s15 = int(disp["s"])
    level = float(max(ctx15.hi[s15:origin_bar])) if bull else float(min(ctx15.lo[s15:origin_bar]))
    checked15 = origin_bar
    threshold = float(rejection["hold_level"])

    for q in range(q0, q1):
        decision_sec = int(ctx5.T[q]) + 5 * 60
        last_closed15 = int(np.searchsorted(ctx15.T, decision_sec - 15 * 60, side="right") - 1)
        last_closed15 = min(last_closed15, ctx15.n - 1)
        if last_closed15 > checked15:
            closes = ctx15.cl[checked15 + 1:last_closed15 + 1]
            broken = bool(np.any(closes < threshold)) if bull else bool(np.any(closes > threshold))
            if broken:
                return None, {"stage": "M15_INVALIDATED", "reason": "body_through_90pct_hold",
                              "hold_level": threshold}
            checked15 = last_closed15

        broke = ctx5.cl[q] > level if bull else ctx5.cl[q] < level
        if broke:
            end = float(max(ctx5.hi[q0:q + 1])) if bull else float(min(ctx5.lo[q0:q + 1]))
            return {
                "dr": direction, "origin": rejection["origin"], "origin_bar": origin_bar,
                "end": round(end, 2), "bos_bar": max(origin_bar, last_closed15),
                "ce": rejection["ce"], "fvg": disp["fvg"], "fvg_bar": disp["fvg_bar"],
                "s": s15, "u": disp["u"], "bos_m5_bar": int(q),
                "bos_ms_override": int(decision_sec * 1000),
                "bos_close_override": float(ctx5.cl[q]), "structure_level": round(level, 2),
            }, {"stage": "M5_BOS_OK"}
        level = max(level, float(ctx5.hi[q])) if bull else min(level, float(ctx5.lo[q]))

    waiting = q1 >= ctx5.n and (q1 - q0) < int(m5_bos_window)
    expires = (int(ctx5.T[q0]) + int(m5_bos_window) * 5 * 60) * 1000 if q0 < ctx5.n else start_sec * 1000
    return None, {
        "stage": "WAIT_M5_BOS" if waiting else "M5_BOS_EXPIRED",
        "reason": "waiting_for_close_through_structure" if waiting else "no_bos_in_60_m5_bars",
        "structure_level": round(level, 2), "rejection_ms": start_sec * 1000,
        "expires_ms": int(expires), "hold_level": threshold,
    }


def install_hybrid_emitter(ctx5: Any, settings: Settings = SETTINGS) -> None:
    """Replace only the child process' final confirmation step."""
    def emit(ctx: Any, trigger: int, model: str, name: str, direction: str,
             disp: Dict[str, Any], conf: Any = None) -> None:
        setup, diag = hybrid_setup(ctx, ctx5, disp, direction, settings.bos_window_m5)
        if setup is None:
            _candidate(ctx, trigger, model, name, direction, diag["stage"], disp, diag)
            return
        entry = get_entry_v10(ctx, setup)
        if entry is None:
            _candidate(ctx, trigger, model, name, direction, "ENTRY_REJECTED", disp,
                       {"reason": "no_causal_entry"})
            return
        if exceeds_risk_cap(ctx, entry.get("risk_ce", entry["risk"])):
            _candidate(ctx, trigger, model, name, direction, "ENTRY_REJECTED", disp,
                       {"reason": "ce_risk_above_cap", "risk": float(entry["risk"])})
            return

        bos15 = int(setup["bos_bar"])
        bos_ms = int(setup["bos_ms_override"])
        bos_utc = pd.Timestamp(bos_ms, unit="ms", tz="UTC")
        bos_et_fixed = bos_utc.tz_convert("Etc/GMT+4")
        bias, _ = bias_for(ctx, bos15)
        align = "Y" if bias.replace("?", "") == direction else (
            "?" if "?" in bias or bias == "niejasny" else "N"
        )
        record = {
            "brk": ctx.cur_break, "date": str(bos_et_fixed.date()), "model": model,
            "cat": name, "dir": direction, "cls": "B" if "+DIB" in name else "A",
            "entry": float(entry["entry"]), "SL": float(entry["sl"]), "TP": float(entry["tp"]),
            "risk": float(entry["risk"]), "kind": entry["kind"], "bias": bias,
            "bias_align": align, "bos": bos_et_fixed.strftime("%H:%M"),
            "s": int(disp["s"]), "u": int(disp["u"]),
            "fvg_lo": round(float(disp["fvg"][0]), 2),
            "fvg_hi": round(float(disp["fvg"][1]), 2),
            "fvg_bar": int(disp["fvg_bar"]), "origin_bar": int(setup["origin_bar"]),
            "bos_bar": bos15, "bos_m5_bar": int(setup["bos_m5_bar"]),
            "ce": round(float(setup["ce"]), 2), "sl_src": entry.get("sl_src"),
            "sl_ce": entry.get("sl_ce"), "sl_struct": entry.get("sl_struct"),
            "sl_fvg_edge": entry.get("sl_fvg_edge"), "tp_src": entry.get("tp_src"),
            "tp_level": entry.get("tp_level"), "signal_close": float(setup["bos_close_override"]),
            "entry_ms": bos_ms, "bos_ms": bos_ms,
            "bos_iso": bos_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "setup_timeframe": "M15", "bos_timeframe": "M5",
            "strategy_version": settings.version, "emit_bar": bos15,
        }
        ctx.out.append(record)
        _candidate(ctx, trigger, model, name, direction, "POTWIERDZONY", disp, {
            "date": record["date"], "bos": record["bos"], "bos_ms": bos_ms,
            "entry": record["entry"], "SL": record["SL"], "TP": record["TP"],
            "risk": record["risk"], "sl_src": record.get("sl_src"),
            "tp_src": record.get("tp_src"), "signal_close": record["signal_close"],
            "structure_level": setup["structure_level"],
        })

    emit_mod.emit = emit


def canonical_signals(signals: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    grouped: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
    for signal in sorted(signals, key=lambda x: (int(x["bos_ms"]), str(x.get("cat", "")))):
        key = (int(signal["bos_ms"]), signal["dir"], round(float(signal["entry"]), 2),
               round(float(signal["SL"]), 2))
        grouped.setdefault(key, []).append(signal)
    out = []
    for rows in grouped.values():
        representative = sorted(
            rows, key=lambda x: (x.get("cls") == "A", x.get("bias_align") == "Y"), reverse=True
        )[0].copy()
        cats = []
        for row in rows:
            if row.get("cat") not in cats:
                cats.append(row.get("cat"))
        representative["cats"] = cats
        representative["cat"] = " + ".join(filter(None, cats))
        out.append(representative)
    out.sort(key=lambda x: int(x["bos_ms"]))
    return out, max(0, len(list(signals)) - len(out)) if isinstance(signals, list) else 0


def run_detection(m5_path: str, m15_path: str, settings: Settings = SETTINGS) -> Dict[str, Any]:
    ctx5 = load(Config(data_csv=m5_path, cutoff="", causal=True))
    install_hybrid_emitter(ctx5, settings)
    old_env = {key: os.environ.get(key) for key in DETECTOR_ENV}
    os.environ.update(DETECTOR_ENV)
    try:
        signals, _, ctx15 = detect(detector_config(m15_path, settings))
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    canonical, duplicates = canonical_signals(signals)
    return {
        "signals": canonical, "trace": ctx15._TRC,
        "detector_signals": len(signals), "canonical_signals": len(canonical),
        "duplicates_removed": duplicates, "settings": asdict(settings),
    }


def _write_json(path: str, value: Any) -> None:
    Path(path).write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--signals-out", required=True)
    parser.add_argument("--trace-out", required=True)
    parser.add_argument("--status-out", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--tail-rows", type=int, default=28000)
    args = parser.parse_args()
    m5_path, m15_path, status = prepare_timeframes(args.input, args.work_dir, args.tail_rows)
    result = run_detection(m5_path, m15_path)
    status.update({key: result[key] for key in ("detector_signals", "canonical_signals", "duplicates_removed")})
    status["settings"] = result["settings"]
    _write_json(args.signals_out, result["signals"])
    _write_json(args.trace_out, result["trace"])
    _write_json(args.status_out, status)


if __name__ == "__main__":
    main()
