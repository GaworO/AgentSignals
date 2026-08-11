"""Pure validation and idempotency rules for the live 1-minute bar intake."""
from __future__ import annotations

import datetime as dt
import hmac
import math
from typing import Any, Mapping


def token_ok(expected: str, headers: Mapping[str, Any], query_token: str = "") -> bool:
    if not expected:
        return False
    supplied = str(headers.get("X-Bars-Token") or "")
    auth = str(headers.get("Authorization") or "")
    if not supplied and auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    if not supplied:
        supplied = str(query_token or "")
    return bool(supplied) and hmac.compare_digest(str(expected), supplied)


def normalize_bar(raw: Mapping[str, Any], require_minute: bool = True) -> tuple[dict[str, Any], int]:
    required = ("ts_event", "open", "high", "low", "close")
    if any(raw.get(k) in (None, "") for k in required):
        raise ValueError("ts_event and complete OHLC are required")
    stamp = str(raw["ts_event"]).strip().replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(stamp)
    if parsed.tzinfo is None:
        # Backward compatibility with the original TradingView feed, which
        # formats time_close in UTC but omits the trailing ``Z``/offset.
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    parsed = parsed.astimezone(dt.timezone.utc)
    if require_minute and (parsed.second or parsed.microsecond):
        raise ValueError("ts_event must be aligned to a closed 1-minute bar")
    out = {"ts_event": parsed.isoformat()}
    for key in ("open", "high", "low", "close", "volume"):
        value = 0.0 if key == "volume" and raw.get(key) in (None, "") else float(raw[key])
        if not math.isfinite(value):
            raise ValueError("non-finite " + key)
        out[key] = value
    if out["high"] < out["low"]:
        raise ValueError("high below low")
    if not (out["low"] <= out["open"] <= out["high"] and out["low"] <= out["close"] <= out["high"]):
        raise ValueError("OHLC geometry invalid")
    if out["volume"] < 0:
        raise ValueError("negative volume")
    return out, int(parsed.timestamp() * 1000)


def same_bar(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    try:
        if str(a.get("ts_event")).replace("Z", "+00:00") != str(b.get("ts_event")).replace("Z", "+00:00"):
            return False
        return all(abs(float(a.get(k)) - float(b.get(k))) < 1e-9
                   for k in ("open", "high", "low", "close", "volume"))
    except Exception:
        return False


def sequence_decision(last: Mapping[str, Any] | None, current: Mapping[str, Any]) -> str:
    """Return new | duplicate | conflict | out_of_order."""
    if not last:
        return "new"
    last_ts = str(last.get("ts_event") or "").replace("Z", "+00:00")
    current_ts = str(current.get("ts_event") or "").replace("Z", "+00:00")
    if last_ts == current_ts:
        return "duplicate" if same_bar(last, current) else "conflict"
    last_ms = int(dt.datetime.fromisoformat(last_ts).timestamp() * 1000)
    current_ms = int(dt.datetime.fromisoformat(current_ts).timestamp() * 1000)
    return "out_of_order" if current_ms < last_ms else "new"


def freshness_error(bar_ms: int, server_ms: int, max_future_sec: float, max_delay_sec: float) -> str | None:
    if bar_ms > server_ms + int(max_future_sec * 1000):
        return "bar timestamp is in the future"
    if server_ms - bar_ms > int(max_delay_sec * 1000):
        return "bar is too old for live challenge intake"
    return None
