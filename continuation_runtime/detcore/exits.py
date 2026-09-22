# detcore/exits.py
# Exit / risk rules. In the monolith the take-profit was an inline `2*risk` inside each entry
# function, and the risk cap was an inline compare inside emit(). They live here now so TP/SL/cap
# policy is in one place. Defaults (rr=2.0, cap=cfg.max_stop_r) reproduce v11 exactly.
#
# NOTE: the dynamic BE@1R / TP2R *simulation* is a separate concern that lives in compare_v11.py
# (and the live management layer manage.py), not in detection. det only emits a static TP = rr*R.


# ---------------------------------------------------------------------------------------------
# v30 TARGET: the last swing level to the LEFT of the BOS bar (sell-side low for a short,
# buy-side high for a long), instead of a blind 2R.
#   - "last low / last high that was created": the most recent CONFIRMED fractal
#     (SWING_TP_K bars on each side, so a fractal is only visible once its right side has
#     printed -- every bar read is <= bos_bar: fully causal, live == backtest).
#   - "deeper": the level must sit at least SWING_TP_MIN_R x risk beyond the entry
#     (default 1R). A shallower swing is skipped and the scan keeps walking left.
#   - capped at SWING_TP_MAX_R x risk (default 3R): a further level is CLAMPED to 3R.
#   - no qualifying level inside SWING_TP_LOOKBACK bars -> fixed 2R (the old rule).
# SWING_TP=0 restores v29 behaviour (always rr*R) bit for bit.
# ---------------------------------------------------------------------------------------------
import os as _os


def swing_tp(ctx, entry, risk, bull, bos_bar):
    """Return (tp, level, 'swing') or None when no qualifying swing exists."""
    try:
        k    = max(1, int(float(_os.environ.get('SWING_TP_K', '5'))))
        look = max(k + 1, int(float(_os.environ.get('SWING_TP_LOOKBACK', '240'))))
        mn   = float(_os.environ.get('SWING_TP_MIN_R', '1.0'))
        mx   = float(_os.environ.get('SWING_TP_MAX_R', '3.0'))
    except Exception:
        k, look, mn, mx = 5, 240, 1.0, 3.0
    hi, lo = ctx.hi, ctx.lo
    bb = int(bos_bar)
    first = max(k, bb - look)
    for i in range(bb - k, first - 1, -1):          # newest confirmed fractal first
        if bull:
            w = hi[i - k:i + k + 1]
            if len(w) and hi[i] == w.max() and hi[i] >= entry + mn * risk:
                lvl = float(hi[i]); break
        else:
            w = lo[i - k:i + k + 1]
            if len(w) and lo[i] == w.min() and lo[i] <= entry - mn * risk:
                lvl = float(lo[i]); break
    else:
        return None
    d = min(abs(lvl - entry), mx * risk)             # cap 3R (clamped, not rejected)
    tp = round(entry + d, 2) if bull else round(entry - d, 2)
    return tp, round(lvl, 2), 'swing'


def take_profit(ctx, entry, risk, bull, bos_bar=None):
    """v30: swing-liquidity target when one qualifies; else the static rr*R (v29 rule).
    Callers that do not pass bos_bar (forex det, strategy_f) keep the old behaviour."""
    tp, _lvl, _src = tp_with_src(ctx, entry, risk, bull, bos_bar)
    return tp


def tp_with_src(ctx, entry, risk, bull, bos_bar=None):
    """Like take_profit() but also reports WHICH rule produced the target."""
    rr = ctx.cfg.rr
    fixed = round(entry + rr * risk, 2) if bull else round(entry - rr * risk, 2)
    if bos_bar is None or _os.environ.get('SWING_TP', '1') == '0':
        return fixed, None, '2R'
    z = swing_tp(ctx, entry, risk, bull, bos_bar)
    if z is None:
        return fixed, None, '2R'
    return z[0], z[1], 'swing'


def exceeds_risk_cap(ctx, risk):
    """True if the stop distance is wider than the configured cap (setup is dropped)."""
    return risk > ctx.cfg.max_stop_r
