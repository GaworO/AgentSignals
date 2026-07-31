# Broker callback contract

Endpoint:

```text
POST /guard/broker-event?t=<token>
```

Authentication precedence:

1. `BROKER_EVENT_TOKEN`
2. `GUARD_TOKEN`
3. unauthenticated callbacks only when `ALLOW_UNAUTH_BROKER_EVENT=1`

At least one identity is mandatory:

```text
plan_id, signal_key, order_id
```

Matching is deliberately strict. The guard does not guess a live order by approximate price or time.

## Minimal events

```json
{"plan_id":"...","order_id":"...","status":"accepted","event_ms":1785432000123}
{"plan_id":"...","order_id":"...","status":"filled","event_ms":1785432015000,"filled_quantity":4,"avg_fill_price":30125.25}
{"plan_id":"...","order_id":"...","status":"closed","event_ms":1785432315000,"realized_pnl":397.76,"exit_price":30175.25}
```

Provider-native aliases such as `orderId`, `orderStatus`, `filledQty`, `avgFillPrice`, `realizedPnl` and `timestamp` are normalized automatically.

Unmatched callbacks are written to `broker_unmatched.json` and exposed as a count in `/guard/data`.
