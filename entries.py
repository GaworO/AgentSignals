# detcore/entries.py
# Stage 4: given a confirmed setup, WHERE do we enter (and the SL/TP that go with it)?
#   find_entry_v10      -> FVG-edge entry
#   find_entry_fibo_v10 -> OTE / fibo entry off the impulse extreme
#   get_entry_v10       -> pick primary per cfg.entry_primary, fall back to the other
# SL = CE of the displacement FVG; TP comes from exits.take_profit (was inline 2*risk in v11).
# Bodies verbatim from det_v11; ctx supplies arrays/params/helpers.
from .primitives import fvgs, impulse_end_v10
from .exits import take_profit


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
    sl = round((su['fvg'][0] + su['fvg'][1]) / 2, 2)
    fl_ = fvgs(ctx, ob, bb + 2, bull); seen = set(); fvl = []
    for f in fl_:
        if f[2] in seen: continue
        seen.add(f[2]); fvl.append(f)
    if not fvl: return None
    fvg = fvl[-1]; entry = fvg[1] if bull else fvg[0]
    risk = (entry - sl) if bull else (sl - entry)
    if risk <= 0: return None
    sl, risk = _cap_stop(ctx, entry, sl, risk, bull)   # v22 stop-cap re-anchor
    tp = take_profit(ctx, entry, risk, bull)
    return dict(entry=round(entry, 2), sl=sl, tp=tp, risk=risk, sfvg_bar=fvg[2])


def find_entry_fibo_v10(ctx, su, ote=0.62):
    bull = su['dr'] == 'LONG'; ob = su['origin_bar']; bb = su['bos_bar']
    sl = round((su['fvg'][0] + su['fvg'][1]) / 2, 2)
    hh, eb = impulse_end_v10(ctx, ob, bb, bull); hl = su['origin']
    entry = round(hh + ote * (hl - hh), 2); risk = (entry - sl) if bull else (sl - entry)
    if risk <= 0: return None
    sl, risk = _cap_stop(ctx, entry, sl, risk, bull)   # v22 stop-cap re-anchor
    tp = take_profit(ctx, entry, risk, bull)
    return dict(entry=entry, sl=sl, tp=tp, risk=risk, hh_bar=eb, ote=ote)


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
