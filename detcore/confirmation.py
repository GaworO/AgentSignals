# detcore/confirmation.py
# Stage 3: does a tapped level actually CONFIRM a setup?
#   find_displacement / _dib -> impulse that breaks structure and leaves an FVG
#   find_rejection_v10       -> price rejects from the displacement FVG (body holds 50%)
#   find_setup_v10           -> rejection + BOS = a tradeable setup
#   bias_for                 -> HTF bias flag (not a filter, just a tag)
# Bodies verbatim from det_v11; ctx supplies arrays/params, and the two cross-stage helpers
# (fvgs from primitives, vi_draw from catalysts) are passed ctx explicitly.
import numpy as np

from .primitives import fvgs
from .catalysts import vi_draw


def find_displacement(ctx, t, dr):
    """From trigger bar t, find an impulse: breaks structure + leaves FVG + strong.
    disp_mode='chain' (V1, default): start a >=minimp same-colour run, extend through the WHOLE
    unbroken run, treat it as one displacement. 'orig' (V0): shortest 1-3 candle impulse."""
    o, hi, lo, cl, ATR, n = ctx.o, ctx.hi, ctx.lo, ctx.cl, ctx.ATR, ctx.n
    DISPWIN, MAXIMP, LOOKBACK, ATRMULT = ctx.cfg.dispwin, ctx.cfg.maximp, ctx.cfg.lookback, ctx.cfg.atrmult
    bull = dr == 'LONG'
    if ctx.cfg.disp_mode == 'chain':                  # ---- V1: start >=minimp candles, extend the whole same-colour run ----
        MINIMP, MAXEXT = ctx.cfg.minimp, ctx.cfg.maxext
        for s in range(t + 1, min(t + 1 + DISPWIN, n)):
            if not ((cl[s] > o[s]) if bull else (cl[s] < o[s])): continue   # s = first candle of a same-colour run
            u = s
            while u + 1 < min(s + MAXEXT, n) and ((cl[u + 1] > o[u + 1]) if bull else (cl[u + 1] < o[u + 1])): u += 1
            if (u - s + 1) < MINIMP: continue          # the run must be >= MINIMP candles
            body = sum((cl[x] - o[x]) if bull else (o[x] - cl[x]) for x in range(s, u + 1))
            if body <= 0: continue
            prior = max(hi[max(0, s - LOOKBACK):s]) if bull else min(lo[max(0, s - LOOKBACK):s])
            if not ((cl[u] > prior) if bull else (cl[u] < prior)): continue
            atr5 = ATR[u] if ATR[u] > 0 else 1e9
            maxbody = max((abs(cl[x] - o[x])) for x in range(max(0, s - 10), s)) if s > 0 else 0
            if body < ATRMULT * atr5: continue
            if body < maxbody: continue
            fl = fvgs(ctx, s, u + 2, bull)
            if not fl: continue
            f = fl[-1]; swlo = float(min(lo[s:u + 1])); swhi = float(max(hi[s:u + 1]))
            return dict(s=s, u=u, L=u - s + 1, body=round(body, 1), fvg=(f[0], f[1]), fvg_bar=f[2],
                        swlo=swlo, swhi=swhi, atr5=round(atr5, 1))
        return None
    for u in range(t + 1, min(t + 1 + DISPWIN, n)):   # ---- V0: shortest 1-3 candle impulse ----
        for L in range(1, MAXIMP + 1):
            s = u - L + 1
            if s <= t: continue
            same = all((cl[x] > o[x]) if bull else (cl[x] < o[x]) for x in range(s, u + 1))
            if not same: continue
            body = sum((cl[x] - o[x]) if bull else (o[x] - cl[x]) for x in range(s, u + 1))
            if body <= 0: continue
            prior = max(hi[max(0, s - LOOKBACK):s]) if bull else min(lo[max(0, s - LOOKBACK):s])
            broke = (cl[u] > prior) if bull else (cl[u] < prior)
            if not broke: continue
            atr5 = ATR[u] if ATR[u] > 0 else 1e9
            maxbody = max((abs(cl[x] - o[x])) for x in range(max(0, s - 10), s)) if s > 0 else 0
            if body < ATRMULT * atr5: continue
            if body < maxbody: continue
            fl = fvgs(ctx, s, u + 2, bull)
            if not fl: continue
            f = fl[-1]                       # freshest displacement FVG
            swlo = float(min(lo[s:u + 1])); swhi = float(max(hi[s:u + 1]))
            return dict(s=s, u=u, L=L, body=round(body, 1), fvg=(f[0], f[1]), fvg_bar=f[2],
                        swlo=swlo, swhi=swhi, atr5=round(atr5, 1))
    return None


