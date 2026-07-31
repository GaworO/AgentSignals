# Rolling buffer and canonical ExecutionPlan

## Rolling buffer conclusion

The current detector uses `CAP_DAYS=10`, so levels expire after ten trading days. The existing
14,000-bar buffer already covers roughly ten full MNQ sessions plus indicator warm-up.

### Four-year archive checks

- 20 random causal checkpoints, 120 recent bars inspected at each checkpoint:
  - 14,000 bars: 33 signals
  - 20,000 bars: 33 signals
  - 28,000 bars: 33 signals
  - Pairwise differences: zero.
- Four checkpoints previously identified as sensitive to rolling/global reconstruction:
  - 14k, 20k, 28k and 56k produced the same recent signal sets.
  - 7k missed one signal at one checkpoint.

**Conclusion:** there is no evidence that increasing beyond 14k improves expectancy or creates
better A/B signals. A 20k buffer can be used as an operational safety margin for feed gaps,
holidays and warm-up, but should not be described as a performance improvement. It also increases
CPU, memory and detector latency. Keep 14k unless the deployed service has comfortable headroom;
if changing it, compare 14k/20k/28k in a new rolling out-of-sample replay.

## What the patch fixes

1. Detector TP is the same TP sent to the broker and scored by shadow.
2. Entry, SL and every TP are tick-aligned once.
3. The first tradable bar is explicit (`active_from_ms`) with no hidden extra bar.
4. Entry expiry is an absolute timestamp (`valid_until_ms`).
5. Through-fill and adverse-first rules are shared.
6. Favourable fill-bar extremes are ignored by default.
7. `manage.py` follows only broker-accepted orders.
8. Multi-leg send is successful only when every leg is accepted.
9. Guard P&L uses the real target multiple, not fixed +2R.
10. All components join on one `signal_key` / `plan_id`.

## Remaining broker limitation

The current TradersPost adapter exposes ticker-wide cancel rather than a confirmed broker order ID
and native 10-minute TTL. The patch cancels a partial multi-leg send and the existing orphan sweep
cancels expired limits, but truly race-free per-order cancellation requires the relay to return and
accept a broker order ID. Until that exists, keep one-position guard enabled and reconcile broker
fills during the ramp.

## Installation

Overlay the patch files on the repository, install the existing requirements and run:

```bash
python -m unittest discover -s tests -v
```

Then follow `EXECUTION_PLAN_DEPLOY.md` for shadow-only, one-contract and normal-size rollout.
