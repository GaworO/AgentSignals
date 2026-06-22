# detcore/emit.py
# Stage 5: turn a confirmed trigger into an output record.
#   emit       -> displacement -> setup -> entry -> risk-cap -> append to ctx.out
#   try_chain  -> v10 chain (class A); falls back to DIB (class B) with the same v10 entry
#   _trc       -> optional debug trace ; session_end_bar -> legacy helper (kept for parity)
# Bodies verbatim from det_v11; ctx replaces the old module globals (out, _cur_break, df, ...).
import pandas as pd

from .confirmation import find_displacement, find_displacement_dib, find_setup_v10, bias_for
from .entries import get_entry_v10
from .exits import exceeds_risk_cap


def _trc(ctx, trigger, dr, model, name, stage, disp=None):
    if not ctx.cfg.debug_trace: return
    df = ctx.df
    r = dict(cat=name, model=model, dir=dr, trig=df.dt[trigger].strftime('%Y-%m-%d %H:%M'),
             trig_ms=int(df.dt[trigger].timestamp() * 1000), stage=stage)
    if disp is not None:
        r['disp_end'] = df.dt[disp['u']].strftime('%H:%M')
        r['fvg'] = [round(disp['fvg'][0], 1), round(disp['fvg'][1], 1)]
    ctx._TRC.append(r)


def session_end_bar(ctx, b):
    """Legacy helper from det_v11 (defined but unused there). Kept for parity."""
    df, n = ctx.df, ctx.n
    SESSION_BOUNDS = list(ctx.cfg.session_bounds)
    t = df.dt.iloc[b]; h = t.hour
    nb = min(x for x in SESSION_BOUNDS + [24] if x > h)
    bound = t.normalize() + pd.Timedelta(hours=nb); j = b
    while j + 1 < n and df.dt.iloc[j + 1] < bound: j += 1
    return j


def emit(ctx, t, model, name, dr, disp, conf=None):
    """v10 entry (FVG-edge / OTE), SL = CE of displacement FVG, TP = rr*R."""
    out, df, dates = ctx.out, ctx.df, ctx.dates
    _trc(ctx, t, dr, model, name, 'displacement OK', disp)
    su = find_setup_v10(ctx, disp, dr)
    if su is None: _trc(ctx, t, dr, model, name, 'brak setupu (odbicie/BOS)', disp); return
    _trc(ctx, t, dr, model, name, 'setup OK (BOS)', disp)
    e = get_entry_v10(ctx, su)
    if e is None: _trc(ctx, t, dr, model, name, 'brak wejscia', disp); return
    if exceeds_risk_cap(ctx, e['risk']):
        _trc(ctx, t, dr, model, name, f'odciety cap (R={e["risk"]:.0f}pkt)', disp); return
    _trc(ctx, t, dr, model, name, 'POTWIERDZONY', disp)
    b, pdv = bias_for(ctx, su['bos_bar'])
    align = 'Y' if b.replace('?', '') == dr else ('?' if '?' in b or b == 'niejasny' else 'N')
    out.append(dict(brk=ctx.cur_break, date=str(dates[su['bos_bar']]), model=model, cat=name, dir=dr,
        cls=('B' if '+DIB' in name else 'A'), entry=e['entry'], SL=e['sl'], TP=e['tp'], risk=e['risk'], kind=e['kind'],
        bias=b, bias_align=align, bos=df.dt[su['bos_bar']].strftime('%H:%M'),
        s=int(disp['s']), u=int(disp['u']), fvg_lo=round(disp['fvg'][0], 2), fvg_hi=round(disp['fvg'][1], 2),
        fvg_bar=int(disp['fvg_bar']), origin_bar=int(su['origin_bar']), bos_bar=int(su['bos_bar']), ce=round(su['ce'], 2),
        sfvg_bar=int(e['sfvg_bar']) if e.get('sfvg_bar') is not None else None,
        hh_bar=int(e['hh_bar']) if e.get('hh_bar') is not None else None,
        emit_bar=int(su['bos_bar']), entry_bar=int(e['start_bar']),
        bos_iso=df.dt[su['bos_bar']].strftime('%Y-%m-%dT%H:%M:%SZ'),
        bos_ms=int(df.dt[su['bos_bar']].timestamp() * 1000)))


def try_chain(ctx, trigger, dr, model, name):
    """v10 chain (class A); DIB = class B with the SAME v10 entry."""
    out = ctx.out
    cur = trigger
    for _ in range(3):
        d = find_displacement(ctx, cur, dr)
        if d is None: break
        before = len(out); emit(ctx, trigger, model, name, dr, d)
        if len(out) > before: return
        cur = d['u']
    d2 = find_displacement_dib(ctx, trigger, dr)
    if d2 is None: return
    emit(ctx, trigger, model, name + '+DIB', dr, d2)
