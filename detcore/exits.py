# detcore/exits.py
# Exit / risk rules. In the monolith the take-profit was an inline `2*risk` inside each entry
# function, and the risk cap was an inline compare inside emit(). They live here now so TP/SL/cap
# policy is in one place. Defaults (rr=2.0, cap=cfg.max_stop_r) reproduce v11 exactly.
#
# NOTE: the dynamic BE@1R / TP2R *simulation* is a separate concern that lives in compare_v11.py
# (and the live management layer manage.py), not in detection. det only emits a static TP = rr*R.


def take_profit(ctx, entry, risk, bull):
    """Static target: rr R above (long) / below (short) the entry. Was `2*risk` in det_v11."""
    rr = ctx.cfg.rr
    return round(entry + rr * risk, 2) if bull else round(entry - rr * risk, 2)


def exceeds_risk_cap(ctx, risk):
    """True if the stop distance is wider than the configured cap (setup is dropped)."""
    return risk > ctx.cfg.max_stop_r
