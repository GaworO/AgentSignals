# detcore/scaffolding.py
# Stage 2b: HOW a level arms, fires and dies (the v10-time vs v11-sweep invalidation).
#   run_level     -> v10 TIME-based re-arm (used only by F.P.FVG)
#   run_level_liq -> v11: level dies by SWEEP ('sweep') or after first setup ('confirm'); CAP_DAYS backstop
#   run_gap       -> v10 GAP zone: every tap re-arms (NDOG/NWOG, VI)
#   run_all       -> the driver: walks F.P.FVG, session H/L, PDH/PDL, PWH/PWL, NDOG/NWOG, BSL/SSL, VI
# Bodies verbatim from det_v11; ctx.cur_break replaces the old `global _cur_break`.
import numpy as np
from collections import OrderedDict

from .emit import try_chain


def dayidx_for(ctx, epoch):
    T, n = ctx.T, ctx.n
    i = int(np.searchsorted(T, epoch)); return min(max(i, 0), n - 1)


def _cap_a1(ctx, form_t):
    days, dayi, dates, day_last_idx, n, CAP_DAYS = (
        ctx.days, ctx.dayi, ctx.dates, ctx.day_last_idx, ctx.n, ctx.cfg.cap_days)
    a0 = dayidx_for(ctx, form_t)
    cap_date = days[min(dayi[dates[a0]] + CAP_DAYS, len(days) - 1)]
    return a0, min(day_last_idx[cap_date] + 1, n)


def _armed_hit_positions(hit_pos, rearm_pos):
    # hit and rearm are mutually exclusive on a bar -> simple two-pointer merge
    res = []; armed = True; hp = hit_pos.tolist(); rp = rearm_pos.tolist()
    ih = ir = 0; Hn = len(hp); Rn = len(rp)
    while ih < Hn or ir < Rn:
        nh = hp[ih] if ih < Hn else float('inf')
        nr = rp[ir] if ir < Rn else float('inf')
        if nh < nr:
            if armed: res.append(nh); armed = False
            ih += 1
        else:
            if not armed: armed = True
            ir += 1
    return res


def run_level(ctx, level, form_t, end_t, name, rev_dir, cont_dir):
    """v10 (TIME): re-arm after a BUF pullback; window to end_t. Used ONLY for F.P.FVG."""
    T, n, lo, hi, cl, BUF = ctx.T, ctx.n, ctx.lo, ctx.hi, ctx.cl, ctx.cfg.buf
    a0 = dayidx_for(ctx, form_t); a1 = min(dayidx_for(ctx, end_t) + 1, n)
    win = [i for i in range(a0, a1) if T[i] > form_t]
    if not win: return
    if rev_dir:
        bull = rev_dir == 'LONG'; armed = True; k = 0
        for i in win:
            hit = (lo[i] <= level) if bull else (hi[i] >= level)
            if armed and hit:
                k += 1; ctx.cur_break = k; try_chain(ctx, i, rev_dir, 'Reversal', name); armed = False
            elif (not armed) and ((lo[i] > level + BUF) if bull else (hi[i] < level - BUF)):
                armed = True
    if cont_dir:
        bull = cont_dir == 'LONG'; armed = True; k = 0
        for i in win:
            hit = (cl[i] > level) if bull else (cl[i] < level)
            if armed and hit:
                k += 1; ctx.cur_break = k; try_chain(ctx, i, cont_dir, 'Cont', name); armed = False
            elif (not armed) and ((cl[i] < level - BUF) if bull else (cl[i] > level + BUF)):
                armed = True


