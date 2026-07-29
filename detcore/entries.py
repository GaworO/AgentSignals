# detcore/entries.py
# Stage 4: given a confirmed setup, WHERE do we enter (and the SL/TP that go with it)?
#   find_entry_v10      -> FVG-edge entry
#   find_entry_fibo_v10 -> OTE / fibo entry off the impulse extreme
#   get_entry_v10       -> pick primary per cfg.entry_primary, fall back to the other
# SL = CE of the displacement FVG; TP comes from exits.take_profit (was inline 2*risk in v11).
# Bodies verbatim from det_v11; ctx supplies arrays/params/helpers.
import os
import numpy as np

from .primitives import fvgs, impulse_end_v10
from .exits import take_profit, tp_with_src

# ---------------------------------------------------------------------------------------------
# v29 STOP ANCHOR (default). Replaces "SL = CE of the displacement FVG", which was hard-coded in
# find_entry_v10 / find_entry_fibo_v10 since v10.
#
#   SL = the DISPLACEMENT-LEG extreme ("struct") whenever that sits within SL_STRUCT_MAX_R points
#        of the entry; otherwise SL = the FAR EDGE of the HELD FVG (short -> fvg high,
#        long -> fvg low). CE remains only as a degenerate fallback.
#
# Both levels are strictly causal - every bar read is <= su['bos_bar'], so live and backtest agree.
# The risk cap (cfg.max_stop_r / exits.exceeds_risk_cap) and the v22 stop-cap re-anchor are
# untouched and still run after this, so the "max SL" rules behave exactly as before.
# ---------------------------------------------------------------------------------------------
SL_STRUCT_MAX_R_DEFAULT = 30.0   # points; struct is used only while it fits inside this
SL_ANCHOR_BUF_DEFAULT   = 0.25   # points beyond the chosen level (1 MNQ tick)


def _envf(name, default):
    try: return float(os.environ.get(name, '') or default)
    except Exception: return float(default)


def struct_sl(ctx, su, bull):
    """The extreme of the whole displacement LEG: displacement start (su['s']) -> rejection origin.
    This is the swing the move came FROM, not the shallow wick that retested the gap."""
    s0, ob = int(su.get('s', su['origin_bar'])), int(su['origin_bar'])
    if ob < s0: s0 = ob
    ext = float(np.min(ctx.lo[s0:ob + 1])) if bull else float(np.max(ctx.hi[s0:ob + 1]))
    return round(ext, 2)


def held_fvg_edge(su, bull):
    """Far edge of the held FVG - the side a protective stop belongs on.
    LONG  -> the FVG low  (su['fvg'][0]);  SHORT -> the FVG high (su['fvg'][1])."""
    return round(float(su['fvg'][0] if bull else su['fvg'][1]), 2)


def pick_sl(ctx, su, bull, entry):
    """v29 anchor selection. Returns (sl, source, levels) where source is one of
    'struct' | 'fvg_edge' | 'ce' (+ '+capped' when the risk cap re-anchored it), and levels
    carries all three candidates plus the CE risk the emit-gate uses.

    RE-ANCHOR, NEVER DELETE: if the chosen level sits further than cfg.max_stop_r points from the
    entry the stop is pulled back TO the cap. The setup is kept. emit() still gates on the CE risk,
    exactly as v28 did, so v29 emits the SAME set of trades as v28 - it only moves the stop."""
    ce = round((su['fvg'][0] + su['fvg'][1]) / 2, 2)
    st = struct_sl(ctx, su, bull)
    ed = held_fvg_edge(su, bull)
    r = (lambda x: (entry - x) if bull else (x - entry))     # risk in points, positive = valid side
    k = _envf('SL_STRUCT_MAX_R', SL_STRUCT_MAX_R_DEFAULT)

    if 0 < r(st) <= k:      lvl, src = st, 'struct'
    elif r(ed) > 0:         lvl, src = ed, 'fvg_edge'
    else:                   lvl, src = ce, 'ce'

    buf = _envf('SL_ANCHOR_BUF', SL_ANCHOR_BUF_DEFAULT)
    sl = round(lvl - buf, 2) if bull else round(lvl + buf, 2)
    if r(sl) <= 0:                                            # degenerate -> old behaviour
        sl, src = ce, 'ce'

    # RE-ANCHOR to the cap rather than letting emit() discard the setup. Same max-SL ceiling,
    # no lost trades.
    cap = float(getattr(ctx.cfg, 'max_stop_r', 0) or 0)
    if cap > 0 and r(sl) > cap:
        sl = round(entry - cap, 2) if bull else round(entry + cap, 2)
        src = src + '+capped'
    return sl, src, dict(sl_ce=ce, sl_struct=st, sl_fvg_edge=ed,
                         risk_ce=round(abs(entry - ce), 2))


