# detcore/catalysts.py
# Stage 2 (the part you change most): WHERE the levels are.
# build_levels() computes every catalyst source onto ctx:
#   fpfvg   - first NYAM FVG of the day (F.P.FVG)
#   day_hl  - PDH/PDL per day
#   gaplev  - NDOG/NWOG gap levels (+ collection time)
#   eqH/eqL - BSL/SSL equal highs/lows from H1
#   vis     - Volume Imbalance zones ; bigvi - the large ones used as magnets
# vi_draw() is the magnet lookup used by the bias flag (confirmation.bias_for).
#
# The ARMING/INVALIDATION logic (run_level / run_level_liq / run_gap) lives in scaffolding.py.
# Bodies below are verbatim from det_v11; only sources come from ctx.
import numpy as np
import pandas as pd
from collections import defaultdict


def _equals(sw, tol=4.):
    eq = []
    for i in range(len(sw)):
        for j in range(i + 1, len(sw)):
            if sw[j][1] - sw[i][1] > 86400: break
            if abs(sw[i][0] - sw[j][0]) <= tol:
                eq.append((round((sw[i][0] + sw[j][0]) / 2, 1), sw[j][1]))
    return eq


def build_levels(ctx):
    cfg = ctx.cfg
    df, ts = ctx.df, ctx.ts
    days, dates, n = ctx.days, ctx.dates, ctx.n
    o, hi, lo, cl, T, H = ctx.o, ctx.hi, ctx.lo, ctx.cl, ctx.T, ctx.H
    S, day_idx, day_last_idx = ctx.S, ctx.day_idx, ctx.day_last_idx

    # ---- F.P.FVG : first NYAM-session FVG of the day (v10, by TIME) ----
    nyam_by_day = defaultdict(list)
    for i in np.where(S == 'NYAM')[0]: nyam_by_day[dates[i]].append(int(i))
    fpfvg = {}
    for d in days:
        ix = nyam_by_day.get(d, [])
        for kk in range(2, len(ix)):
            k, k2 = ix[kk], ix[kk - 2]
            if lo[k] > hi[k2]: fpfvg[d] = (hi[k2], lo[k], k); break
            if hi[k] < lo[k2]: fpfvg[d] = (hi[k], lo[k2], k); break
    ctx.fpfvg = fpfvg

    # ---- PDH/PDL ----
    gday = df.groupby('date').agg(dh=('high', 'max'), dl=('low', 'min'))
    ctx.day_hl = {d: (float(gday.dh[d]), float(gday.dl[d])) for d in days}

    # ---- NDOG/NWOG : level = close, lifetime 2 / 5 days, collection time appended ----
    gaplev = []
    for d in days:
        di = day_idx[d]; Hd = H[di]
        bef = [di[k] for k in range(len(di)) if Hd[k] < 17]
        aft = [di[k] for k in range(len(di)) if Hd[k] >= 18]
        wd = pd.Timestamp(d).weekday()
        if bef and aft and wd < 4:
            c17 = cl[bef[-1]]; ta = T[aft[0]]; gaplev.append([c17, ta, 'NDOG', d, 2])
        if wd == 6:
            sun = aft; frd = [x for x in days if pd.Timestamp(x).weekday() == 4 and x < d]
            if sun and frd:
                fdi = day_idx[frd[-1]]; Hf = H[fdi]
                fri = [fdi[k] for k in range(len(fdi)) if Hf[k] < 17]
                if fri:
                    fc = cl[fri[-1]]; ta = T[sun[0]]; gaplev.append([fc, ta, 'NWOG', d, 5])
    for g in gaplev:
        pr, ta = g[0], g[1]; ct = float('inf')
        idxs = np.where((T > ta) & (lo <= pr) & (hi >= pr))[0]
        if len(idxs): ct = T[idxs[0]]
        g.append(ct)
    ctx.gaplev = gaplev

    # ---- BSL/SSL : equal highs/lows from H1 ----
    g1 = df.set_index(ts).resample('1h').agg(h=('high', 'max'), l=('low', 'min')).dropna()
    H1, L1 = g1.h.values, g1.l.values; G1 = (g1.index.astype('int64') // 10**9).values
    swh = [(H1[k], G1[k + 2]) for k in range(2, len(g1) - 2)
           if H1[k] >= max(H1[k - 1], H1[k - 2], H1[k + 1], H1[k + 2])]
    swl = [(L1[k], G1[k + 2]) for k in range(2, len(g1) - 2)
           if L1[k] <= min(L1[k - 1], L1[k - 2], L1[k + 1], L1[k + 2])]
    ctx.eqH = _equals(swh); ctx.eqL = _equals(swl)

    # ---- VOLUME IMBALANCE (body-to-body gap open[k] vs close[k-1]) ----
    VIMIN, VIBIG = cfg.vimin, cfg.vibig
    vis = []   # (lo, hi, bar, bull, mag)
    for k in range(1, n):
        g = o[k] - cl[k - 1]
        if g >= VIMIN and cl[k] > o[k]:
            vis.append((round(float(cl[k - 1]), 2), round(float(o[k]), 2), k, True, round(float(g), 1)))
        if -g >= VIMIN and cl[k] < o[k]:
            vis.append((round(float(o[k]), 2), round(float(cl[k - 1]), 2), k, False, round(float(-g), 1)))
    ctx.vis = vis
    bigvi = []   # big VI + fill bar (magnet valid until filled)
    for a, b, bar, bull, mag in vis:
        if mag < VIBIG: continue
        fillbar = n
        idx = np.where((lo[bar + 1:] <= b) & (hi[bar + 1:] >= a))[0]
        if len(idx): fillbar = bar + 1 + int(idx[0])
        bigvi.append((round((a + b) / 2, 2), bar, bull, mag, fillbar))
    ctx.bigvi = bigvi

    return ctx


def vi_draw(ctx, t):
    """Nearest unfilled big VI (within 2 days) -> magnet direction and level."""
    bigvi, cl, T = ctx.bigvi, ctx.cl, ctx.T
    up = dn = None
    for ce, bar, bull, mag, fillbar in bigvi:
        if not (bar < t < fillbar): continue
        if (T[t] - T[bar]) > 2 * 86400: continue
        if ce > cl[t] and (up is None or ce < up): up = ce
        if ce < cl[t] and (dn is None or ce > dn): dn = ce
    return up, dn