def run_level_liq(ctx, level, form_t, name, rev_dir, cont_dir, intraday=False):
    """v11: high -> rev SHORT / cont LONG ; low -> rev LONG / cont SHORT.
       MODE='sweep'   -> level dies on the 1st breach (wick/body).
       MODE='confirm' -> v10 multi-break (re-arm after BUF), dies after the 1st emission.
       Both: CAP_DAYS backstop if never collected."""
    T, hi, lo, cl, MODE, BUF, out = ctx.T, ctx.hi, ctx.lo, ctx.cl, ctx.cfg.mode, ctx.cfg.buf, ctx.out
    a0, a1 = _cap_a1(ctx, form_t)
    if a1 <= a0: return
    base = np.arange(a0, a1); win = base[T[a0:a1] > form_t]
    if win.size == 0: return
    is_high = (rev_dir == 'SHORT')
    hiw, low_, clw = hi[win], lo[win], cl[win]

    if MODE == 'sweep':
        through = (hiw >= level) if is_high else (low_ <= level)
        if not through.any(): return
        i = int(win[int(np.argmax(through))])
        ctx.cur_break = 1
        if rev_dir: try_chain(ctx, i, rev_dir, 'Reversal', name)
        if cont_dir:
            closed = (cl[i] > level) if is_high else (cl[i] < level)
            if closed: try_chain(ctx, i, cont_dir, 'Cont', name)
        return

    # CONFIRM
    events = []
    if rev_dir:
        rev_hit = np.flatnonzero((hiw >= level) if is_high else (low_ <= level))
        rev_re = np.flatnonzero((hiw < level - BUF) if is_high else (low_ > level + BUF))
        for q in _armed_hit_positions(rev_hit, rev_re): events.append((q, 'R'))
    if cont_dir:
        cont_hit = np.flatnonzero((clw > level) if is_high else (clw < level))
        cont_re = np.flatnonzero((clw < level - BUF) if is_high else (clw > level + BUF))
        for q in _armed_hit_positions(cont_hit, cont_re): events.append((q, 'C'))
    events.sort()
    if ctx.cfg.eod_intraday and intraday and events:   # v12: tapped-but-unconfirmed session level dies EOD
        ft = int(win[events[0][0]]); Hh_ = ctx.H        # first tap bar
        di0 = ctx.dayi[ctx.dates[ft]]; extra = 1 if Hh_[ft] >= 18 else 0   # evening tap -> roll to next day
        exp = ctx.day_last_idx[ctx.days[min(di0 + extra, len(ctx.days) - 1)]]
        events = [(q, t) for (q, t) in events if int(win[q]) <= exp]
    k = 0
    for q, typ in events:
        k += 1; ctx.cur_break = k; i = int(win[q]); before = len(out)
        if typ == 'R': try_chain(ctx, i, rev_dir, 'Reversal', name)
        else:          try_chain(ctx, i, cont_dir, 'Cont', name)
        if len(out) > before: return   # dies after the first emission


def run_gap(ctx, zlo, zhi, form_t, end_t, name):
    """v10 (GAP, TIME): EVERY tap of the zone (re-arm after leaving). NDOG/NWOG/VI."""
    T, n, lo, hi, cl = ctx.T, ctx.n, ctx.lo, ctx.hi, ctx.cl
    a0 = dayidx_for(ctx, form_t); a1 = min(dayidx_for(ctx, end_t) + 1, n)
    win = [i for i in range(a0, a1) if T[i] > form_t]
    if not win: return
    mid = (zlo + zhi) / 2; armed = False; k = 0
    for i in win:
        inzone = lo[i] <= zhi and hi[i] >= zlo
        if armed and inzone:
            k += 1; ctx.cur_break = k
            from_below = cl[i - 1] < mid
            if from_below:
                try_chain(ctx, i, 'SHORT', 'Reversal', name); try_chain(ctx, i, 'LONG', 'Cont', name)
            else:
                try_chain(ctx, i, 'LONG', 'Reversal', name); try_chain(ctx, i, 'SHORT', 'Cont', name)
            armed = False
        elif not inzone:
            armed = True


