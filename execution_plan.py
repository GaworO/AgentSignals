"""Canonical execution plan for AgentSignals.

This module is deliberately dependency-free.  It converts one detector signal into
one immutable, validated description of the order that every downstream component
must use: broker payloads, shadow scoring, management alerts and guard accounting.

The detector may continue to emit its native fields (entry, SL, TP, bos_ms,
entry_ms).  ``build_execution_plan`` normalises those values once and stores the
serialised plan on the signal under ``_execution_plan`` for backwards-compatible
integration with the current codebase.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import datetime as dt
import hashlib
import json
import math
import os
import time
from typing import Any, Mapping, MutableMapping, Sequence


SCHEMA_VERSION = 1


class PlanValidationError(ValueError):
    """Raised when an execution plan cannot represent a valid bracket order."""


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def broker_action(self) -> str:
        return "buy" if self is Side.LONG else "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1


@dataclass(frozen=True, slots=True)
class ExecutionLeg:
    quantity: int
    take_profit: float
    label: str = "runner"

    def to_dict(self) -> dict[str, Any]:
        return {
            "quantity": int(self.quantity),
            "take_profit": float(self.take_profit),
            "label": str(self.label),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionLeg":
        return cls(
            quantity=int(value["quantity"]),
            take_profit=float(value["take_profit"]),
            label=str(value.get("label") or "runner"),
        )


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    plan_id: str
    signal_key: str
    strategy: str
    ticker: str
    side: Side
    order_type: str
    entry: float
    stop_loss: float
    legs: tuple[ExecutionLeg, ...]
    tick_size: float
    time_in_force: str
    signal_ms: int
    active_from_ms: int
    valid_until_ms: int
    created_ms: int
    fill_through_ticks: int = 1
    allow_fill_bar_take_profit: bool = False
    adverse_first: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict, compare=False)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise PlanValidationError(f"unsupported schema_version={self.schema_version}")
        if not self.plan_id or not self.signal_key:
            raise PlanValidationError("plan_id and signal_key are required")
        if self.order_type.lower() != "limit":
            raise PlanValidationError("only limit entry orders are supported")
        if self.tick_size <= 0:
            raise PlanValidationError("tick_size must be positive")
        if self.entry <= 0 or self.stop_loss <= 0:
            raise PlanValidationError("entry and stop_loss must be positive")
        if not self.legs:
            raise PlanValidationError("at least one take-profit leg is required")
        if any(leg.quantity <= 0 for leg in self.legs):
            raise PlanValidationError("every leg quantity must be positive")
        if self.active_from_ms < self.signal_ms:
            raise PlanValidationError("active_from_ms cannot precede signal_ms")
        if self.valid_until_ms <= self.active_from_ms:
            raise PlanValidationError("valid_until_ms must be after active_from_ms")
        if self.fill_through_ticks < 0:
            raise PlanValidationError("fill_through_ticks cannot be negative")
        self._validate_price(self.entry, "entry")
        self._validate_price(self.stop_loss, "stop_loss")
        for leg in self.legs:
            self._validate_price(leg.take_profit, f"take_profit:{leg.label}")
        if self.side is Side.LONG:
            if not self.stop_loss < self.entry:
                raise PlanValidationError("LONG stop_loss must be below entry")
            if any(leg.take_profit <= self.entry for leg in self.legs):
                raise PlanValidationError("LONG take-profit must be above entry")
        else:
            if not self.stop_loss > self.entry:
                raise PlanValidationError("SHORT stop_loss must be above entry")
            if any(leg.take_profit >= self.entry for leg in self.legs):
                raise PlanValidationError("SHORT take-profit must be below entry")

    def _validate_price(self, price: float, label: str) -> None:
        units = price / self.tick_size
        if abs(units - round(units)) > 1e-7:
            raise PlanValidationError(f"{label}={price} is not aligned to tick {self.tick_size}")

    @property
    def total_quantity(self) -> int:
        return sum(leg.quantity for leg in self.legs)

    @property
    def risk_points(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def primary_take_profit(self) -> float:
        runner = [leg for leg in self.legs if leg.label == "runner"]
        return (runner[-1] if runner else self.legs[-1]).take_profit

    def reward_r(self, take_profit: float) -> float:
        return abs(float(take_profit) - self.entry) / self.risk_points

    def weighted_target_r(self) -> float:
        return sum(leg.quantity * self.reward_r(leg.take_profit) for leg in self.legs) / self.total_quantity

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "signal_key": self.signal_key,
            "strategy": self.strategy,
            "ticker": self.ticker,
            "side": self.side.value,
            "order_type": self.order_type,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "legs": [leg.to_dict() for leg in self.legs],
            "tick_size": self.tick_size,
            "time_in_force": self.time_in_force,
            "signal_ms": self.signal_ms,
            "active_from_ms": self.active_from_ms,
            "valid_until_ms": self.valid_until_ms,
            "created_ms": self.created_ms,
            "fill_through_ticks": self.fill_through_ticks,
            "allow_fill_bar_take_profit": self.allow_fill_bar_take_profit,
            "adverse_first": self.adverse_first,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionPlan":
        return cls(
            schema_version=int(value.get("schema_version", SCHEMA_VERSION)),
            plan_id=str(value["plan_id"]),
            signal_key=str(value["signal_key"]),
            strategy=str(value.get("strategy") or "A/B"),
            ticker=str(value["ticker"]),
            side=Side(str(value["side"]).upper()),
            order_type=str(value.get("order_type") or "limit").lower(),
            entry=float(value["entry"]),
            stop_loss=float(value["stop_loss"]),
            legs=tuple(ExecutionLeg.from_dict(x) for x in value["legs"]),
            tick_size=float(value["tick_size"]),
            time_in_force=str(value.get("time_in_force") or "day").lower(),
            signal_ms=int(value["signal_ms"]),
            active_from_ms=int(value["active_from_ms"]),
            valid_until_ms=int(value["valid_until_ms"]),
            created_ms=int(value.get("created_ms") or value["signal_ms"]),
            fill_through_ticks=int(value.get("fill_through_ticks", 1)),
            allow_fill_bar_take_profit=bool(value.get("allow_fill_bar_take_profit", False)),
            adverse_first=bool(value.get("adverse_first", True)),
            metadata=dict(value.get("metadata") or {}),
        )

    def broker_payloads(self, text: str | None = None) -> list[dict[str, Any]]:
        """Return the exact TradersPost-compatible payloads represented by this plan.

        ``cancelAfter`` is derived from the plan's absolute validity window so an entry
        cannot remain working at TradersPost after AgentSignals considers it expired.
        Identity is stored in the documented ``extras`` object and repeated in ``text``
        for operator visibility. TradersPost's response ID is a *signal ID*, not a broker
        order ID; downstream code must keep those concepts separate.
        """
        payloads: list[dict[str, Any]] = []
        cancel_after = max(1, min(3600, int(math.ceil(
            (self.valid_until_ms - self.active_from_ms) / 1000.0
        ))))
        signal_time = dt.datetime.fromtimestamp(
            self.signal_ms / 1000.0, tz=dt.timezone.utc
        ).isoformat().replace("+00:00", "Z")
        for index, leg in enumerate(self.legs):
            identity = f"[plan:{self.plan_id}] [signal:{self.signal_key}] [leg:{index + 1}/{len(self.legs)}]"
            payload: dict[str, Any] = {
                "ticker": self.ticker,
                "action": self.side.broker_action,
                "orderType": self.order_type,
                "limitPrice": self.entry,
                "quantity": leg.quantity,
                "takeProfit": {"limitPrice": leg.take_profit},
                "stopLoss": {"type": "stop", "stopPrice": self.stop_loss},
                "timeInForce": self.time_in_force,
                "time": signal_time,
                "cancelAfter": cancel_after,
                "extras": {
                    "plan_id": self.plan_id,
                    "signal_key": self.signal_key,
                    "strategy": self.strategy,
                    "leg": index + 1,
                    "leg_count": len(self.legs),
                    "active_from_ms": self.active_from_ms,
                    "valid_until_ms": self.valid_until_ms,
                    "schema_version": self.schema_version,
                },
                "text": (f"{text}\n{identity}" if text else identity).strip(),
            }
            payloads.append(payload)
        return payloads


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    ticker: str = "MNQ1!"
    tick_size: float = 0.25
    price_offset: float = 0.0
    time_in_force: str = "day"
    fill_window_min: int = 10
    bar_interval_ms: int = 60_000
    fill_through_ticks: int = 1
    allow_fill_bar_take_profit: bool = False
    adverse_first: bool = True
    partial_at_1r: bool = False
    risk_pct: float = 0.5
    partial_account_pct: float = 0.2

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ExecutionConfig":
        e = os.environ if env is None else env
        return cls(
            ticker=(e.get("EXEC_TICKER") or e.get("CONTRACT") or "MNQ1!"),
            tick_size=float(e.get("EXEC_TICK", "0.25") or 0.25),
            price_offset=float(e.get("PRICE_OFFSET", "0") or 0),
            time_in_force=(e.get("EXEC_TIF", "day").strip().lower() or "day"),
            fill_window_min=max(1, int(float(e.get("FILL_WIN_MIN", "10") or 10))),
            bar_interval_ms=max(1, int(float(e.get("EXEC_BAR_MS", "60000") or 60000))),
            fill_through_ticks=max(0, int(float(e.get("EXEC_FILL_THROUGH_TICKS", "1") or 1))),
            allow_fill_bar_take_profit=(e.get("EXEC_FILLBAR_TP", "0") == "1"),
            adverse_first=(e.get("EXEC_ADVERSE_FIRST", "1") != "0"),
            partial_at_1r=(e.get("PARTIAL_AT_1R", "0") == "1"),
            risk_pct=float(e.get("RISK_PCT", "0.5") or 0.5),
            partial_account_pct=float(e.get("PARTIAL_ACCT_PCT", "0.2") or 0.2),
        )


def tick_align(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        raise PlanValidationError("tick_size must be positive")
    return round(round(float(price) / tick_size) * tick_size, 8)


def _default_signal_key(signal: Mapping[str, Any], strategy: str) -> str:
    parts = (
        strategy,
        str(signal.get("date") or ""),
        str(signal.get("model") or ""),
        str(signal.get("cat") or ""),
        str(signal.get("dir") or ""),
        str(signal.get("bos") or signal.get("bos_ms") or ""),
        f"{float(signal.get('entry') or 0):.8f}",
    )
    return "|".join(parts)


def _plan_id(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return "ep_" + hashlib.sha256(encoded).hexdigest()[:24]


def build_execution_plan(
    signal: Mapping[str, Any],
    quantity: int,
    *,
    strategy: str = "A/B",
    signal_key: str | None = None,
    config: ExecutionConfig | None = None,
    now_ms: int | None = None,
) -> ExecutionPlan:
    """Build, validate and return the canonical plan for one detector signal."""
    cfg = config or ExecutionConfig.from_env()
    side = Side(str(signal["dir"]).upper())
    quantity = int(quantity)
    if quantity <= 0:
        raise PlanValidationError("quantity must be positive")

    raw_entry = float(signal["entry"])
    raw_stop = float(signal["SL"])
    raw_risk = abs(raw_entry - raw_stop)
    if raw_risk <= 0:
        raise PlanValidationError("detector emitted a zero-distance stop")
    fallback_tp = raw_entry + side.sign * 2.0 * raw_risk
    raw_target = float(signal.get("TP") if signal.get("TP") is not None else fallback_tp)

    entry = tick_align(raw_entry + cfg.price_offset, cfg.tick_size)
    stop = tick_align(raw_stop + cfg.price_offset, cfg.tick_size)
    target = tick_align(raw_target + cfg.price_offset, cfg.tick_size)
    final_risk = abs(entry - stop)
    if final_risk <= 0:
        raise PlanValidationError("tick alignment collapsed the stop distance")

    legs: list[ExecutionLeg] = [ExecutionLeg(quantity=quantity, take_profit=target, label="runner")]
    if cfg.partial_at_1r and quantity >= 2 and cfg.risk_pct > 0:
        fraction = max(0.0, min(0.9, cfg.partial_account_pct / cfg.risk_pct))
        banker_qty = int(round(quantity * fraction))
        if 0 < banker_qty < quantity:
            one_r = tick_align(entry + side.sign * final_risk, cfg.tick_size)
            legs = [
                ExecutionLeg(quantity=banker_qty, take_profit=one_r, label="banker_1R"),
                ExecutionLeg(quantity=quantity - banker_qty, take_profit=target, label="runner"),
            ]

    _signal_ms_value = signal.get("bos_ms")
    signal_ms = int(_signal_ms_value if _signal_ms_value is not None else (now_ms or time.time() * 1000))
    proposed_active = int(signal.get("entry_ms") or (signal_ms + cfg.bar_interval_ms))
    active_from_ms = max(signal_ms + cfg.bar_interval_ms, proposed_active)
    valid_until_ms = active_from_ms + cfg.fill_window_min * 60_000
    created_ms = int(now_ms or time.time() * 1000)
    key = signal_key or str(signal.get("_signal_key") or _default_signal_key(signal, strategy))

    metadata = {
        "date": signal.get("date"),
        "bos": signal.get("bos"),
        "model": signal.get("model"),
        "cat": signal.get("cat"),
        "class": signal.get("cls") or signal.get("grade"),
        "session": signal.get("sess") or signal.get("session"),
        "sl_src": signal.get("sl_src"),
        "tp_src": signal.get("tp_src"),
    }
    identity = {
        "schema_version": SCHEMA_VERSION,
        "signal_key": key,
        "strategy": strategy,
        "ticker": cfg.ticker,
        "side": side.value,
        "entry": entry,
        "stop_loss": stop,
        "legs": [leg.to_dict() for leg in legs],
        "signal_ms": signal_ms,
        "active_from_ms": active_from_ms,
        "valid_until_ms": valid_until_ms,
    }
    return ExecutionPlan(
        plan_id=_plan_id(identity),
        signal_key=key,
        strategy=strategy,
        ticker=cfg.ticker,
        side=side,
        order_type="limit",
        entry=entry,
        stop_loss=stop,
        legs=tuple(legs),
        tick_size=cfg.tick_size,
        time_in_force=cfg.time_in_force,
        signal_ms=signal_ms,
        active_from_ms=active_from_ms,
        valid_until_ms=valid_until_ms,
        created_ms=created_ms,
        fill_through_ticks=cfg.fill_through_ticks,
        allow_fill_bar_take_profit=cfg.allow_fill_bar_take_profit,
        adverse_first=cfg.adverse_first,
        metadata=metadata,
    )


def attach_plan(signal: MutableMapping[str, Any], plan: ExecutionPlan) -> MutableMapping[str, Any]:
    """Attach a serialised plan and legacy compatibility fields to ``signal``."""
    signal["_execution_plan"] = plan.to_dict()
    signal["_exec_entry"] = plan.entry
    signal["_exec_sl"] = plan.stop_loss
    signal["_exec_tp"] = plan.primary_take_profit
    signal["_legs"] = [{"qty": leg.quantity, "tp": leg.take_profit, "label": leg.label} for leg in plan.legs]
    signal["_plan_id"] = plan.plan_id
    signal["_signal_key"] = plan.signal_key
    return signal


def plan_from_signal(signal: Mapping[str, Any]) -> ExecutionPlan | None:
    value = signal.get("_execution_plan")
    if not isinstance(value, Mapping):
        return None
    return ExecutionPlan.from_dict(value)
