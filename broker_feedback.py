"""Normalize broker/relay callbacks for guardrails.

The execution webhook may be TradersPost or a custom relay. Providers use different
field names, so this module accepts common aliases and emits one stable event schema.
The relay should POST the normalized or provider-native payload to:

    POST /guard/broker-event?t=<BROKER_EVENT_TOKEN or GUARD_TOKEN>

Minimum identity: one of plan_id, signal_key, order_id. The strongest flow is for the
relay to echo plan_id and signal_key that AgentSignals placed in the order text.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Mapping

import timebase

TERMINAL = {"closed", "canceled", "rejected", "expired"}
OPEN = {"submitted", "accepted", "working", "partial", "filled"}
ALIASES = {
    "new": "accepted",
    "pending": "submitted",
    "pending_new": "submitted",
    "received": "submitted",
    "acknowledged": "accepted",
    "ack": "accepted",
    "open": "working",
    "active": "working",
    "partially_filled": "partial",
    "partfilled": "partial",
    "complete": "filled",
    "completed": "closed",
    "done": "closed",
    "cancelled": "canceled",
    "void": "canceled",
    "denied": "rejected",
}


@dataclass(frozen=True, slots=True)
class BrokerEvent:
    status: str
    event_ms: int
    plan_id: str | None = None
    signal_key: str | None = None
    order_id: str | None = None
    parent_order_id: str | None = None
    relay_signal_id: str | None = None
    relay_log_id: str | None = None
    ticker: str | None = None
    side: str | None = None
    quantity: int | None = None
    filled_quantity: int | None = None
    remaining_quantity: int | None = None
    avg_fill_price: float | None = None
    exit_price: float | None = None
    realized_pnl: float | None = None
    reason: str | None = None
    provider: str | None = None
    raw_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)



def _first(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(float(value))


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).replace("$", "").replace(",", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    return float(text)


def _status(data: Mapping[str, Any]) -> tuple[str, str]:
    raw = str(_first(data, "status", "orderStatus", "order_status", "state", "event", "type") or "").strip().lower()
    raw = raw.replace(" ", "_").replace("-", "_")
    status = ALIASES.get(raw, raw)
    if status not in OPEN | TERMINAL:
        # Infer terminal state from PnL/exit data only when provider omitted status.
        if _first(data, "realized_pnl", "realizedPnl", "pnl", "profitLoss") is not None:
            status = "closed"
        elif _first(data, "filled_quantity", "filledQuantity", "filledQty"):
            status = "filled"
        else:
            status = "submitted"
    return status, raw


def normalize(payload: Mapping[str, Any]) -> BrokerEvent:
    # Common webhook wrappers: {data:{...}}, {order:{...}}, {event:{...}}
    data: Mapping[str, Any] = payload
    for wrapper in ("data", "order", "payload"):
        nested = data.get(wrapper) if isinstance(data, Mapping) else None
        if isinstance(nested, Mapping):
            data = {**payload, **nested}
            break

    status, raw = _status(data)
    provider = str(_first(data, "provider", "source", "broker") or "") or None
    provider_key = (provider or "").strip().lower()
    # TradersPost returns a webhook Signal ID in the generic ``id`` field. It is not
    # a broker order ID and must never be used to claim broker acknowledgement.
    order_id = _first(data, "order_id", "orderId", "brokerOrderId")
    if order_id is None and provider_key not in {
        "execution-relay", "traderspost", "traderspost-relay", "traderspost_webhook"
    }:
        order_id = _first(data, "id")
    event_ms = timebase.parse_event_ms(
        _first(data, "event_ms", "eventMs", "timestamp", "time", "updatedAt", "updated_at", "createdAt"),
        default=timebase.now_ms(),
    )
    side = _first(data, "side", "action", "direction")
    if side is not None:
        side = str(side).upper()
        if side == "BUY": side = "LONG"
        if side == "SELL": side = "SHORT"

    return BrokerEvent(
        status=status,
        event_ms=event_ms,
        plan_id=str(_first(data, "plan_id", "planId", "client_plan_id") or "") or None,
        signal_key=str(_first(data, "signal_key", "signalKey", "client_signal_key") or "") or None,
        order_id=str(order_id or "") or None,
        parent_order_id=str(_first(data, "parent_order_id", "parentOrderId", "parentId") or "") or None,
        relay_signal_id=str(_first(data, "relay_signal_id", "relaySignalId", "signal_id", "signalId") or "") or None,
        relay_log_id=str(_first(data, "relay_log_id", "relayLogId", "log_id", "logId") or "") or None,
        ticker=str(_first(data, "ticker", "symbol", "instrument") or "") or None,
        side=side,
        quantity=_int_or_none(_first(data, "quantity", "qty", "orderQty")),
        filled_quantity=_int_or_none(_first(data, "filled_quantity", "filledQuantity", "filledQty", "cumQty")),
        remaining_quantity=_int_or_none(_first(data, "remaining_quantity", "remainingQuantity", "leavesQty")),
        avg_fill_price=_float_or_none(_first(data, "avg_fill_price", "avgFillPrice", "averagePrice", "fillPrice")),
        exit_price=_float_or_none(_first(data, "exit_price", "exitPrice", "closePrice")),
        realized_pnl=_float_or_none(_first(data, "realized_pnl", "realizedPnl", "pnl", "profitLoss")),
        reason=str(_first(data, "reason", "message", "rejectReason") or "") or None,
        provider=provider,
        raw_status=raw or None,
    )