def run_all(ctx):
    """Walk every catalyst and emit setups into ctx.out. Order is identical to det_v11."""
    days, T, day_last_idx, day_first_idx = ctx.days, ctx.T, ctx.day_last_idx, ctx.day_first_idx
    dayi, sessinst, day_hl = ctx.dayi, ctx.sessinst, ctx.day_hl
    fpfvg, gaplev, eqH, eqL, vis, dates = ctx.fpfvg, ctx.gaplev, ctx.eqH, ctx.eqL, ctx.vis, ctx.dates
    df, hi, lo = ctx.df, ctx.hi, ctx.lo

    # ---- F.P.FVG (zone, v10/TIME): reversal both ways + cont both ways ----
    for d in days:
        if d not in fpfvg: continue
        a, b, form = fpfvg[d]; ft = T[form]; et = T[day_last_idx[d]]
        run_level(ctx, a, ft, et, 'F.P.FVG', 'LONG', None)
        run_level(ctx, b, ft, et, 'F.P.FVG', 'SHORT', None)
        run_level(ctx, b, ft, et, 'F.P.FVG', None, 'LONG')
        run_level(ctx, a, ft, et, 'F.P.FVG', None, 'SHORT')

    # ---- session H/L (v11/SWEEP): low->rev LONG/cont SHORT ; high->rev SHORT/cont LONG ----
    SH = {'ASIA': ('AH', 'AL'), 'LO': ('LH', 'LL'), 'NYAM': ('NYAMH', 'NYAML'),
          'NYL': ('NYLH', 'NYLL'), 'NYPM': ('NYPMH', 'NYPML')}
    for sname, s0, eidx, Hh, Ll in sessinst:
        if sname not in SH: continue
        hn, ln = SH[sname]; ft = T[eidx]
        run_level_liq(ctx, Hh, ft, hn, 'SHORT', 'LONG', intraday=True)   # session high
        run_level_liq(ctx, Ll, ft, ln, 'LONG', 'SHORT', intraday=True)   # session low

    # ---- PDH/PDL (v11/SWEEP) ----
    for _di in range(1, len(days)):
        d = days[_di]; pdh, pdl = day_hl[days[_di - 1]]
        if d not in day_first_idx: continue
        ft = int(T[day_first_idx[d]]) - 1
        run_level_liq(ctx, pdh, ft, 'PDH', 'SHORT', 'LONG')
        run_level_liq(ctx, pdl, ft, 'PDL', 'LONG', 'SHORT')

    # ---- PWH/PWL (v11/SWEEP, previous ISO week) ----
    _iso = df.dt.dt.isocalendar()
    _wk = list(zip(_iso.year.values, _iso.week.values))
    _weeks = OrderedDict()
    for _i, _k in enumerate(_wk):
        if _k not in _weeks: _weeks[_k] = [_i, _i, float(hi[_i]), float(lo[_i])]
        else:
            _w = _weeks[_k]; _w[1] = _i
            if hi[_i] > _w[2]: _w[2] = float(hi[_i])
            if lo[_i] < _w[3]: _w[3] = float(lo[_i])
    _wkeys = list(_weeks.keys())
    for _wi in range(1, len(_wkeys)):
        pwh = _weeks[_wkeys[_wi - 1]][2]; pwl = _weeks[_wkeys[_wi - 1]][3]
        _s0 = _weeks[_wkeys[_wi]][0]
        ft = int(T[_s0]) - 1
        run_level_liq(ctx, pwh, ft, 'PWH', 'SHORT', 'LONG')
        run_level_liq(ctx, pwl, ft, 'PWL', 'LONG', 'SHORT')

    # ---- NDOG/NWOG (v10/GAP): trigger = return to the level ----
    for pr, ta, nm, fd, md, ct in gaplev:
        et = T[day_last_idx[days[min(dayi[fd] + md, len(days) - 1)]]]
        et = min(et, ct if ct != float('inf') else et)
        run_gap(ctx, pr, pr, ta, et, nm)

    # ---- BSL/SSL H1 (v11/SWEEP) ----
    for P, t0 in eqH:
        run_level_liq(ctx, P, t0, 'BSL H1', 'SHORT', 'LONG')
    for P, t0 in eqL:
        run_level_liq(ctx, P, t0, 'SSL H1', 'LONG', 'SHORT')

    # ---- VOLUME IMBALANCE (v10/GAP, 2 days): trigger = return to the VI zone ----
    for a, b, bar, bull, mag in vis:
        et = T[day_last_idx[days[min(dayi[dates[bar]] + 2, len(days) - 1)]]]
        run_gap(ctx, a, b, T[bar], et, 'VI')

    return ctx
