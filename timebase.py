"""Canonical time handling for AgentSignals v32.

Design
------
* Persist and compare timestamps as UTC epoch milliseconds.
* Interpret strategy sessions on ONE fixed chart clock: UTC-04:00.
* Never derive session labels from the host machine timezone.
* Exchange-calendar rules are the sole exception and remain in America/Chicago.

Important: fixed UTC-04:00 is intentionally NOT daylight-saving aware. In winter it
is one hour ahead of New York civil time. This preserves the strategy's existing
TFO/fixed-UTC-4 definitions and makes detector, guard, shadow and executor agree.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

UTC = dt.timezone.utc
STRATEGY_OFFSET = dt.timedelta(hours=-4)
STRATEGY_TZ = dt.timezone(STRATEGY_OFFSET, name="UTC-04:00")
# Pandas/ZoneInfo-compatible fixed-offset alias. The sign is reversed in Etc/GMT names.
STRATEGY_TZ_NAME = "Etc/GMT+4"

SESSION_NAMES = ("ASIA", "LO", "PREM", "NYAM", "NYL", "NYPM", "PM_AH")


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def utc_now_naive() -> dt.datetime:
    """UTC now without tzinfo for compatibility with older subtraction code."""
    return utc_now().replace(tzinfo=None)


def now_ms() -> int:
    return int(utc_now().timestamp() * 1000)


def utc_from_ms(ms: int | float) -> dt.datetime:
    return dt.datetime.fromtimestamp(float(ms) / 1000.0, tz=UTC)


def strategy_from_ms(ms: int | float) -> dt.datetime:
    return utc_from_ms(ms).astimezone(STRATEGY_TZ)


def strategy_now() -> dt.datetime:
    return utc_now().astimezone(STRATEGY_TZ)


def utc_iso(ms: int | float | None = None, *, timespec: str = "seconds") -> str:
    value = utc_now() if ms is None else utc_from_ms(ms)
    return value.isoformat(timespec=timespec).replace("+00:00", "Z")


def strategy_iso(ms: int | float | None = None, *, timespec: str = "minutes") -> str:
    value = strategy_now() if ms is None else strategy_from_ms(ms)
    return value.isoformat(timespec=timespec)


def session_name(value: dt.datetime | int | float) -> str:
    """Return the strategy session on the fixed UTC-04:00 clock.

    Boundaries match the existing shadow/guard definitions:
      ASIA 18:00-01:59, LO 02:00-04:59, PREM 05:00-09:29,
      NYAM 09:30-10:59, NYL 11:00-13:29, NYPM 13:30-15:59,
      PM_AH 16:00-17:59.
    """
    if isinstance(value, (int, float)):
        value = strategy_from_ms(value)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=STRATEGY_TZ)
    else:
        value = value.astimezone(STRATEGY_TZ)
    minute = value.hour * 60 + value.minute
    if minute >= 18 * 60 or minute < 2 * 60:
        return "ASIA"
    if minute < 5 * 60:
        return "LO"
    if minute < 9 * 60 + 30:
        return "PREM"
    if minute < 11 * 60:
        return "NYAM"
    if minute < 13 * 60 + 30:
        return "NYL"
    if minute < 16 * 60:
        return "NYPM"
    return "PM_AH"


def trading_day(value: dt.datetime | int | float | None = None) -> str:
    """18:00 strategy-clock rollover used by guard daily limits and dedup."""
    if value is None:
        value = strategy_now()
    elif isinstance(value, (int, float)):
        value = strategy_from_ms(value)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=STRATEGY_TZ)
    else:
        value = value.astimezone(STRATEGY_TZ)
    if value.hour >= 18:
        value = value + dt.timedelta(days=1)
    return value.strftime("%Y-%m-%d")


def parse_event_ms(value: Any, default: int | None = None) -> int:
    """Parse broker/event timestamps into UTC epoch milliseconds.

    Accepts epoch seconds, epoch milliseconds, datetime, or ISO-8601. Naive ISO
    values are interpreted as UTC to avoid depending on the server timezone.
    """
    if value is None or value == "":
        if default is not None:
            return int(default)
        return now_ms()
    if isinstance(value, dt.datetime):
        d = value if value.tzinfo else value.replace(tzinfo=UTC)
        return int(d.timestamp() * 1000)
    if isinstance(value, (int, float)):
        n = float(value)
        return int(n if abs(n) >= 10_000_000_000 else n * 1000)
    text = str(value).strip()
    try:
        n = float(text)
        return int(n if abs(n) >= 10_000_000_000 else n * 1000)
    except ValueError:
        pass
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    d = dt.datetime.fromisoformat(text)
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return int(d.astimezone(UTC).timestamp() * 1000)