def find_displacement_dib(ctx, t, dr):
    o, hi, lo, cl, ATR, n = ctx.o, ctx.hi, ctx.lo, ctx.cl, ctx.ATR, ctx.n
    MAXIMP, LOOKBACK, ATRMULT = ctx.cfg.maximp, ctx.cfg.lookback, ctx.cfg.atrmult
    bull = dr == 'LONG'; best = None
    for u in range(max(MAXIMP, t - 2), min(t + 3, n)):
        for L in range(1, MAXIMP + 1):
            s = u - L + 1
            if s < 1: continue
            same = all((cl[x] > o[x]) if bull else (cl[x] < o[x]) for x in range(s, u + 1))
            if not same: continue
            body = sum((cl[x] - o[x]) if bull else (o[x] - cl[x]) for x in range(s, u + 1))
            if body <= 0: continue
            prior = max(hi[max(0, s - LOOKBACK):s]) if bull else min(lo[max(0, s - LOOKBACK):s])
            broke = (cl[u] > prior) if bull else (cl[u] < prior)
            if not broke: continue
            atr5 = ATR[u] if ATR[u] > 0 else 1e9
            maxbody = max((abs(cl[x] - o[x])) for x in range(max(0, s - 10), s)) if s > 0 else 0
            if body < ATRMULT * atr5: continue
            if body < maxbody: continue
            fl = fvgs(ctx, s, u + 2, bull)
            if not fl: continue
            f = fl[-1]; swlo = float(min(lo[s:u + 1])); swhi = float(max(hi[s:u + 1]))
            cand = dict(s=s, u=u, L=L, body=round(body, 1), fvg=(f[0], f[1]), fvg_bar=f[2],
                        swlo=swlo, swhi=swhi, atr5=round(atr5, 1))
            if best is None or body > best['body']: best = cand
    return best


def find_rejection_v10(ctx, disp, dr):
    hi, lo, cl, n = ctx.hi, ctx.lo, ctx.cl, ctx.n
    RETWIN = ctx.cfg.retwin
    bull = dr == 'LONG'; fl, fh = disp['fvg']; ce = round((fl + fh) / 2, 2); fb = disp['fvg_bar']
    rf = ctx.cfg.rej_frac                                    # rejection invalidation/body-hold threshold as frac of gap from entry side (0.5 = CE)
    thr = round((fl + rf * (fh - fl)) if not bull else (fh - rf * (fh - fl)), 2)
    origin = None; ob = None; tests = []; broke = None
    for j in range(fb + 1, min(fb + 1 + RETWIN, n)):
        if (cl[j] > thr) if not bull else (cl[j] < thr): broke = j; break
        wick = (hi[j] >= fl) if not bull else (lo[j] <= fh)
        body = (cl[j] <= thr) if not bull else (cl[j] >= thr)
        if wick and body:
            ext = hi[j] if not bull else lo[j]; tests.append(j)
            if origin is None or (ext > origin if not bull else ext < origin): origin, ob = ext, j
    if broke is not None or origin is None: return None
    return dict(ce=ce, origin=round(origin, 2), origin_bar=ob, tests=tests)


def find_setup_v10(ctx, disp, dr):
    hi, lo, cl, n = ctx.hi, ctx.lo, ctx.cl, ctx.n
    BOSWIN = ctx.cfg.boswin
    bull = dr == 'LONG'; rej = find_rejection_v10(ctx, disp, dr)
    if not rej: return None
    origin = rej['origin']; ob = rej['origin_bar']; ce = rej['ce']; s = disp['s']; u = disp['u']
    fl, fh = disp['fvg']; rf = ctx.cfg.rej_frac             # same threshold as the rejection (0.5 = CE)
    thr = round((fl + rf * (fh - fl)) if not bull else (fh - rf * (fh - fl)), 2)
    struct0 = float(max(hi[s:ob])) if bull else float(min(lo[s:ob])); level = struct0
    for j in range(ob + 1, min(ob + 1 + BOSWIN, n)):
        if (cl[j] > thr) if not bull else (cl[j] < thr): return None
        if (cl[j] > level) if bull else (cl[j] < level):
            end = float(max(hi[ob:j + 1])) if bull else float(min(lo[ob:j + 1]))
            return dict(dr=dr, origin=origin, origin_bar=ob, end=round(end, 2), bos_bar=j, ce=ce,
                        fvg=disp['fvg'], fvg_bar=disp['fvg_bar'], s=s, u=u)
        level = max(level, hi[j]) if bull else min(level, lo[j])
    return None


def bias_for(ctx, t):
    """HTF bias flag (v0). Returns (bias, premium/discount). Tag only, never filters."""
    dates, dayi, dD, dH, dL, cl = ctx.dates, ctx.dayi, ctx.dD, ctx.dH, ctx.dL, ctx.cl
    d = dates[t]; di = dayi[d]
    j = np.searchsorted(dD, d)
    if j < 5: return ('niejasny', '-')
    rngH = dH[j - 5:j].max(); rngL = dL[j - 5:j].min(); eq = (rngH + rngL) / 2
    px = cl[t]
    pd_ = 'discount' if px < eq else 'premium'
    up = dH[j - 1] > dH[j - 3] and dL[j - 1] > dL[j - 3]
    dn = dH[j - 1] < dH[j - 3] and dL[j - 1] < dL[j - 3]
    if pd_ == 'discount' and up: b = 'LONG'
    elif pd_ == 'premium' and dn: b = 'SHORT'
    elif pd_ == 'discount' and not dn: b = 'LONG?'
    elif pd_ == 'premium' and not up: b = 'SHORT?'
    else: b = 'niejasny'
    vu, vd = vi_draw(ctx, t)
    if b == 'niejasny':
        if vu and not vd: b = 'LONG?'
        elif vd and not vu: b = 'SHORT?'
    elif b == 'LONG?' and vu and not vd: b = 'LONG'
    elif b == 'SHORT?' and vd and not vu: b = 'SHORT'
    return (b, pd_)