def _cap_stop(ctx, entry, sl, risk, bull):
    """v22 stop-cap re-anchor. If cfg.stop_cap > 0 and the structural (FVG-mid) risk exceeds
    cfg.stop_cap_trigger (0 => use stop_cap, i.e. cap every trade), move the SL to stop_cap
    points from entry. TP downstream uses the returned risk, so the 2R target follows automatically."""
    cap = getattr(ctx.cfg, 'stop_cap', 0.0) or 0.0
    if cap > 0:
        trig = (getattr(ctx.cfg, 'stop_cap_trigger', 0.0) or 0.0) or cap
        if risk > trig:
            sl = round(entry - cap, 2) if bull else round(entry + cap, 2)
            risk = cap
    return sl, round(risk, 2)


def find_entry_v10(ctx, su):
    bull = su['dr'] == 'LONG'; ob = su['origin_bar']; bb = su['bos_bar']
    # v28 CAUSAL: an FVG whose middle bar is bb+1 does not exist yet when the detector fires on bb.
    # Live never sees it (the buffer ends at bb); every backtest did -> ~0.23R/fill of phantom edge.
    lim = (bb + 1) if getattr(ctx.cfg, 'causal', True) else (bb + 2)
    fl_ = fvgs(ctx, ob, lim, bull); seen = set(); fvl = []
    for f in fl_:
        if f[2] in seen: continue
        seen.add(f[2]); fvl.append(f)
    if not fvl: return None
    fvg = fvl[-1]; entry = fvg[1] if bull else fvg[0]
    sl, sl_src, lv = pick_sl(ctx, su, bull, entry)     # v29 anchor (was: CE of the displacement FVG)
    risk = (entry - sl) if bull else (sl - entry)
    if risk <= 0: return None
    sl, risk = _cap_stop(ctx, entry, sl, risk, bull)   # v22 stop-cap re-anchor
    tp, tp_level, tp_src = tp_with_src(ctx, entry, risk, bull, bb)   # v30 swing-liquidity target
    return dict(entry=round(entry, 2), sl=sl, tp=tp, risk=risk, sfvg_bar=fvg[2],
                sl_src=sl_src, tp_src=tp_src, tp_level=tp_level, **lv)


def find_entry_fibo_v10(ctx, su, ote=0.62):
    bull = su['dr'] == 'LONG'; ob = su['origin_bar']; bb = su['bos_bar']
    hh, eb = impulse_end_v10(ctx, ob, bb, bull); hl = su['origin']
    entry = round(hh + ote * (hl - hh), 2)
    sl, sl_src, lv = pick_sl(ctx, su, bull, entry)     # v29 anchor (was: CE of the displacement FVG)
    risk = (entry - sl) if bull else (sl - entry)
    if risk <= 0: return None
    sl, risk = _cap_stop(ctx, entry, sl, risk, bull)   # v22 stop-cap re-anchor
    tp, tp_level, tp_src = tp_with_src(ctx, entry, risk, bull, bb)   # v30 swing-liquidity target
    return dict(entry=entry, sl=sl, tp=tp, risk=risk, hh_bar=eb, ote=ote,
                sl_src=sl_src, tp_src=tp_src, tp_level=tp_level, **lv)


def _ent_fvg(ctx, su):
    e = find_entry_v10(ctx, su)
    return dict(**e, kind='FVG', start_bar=max(e['sfvg_bar'], su['bos_bar']) + 1) if e else None


def _ent_fibo(ctx, su):
    e = find_entry_fibo_v10(ctx, su)
    return dict(**e, kind='FIBO', start_bar=max(e['hh_bar'], su['bos_bar']) + 1) if e else None


def get_entry_v10(ctx, su):
    if ctx.cfg.entry_primary == 'fibo':
        return _ent_fibo(ctx, su) or _ent_fvg(ctx, su)
    return _ent_fvg(ctx, su) or _ent_fibo(ctx, su)
