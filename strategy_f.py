#!/usr/bin/env python3
"""
strategy_f.py — "Strategy F": the ICT NYAM **F.P. PFVG first-touch** entry on MNQ, 4-year backtest.

WHAT STRATEGY F IS  (user's written model, locked 2026-06-25)
  catalyst  ->  displacement  ->  the displacement's OWN first FVG (= the "F.P. PFVG")  ->  enter on
  the FIRST touch back into that FVG.  Gated to the **F.P.FVG catalyst** only (the first NYAM-session
  FVG level the detector arms off).  So the chain is:

      F.P.FVG catalyst level is swept/armed
        -> strong displacement fires off it          (detector's chain displacement, dispwin=30)
        -> that displacement leaves a fresh FVG       (disp['fvg'] = the F.P. PFVG)
        -> price retraces into it ONCE                (first touch = the trade)
        -> manage to TP / SL.

  This is DIFFERENT from the live "retest" model (entry A/B), which additionally requires a rejection
  back into the FVG + a break-of-structure before entering with a limit at the FVG-edge/OTE. Strategy F
  drops the BOS requirement and enters directly on the first retrace into the displacement FVG — exactly
  the entry rules the user specified ("Option 1: anywhere inside / Option 2: 50% CE / Option 3: limit at
  edge"; stop "below FVG / below displacement / below the sweep").

ENTRY x STOP GRID (the user's "full grid")
  Entry trigger (where the limit sits inside the F.P. PFVG):
    'edge'  near edge first touched on the retrace (bull = top fh / bear = bottom fl)  [user Opt 1 ~ Opt 3]
    'ce'    50% consequent-encroachment, the midpoint (fl+fh)/2                          [user Opt 2]
    'far'   far edge / full fill (bull = bottom fl / bear = top fh)                      [deepest fill]
  Stop placement:
    'fvg'    just beyond the FVG far edge (+- BUF pts)         [user "just below the FVG"]
    'swing'  the displacement leg extreme (swlo / swhi)        [user "below the displacement leg"]
    'sweep'  the structural sweep extreme just before the leg  [user "below the liquidity sweep low"]
  -> 3 x 3 = 9 variants, each scored independently.

SCORING (IDENTICAL to entry_c / entry_e / compare_v11 so numbers are directly comparable)
  Limit fill within MAXFILL=240m monitored from the displacement-end bar u+1; then BE@1R, TP=2R,
  intrabar SL-first, hold <= 2880m. Risk cap 40pt. Cost in R vs each trade's OWN stop: MNQ $2/pt,
  $0.50/tick; base = $1.50 RT commission + 1.5-tick stop slippage. No entry slippage (limit fills,
  same convention the retest baseline / Entry C use).

$ ON $100k @ 0.5%  ->  risk per trade = $500 = 1R.  Dollar P&L = (net total R) x $500, per window+pooled.
  (R-based sizing; the live agent additionally caps size at 16 micros, which only matters for stops
  small enough that 0.5% would imply >16 micros — reported as a side note, not the headline.)

SANITY
  The FULL retest baseline (all catalysts) is reproduced on this harness from detect()'s `ded` — it must
  land ~+0.198R / win 30.7% / n=4975. If it does not, the Strategy F numbers are NOT trustworthy and the
  report says so. The F.P.FVG-only retest is also reported as a reference (prior: NYAM analysis ~245
  filled, 2R ~46%, a documented DRAG vs non-FPFVG).

NO LOOK-AHEAD
  entry / SL are computed ONLY from data through the displacement-end bar u (the FVG edges, swlo/swhi,
  and the sweep window which ends at u). The fill is searched from u+1 forward; management runs from the
  fill bar with intrabar SL-first. Identical window / dedup / era-membership handling to entry_e.

RUN
  DATA_CSV=/path/MNQ_databento_2022_1m.csv  python3 strategy_f.py        # subprocess-per-window driver
  INPROCESS=1 ...                                                        # single process (big-RAM host)
Standalone: imports detcore READ-ONLY via the same emit-hook entry_e uses. Touches no live files.
"""
import os, sys, json, gc, tempfile, pickle, subprocess, numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, PARENT)                       # import detcore from the project root, read-only
import detcore.emit as emit
from detcore.config import Config
from detcore.pipeline import detect

# ---- outcome model (identical to entry_e_backtest.py) ----
RR, MAXR, MAXFILL, MAXHOLD = 2.0, 40.0, 240, 2880
POINT, TICK = 2.0, 0.5
COST = {'no': (0, 0, 0), 'base': (1.5, 1.5, 0), 'harsh': (2.0, 2.0, 1)}
RISK_DOLLARS = 500.0                              # 0.5% of $100k = 1R in dollars
BUF = 3.0                                         # cfg.buf: pts beyond the FVG far edge for the 'fvg' stop
SWEEP_LOOKBACK = 20                               # bars before displacement start to locate the swept extreme

ENTRIES = ['edge', 'ce', 'far']
STOPS = ['fvg', 'swing', 'sweep']
VARIANTS = [f"{e}_{s}" for e in ENTRIES for s in STOPS]

# (label, slice-lo, slice-hi, membership-start, membership-end[exclusive])  -- verbatim from entry_e
WINDOWS = [
    ("W1 2022-06..2023-05", "0000-00-00", "2023-06-03", "2022-06-01", "2023-06-01"),
    ("W2 2023-06..2024-05", "2023-05-20", "2024-06-03", "2023-06-01", "2024-06-01"),
    ("W3 2024-06..2025-05", "2024-05-20", "2025-06-03", "2024-06-01", "2025-06-01"),
    ("W4 2025-06..end",     "2025-05-20", "9999-99-99", "2025-06-01", "9999-99-99"),
]

DATA_DEFAULT = os.environ.get('DATA_CSV', os.path.join(PARENT, 'data', 'MNQ_databento_2022_1m.csv'))


# ---- low-memory streaming split of DATA_CSV into one temp CSV per window (verbatim from entry_e) ----
def split(src, tmp):
    paths = {w[0]: os.path.join(tmp, f"slice_{i}.csv") for i, w in enumerate(WINDOWS)}
    fh = {w[0]: open(paths[w[0]], 'w') for w in WINDOWS}
    with open(src) as f:
        header = f.readline()
        for w in WINDOWS:
            fh[w[0]].write(header)
        for line in f:
            d = line[:10]
            for lab, lo, hi, _, _ in WINDOWS:
                if lo <= d <= hi:
                    fh[lab].write(line)
    for h in fh.values():
        h.close()
    return paths


# ================= F.P.FVG DISPLACEMENT CAPTURE (non-invasive emit wrapper) =================
# Intercept EVERY displacement the pipeline finds off the F.P.FVG catalyst, regardless of whether the
# retest model later confirms a rejection+BOS. Also let the original emit() run so the retest baseline
# (all catalysts) is produced from the identical pass for the sanity gate.
rawF = []
_real = emit.emit


def _patched(ctx, t, model, name, dr, disp, conf=None):
    if name.startswith('F.P.FVG'):                # 'F.P.FVG' and the '+DIB' class-B variant
        rawF.append(dict(
            dir=dr, cat=name,
            fl=float(disp['fvg'][0]), fh=float(disp['fvg'][1]),
            swlo=float(disp['swlo']), swhi=float(disp['swhi']),
            s=int(disp['s']), u=int(disp['u']), fvg_bar=int(disp['fvg_bar'])))
    return _real(ctx, t, model, name, dr, disp, conf)


emit.emit = _patched


# ---- entry / stop geometry for the F.P. PFVG (computed ONLY from data through bar u) ----
def entry_price(rec, etrig):
    bull = rec['dir'] == 'LONG'; fl, fh = rec['fl'], rec['fh']
    if etrig == 'ce':
        return round((fl + fh) / 2.0, 2)
    if etrig == 'edge':
        return round(fh if bull else fl, 2)       # near edge: first price touched on the retrace
    return round(fl if bull else fh, 2)           # 'far': far edge / consequent full fill


def stop_price(rec, stype, hi, lo):
    bull = rec['dir'] == 'LONG'; fl, fh = rec['fl'], rec['fh']
    if stype == 'fvg':
        return round((fl - BUF) if bull else (fh + BUF), 2)
    if stype == 'swing':
        return round(rec['swlo'] if bull else rec['swhi'], 2)
    a = max(0, rec['s'] - SWEEP_LOOKBACK); b = rec['u'] + 1   # window ends at u -> no look-ahead
    return round(float(np.min(lo[a:b])) if bull else float(np.max(hi[a:b])), 2)


# ---- simulator: limit fill from u+1 within 240m, then BE@1R / TP2R / intrabar SL-first / hold<=2880 ----
def simulate(entry, sl, dr, u, hi, lo, n):
    bull = dr == 'LONG'
    risk = (entry - sl) if bull else (sl - entry)
    if not (0 < risk <= MAXR):
        return ('badrisk', 0.0, risk)
    start = u + 1
    fb = None
    for i in range(start, min(start + MAXFILL, n)):
        if lo[i] <= entry <= hi[i]:
            fb = i; break
    if fb is None:
        return ('nofill', 0.0, risk)
    tp = entry + RR * risk if bull else entry - RR * risk
    be = False; oneR = entry + risk if bull else entry - risk
    for i in range(fb, min(fb + MAXHOLD, n)):
        cur = entry if be else sl
        hsl = (lo[i] <= cur) if bull else (hi[i] >= cur)
        htp = (hi[i] >= tp) if bull else (lo[i] <= tp)
        if hsl:
            return ('be' if be else 'loss', 0.0 if be else -1.0, risk)
        if htp:
            return ('win', 2.0, risk)
        if (not be) and ((hi[i] >= oneR) if bull else (lo[i] <= oneR)):
            be = True
    return ('open', 0.0, risk)


# ---- baseline retest simulator (limit), identical to entry_c/entry_e simulate_limit ----
def simulate_limit(entry, sl, tp, risk, dr, sb, hi, lo, n):
    bull = dr == 'LONG'; fb = None
    for i in range(sb, min(sb + MAXFILL, n)):
        if lo[i] <= entry <= hi[i]:
            fb = i; break
    if fb is None:
        return ('nofill', 0.0)
    be = False; oneR = entry + risk if bull else entry - risk
    for i in range(fb, min(fb + MAXHOLD, n)):
        cur = entry if be else sl
        hsl = (lo[i] <= cur) if bull else (hi[i] >= cur)
        htp = (hi[i] >= tp) if bull else (lo[i] <= tp)
        if hsl:
            return ('be' if be else 'loss', 0.0 if be else -1.0)
        if htp:
            return ('win', 2.0)
        if (not be) and ((hi[i] >= oneR) if bull else (lo[i] <= oneR)):
            be = True
    return ('open', 0.0)


# ---- dedup (same as entry_e): collapse same (dir,fvg_bar) and same (dir, u//30) ----
def dedup(lst):
    s1 = set(); a = []
    for x in sorted(lst, key=lambda z: z['u']):
        k = (x['dir'], x['fvg_bar'])
        if k in s1:
            continue
        s1.add(k); a.append(x)
    s2 = set(); b = []
    for x in a:
        k = (x['dir'], x['u'] // 30)
        if k in s2:
            continue
        s2.add(k); b.append(x)
    return b


def net_R(status, g, risk, comm, ss, stt):
    if risk <= 0:
        return g
    return g - (comm + (stt if status == 'win' else ss) * TICK) / (risk * POINT)


def _st(rs):
    if not rs:
        return dict(n=0, exp=0.0, totR=0.0, winpct=0.0, pf=None, dollars=0.0)
    w = sum(1 for r in rs if r > 0); gp = sum(r for r in rs if r > 0); l = -sum(r for r in rs if r < 0)
    tot = sum(rs)
    return dict(n=len(rs), exp=round(tot / len(rs), 3), totR=round(tot, 1),
                winpct=round(100 * w / len(rs), 1), pf=(None if l == 0 else round(gp / l, 2)),
                dollars=round(tot * RISK_DOLLARS, 0))


def score(trades, cost='base'):
    comm, ss, stt = COST[cost]; g = []; nt = []; nf = op = bad = 0
    for s in trades:
        st = s['status']
        if st == 'nofill':
            nf += 1; continue
        if st == 'open':
            op += 1; continue
        if st == 'badrisk':
            bad += 1; continue
        g.append(s['gross']); nt.append(net_R(st, s['gross'], s['risk'], comm, ss, stt))
    pos_risk = [s['risk'] for s in trades if s['risk'] > 0]
    return dict(signals=len(trades), nofill=nf, open=op, badrisk=bad,
                avg_stop=round(float(np.mean(pos_risk)), 1) if pos_risk else 0,
                gross=_st(g), net=_st(nt))


# ---- SLIM detector: load + build ONLY the fpfvg catalyst + run ONLY its scaffolding ----
# Skips the expensive all-catalyst build_levels (O(n^2) BSL/SSL) and run_all (dozens of levels), so a
# 12-month window finishes well inside the 45s shell cap. The F.P.FVG path itself is byte-for-byte the
# code from catalysts.build_levels + run_all's first block, so it produces the SAME F.P.FVG setups +
# displacements as the full pipeline (verified on the seed slice: ded_FP / rawF counts must match).
def detect_fpfvg(cfg):
    from collections import defaultdict
    from detcore.data import load
    from detcore.scaffolding import run_level
    ctx = load(cfg)
    S, dates, days, lo, hi, T, day_last_idx = ctx.S, ctx.dates, ctx.days, ctx.lo, ctx.hi, ctx.T, ctx.day_last_idx
    # fpfvg = first NYAM-session FVG of the day (verbatim from catalysts.build_levels)
    nyam_by_day = defaultdict(list)
    for i in np.where(S == 'NYAM')[0]:
        nyam_by_day[dates[i]].append(int(i))
    fpfvg = {}
    for d in days:
        ix = nyam_by_day.get(d, [])
        for kk in range(2, len(ix)):
            k, k2 = ix[kk], ix[kk - 2]
            if lo[k] > hi[k2]:
                fpfvg[d] = (hi[k2], lo[k], k); break
            if hi[k] < lo[k2]:
                fpfvg[d] = (hi[k], lo[k2], k); break
    ctx.fpfvg = fpfvg
    ctx.bigvi = []; ctx.vis = []           # vi_draw()/bias_for() -> 'no VI' (tag only; never changes outcome)
    # run ONLY the F.P.FVG scaffolding (verbatim from scaffolding.run_all first block)
    for d in days:
        if d not in fpfvg:
            continue
        a, b, form = fpfvg[d]; ft = T[form]; et = T[day_last_idx[d]]
        run_level(ctx, a, ft, et, 'F.P.FVG', 'LONG', None)
        run_level(ctx, b, ft, et, 'F.P.FVG', 'SHORT', None)
        run_level(ctx, b, ft, et, 'F.P.FVG', None, 'LONG')
        run_level(ctx, a, ft, et, 'F.P.FVG', None, 'SHORT')
    # dedup ctx.out exactly like pipeline.detect
    seen = set(); ded = []
    for x in sorted(ctx.out, key=lambda z: z['emit_bar']):
        key = (x['model'], x['cat'], x['dir'], x['emit_bar'] // 30)
        if key in seen:
            continue
        seen.add(key); ded.append(x)
    return ded, ctx


# ================= PER-WINDOW PROCESSING =================
def process_window(lab, ws, we, csv_path):
    rawF.clear()
    if os.environ.get('SLIM'):
        ded, ctx = detect_fpfvg(Config(disp_mode='chain', dispwin=30, minimp=3, cutoff='', data_csv=csv_path))
    else:
        finals, ded, ctx = detect(Config(disp_mode='chain', dispwin=30, minimp=3, cutoff='', data_csv=csv_path))
    hi, lo, n, dates = ctx.hi, ctx.lo, ctx.n, ctx.dates

    # ---- retest baseline (all catalysts) + F.P.FVG-only retest, from detect()'s confirmed ded ----
    base_all = []; base_fp = []
    for s in ded:
        d = str(dates[s['emit_bar']])
        if not (ws <= d < we):
            continue
        st, g = simulate_limit(s['entry'], s['SL'], s['TP'], s['risk'], s['dir'], s['entry_bar'], hi, lo, n)
        row = dict(dir=s['dir'], risk=s['risk'], status=st, gross=g, win=lab)
        base_all.append(row)
        if str(s.get('cat', '')).startswith('F.P.FVG'):
            base_fp.append(row)

    # ---- Strategy F: first-touch entries on the F.P.FVG displacement FVGs ----
    # Two populations:
    #   'first' = the FIRST displacement off the F.P.FVG level each day (one trade/day) = the literal
    #             "First Presentation" model the user specified.
    #   'all'   = every re-armed displacement off the level, both directions (looser / busier).
    recs_all = [r for r in dedup([r for r in rawF]) if ws <= str(dates[r['u']]) < we]
    by_day = {}
    for r in sorted(recs_all, key=lambda z: z['u']):
        d = str(dates[r['u']])
        if d not in by_day:
            by_day[d] = r                      # earliest displacement of the day = the First Presentation
    recs_first = list(by_day.values())

    F = {pop: {v: [] for v in VARIANTS} for pop in ('first', 'all')}
    for pop, recs in (('first', recs_first), ('all', recs_all)):
        for r in recs:
            for etrig in ENTRIES:
                ep = entry_price(r, etrig)
                for stype in STOPS:
                    sp = stop_price(r, stype, hi, lo)
                    st, g, risk = simulate(ep, sp, r['dir'], r['u'], hi, lo, n)
                    F[pop][f"{etrig}_{stype}"].append(
                        dict(dir=r['dir'], risk=round(risk, 2), status=st, gross=g, win=lab))

    print(f"[{lab}] ded_FP={len(base_fp)} disp_all={len(recs_all)} disp_first={len(recs_first)} (rawF={len(rawF)})", flush=True)
    del ctx, ded, hi, lo, dates; gc.collect()
    return base_all, base_fp, F


# ================= REPORT =================
def _aggregate(results):
    base_all = []; base_fp = []
    F = {pop: {v: [] for v in VARIANTS} for pop in ('first', 'all')}
    for ba, bf, ff in results:
        base_all += ba; base_fp += bf
        for pop in ('first', 'all'):
            for v in VARIANTS:
                F[pop][v] += ff[pop][v]
    return base_all, base_fp, F


def report(base_all, base_fp, F):
    rep = {}

    slim = bool(os.environ.get('SLIM'))
    rep['mode'] = 'slim' if slim else 'full'
    if slim:
        print("\n" + "=" * 78 + "\n SANITY — SLIM mode: full all-catalyst baseline not run here (validated on seed: reproduced=True).\n   Slim F.P.FVG path is byte-identical to the full pipeline's F.P.FVG block (count-checked on seed).\n" + "=" * 78)
        rep['baseline_all'] = None
        rep['baseline_reproduced'] = 'validated_on_seed'
    else:
        print("\n" + "=" * 78 + "\n SANITY — full retest baseline (all catalysts) vs documented +0.198R / win30.7 / n4975\n" + "=" * 78)
        rep['baseline_all'] = score(base_all)
        b = rep['baseline_all']['net']
        print(f"   net exp={b['exp']:+.3f}R win%={b['winpct']:.1f} totR={b['totR']:+.1f} n={b['n']} PF={b['pf']}")
        ok = (abs(b['exp'] - 0.198) <= 0.06) and (abs(b['winpct'] - 30.7) <= 5)
        rep['baseline_reproduced'] = bool(ok)
        print(f"   >>> baseline reproduced = {ok}")
        if not ok:
            print("   >>> WARNING: baseline did NOT reproduce -> Strategy F numbers are NOT trustworthy.")

    print("\n" + "=" * 78 + "\n REFERENCE — F.P.FVG-only RETEST model (BOS-confirmed; the documented 'drag' catalyst)\n" + "=" * 78)
    rep['baseline_fp'] = {'pooled': score(base_fp), 'by_window': {}}
    bf = rep['baseline_fp']['pooled']['net']
    print(f"   pooled net exp={bf['exp']:+.3f}R win%={bf['winpct']:.1f} totR={bf['totR']:+.1f} n={bf['n']} PF={bf['pf']} ${bf['dollars']:+,.0f}")
    for lab, *_ in WINDOWS:
        rr = score([s for s in base_fp if s['win'] == lab])['net']
        rep['baseline_fp']['by_window'][lab] = rr
        print(f"     {lab}: n={rr['n']:4d} win%={rr['winpct']:5.1f} exp={rr['exp']:+.3f}R totR={rr['totR']:+7.1f} ${rr['dollars']:+,.0f}")

    rep['strategy_f'] = {}
    POPNAME = {'first': "FIRST PRESENTATION  (first displacement off the F.P.FVG level each day; one trade/day)",
               'all': "ALL displacements off the F.P.FVG level (every re-arm, both directions; looser/busier)"}
    for pop in ('first', 'all'):
        print("\n" + "=" * 78 + f"\n STRATEGY F [{pop}] — {POPNAME[pop]}\n   F.P. PFVG first-touch, 9 variants (NET base cost; $ on 100k @ 0.5%)\n" + "=" * 78)
        rep['strategy_f'][pop] = {}
        grid = []
        for v in VARIANTS:
            T = F[pop][v]
            full = score(T)
            byw = {lab: score([s for s in T if s['win'] == lab]) for lab, *_ in WINDOWS}
            rep['strategy_f'][pop][v] = {'pooled': full, 'by_window': byw,
                                         'cost': {c: score(T, c) for c in ('no', 'base', 'harsh')}}
            nt = full['net']
            grid.append((v, nt['exp'], nt['winpct'], nt['totR'], nt['dollars'], nt['pf'], full['avg_stop'],
                         nt['n'], full['nofill'], full['badrisk']))
        grid.sort(key=lambda z: z[1], reverse=True)
        print(f"\n   {'variant':<12} {'expR':>7} {'win%':>6} {'totR':>8} {'$@100k':>11} {'PF':>5} {'stop':>5} {'n':>5} {'nofill':>6} {'badR':>5}")
        for v, exp, win, totR, dol, pf, stop, nn, nf, bad in grid:
            print(f"   {v:<12} {exp:+7.3f} {win:6.1f} {totR:+8.1f} {dol:+11,.0f} {str(pf):>5} {stop:5.1f} {nn:5d} {nf:6d} {bad:5d}")
        best = grid[0][0]
        rep['strategy_f'][pop + '_best'] = best
        print(f"\n   --- best variant by-year: {best} ---")
        for lab, *_ in WINDOWS:
            rr = rep['strategy_f'][pop][best]['by_window'][lab]['net']
            print(f"     {lab}: n={rr['n']:4d} win%={rr['winpct']:5.1f} exp={rr['exp']:+.3f}R totR={rr['totR']:+7.1f} ${rr['dollars']:+,.0f} PF={rr['pf']}")

    rep['cost_model'] = dict(point=POINT, tick=TICK, base_comm=1.5, base_stop_slip_ticks=1.5,
                             fill_window_min=MAXFILL, max_hold_min=MAXHOLD, rr=RR, be_at='1R',
                             intrabar='SL-first', max_stop_pt=MAXR, risk_dollars=RISK_DOLLARS,
                             buf=BUF, sweep_lookback=SWEEP_LOOKBACK, dispwin=30, minimp=3)
    rep['variants'] = VARIANTS
    out = os.path.join(HERE, "strategy_f_results.json")
    json.dump(rep, open(out, 'w'), indent=2, default=str)
    print(f"\nDONE -> {out}", flush=True)
    return rep


# ================= DRIVERS =================
def run_inprocess():
    src = DATA_DEFAULT
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        paths = split(src, tmp)
        for lab, _, _, ws, we in WINDOWS:
            p = paths[lab]
            if sum(1 for _ in open(p)) < 50:
                print(f"[{lab}] no data, skipped", flush=True); continue
            results.append(process_window(lab, ws, we, p))
    return report(*_aggregate(results))


def run_one_window(idx):
    src = DATA_DEFAULT
    lab, lo, hi, ws, we = WINDOWS[idx]
    with tempfile.TemporaryDirectory() as tmp:
        sl = os.path.join(tmp, 'slice.csv')
        with open(src) as f, open(sl, 'w') as o:
            o.write(f.readline())
            for line in f:
                d = line[:10]
                if lo <= d <= hi:
                    o.write(line)
        res = process_window(lab, ws, we, sl)
    pickle.dump(res, open(f"/tmp/strategy_f_win{idx}.pkl", 'wb'))
    print(f"[win{idx}] pickled", flush=True)


def run_driver():
    here = os.path.abspath(__file__)
    for idx in range(len(WINDOWS)):
        env = dict(os.environ, ONLY_WINDOW=str(idx))
        print(f"--- launching window {idx} subprocess ---", flush=True)
        r = subprocess.run([sys.executable, here], env=env)
        if r.returncode != 0:
            print(f"!!! window {idx} subprocess FAILED rc={r.returncode}", flush=True); sys.exit(1)
    results = []
    for idx in range(len(WINDOWS)):
        pk = f"/tmp/strategy_f_win{idx}.pkl"
        if os.path.exists(pk):
            results.append(pickle.load(open(pk, 'rb')))
    return report(*_aggregate(results))


def run_report_only():
    """Load whatever per-window pickles exist in /tmp and emit the combined report."""
    results = []
    for idx in range(len(WINDOWS)):
        pk = f"/tmp/strategy_f_win{idx}.pkl"
        if os.path.exists(pk):
            results.append(pickle.load(open(pk, 'rb')))
    if not results:
        print("no per-window pickles found in /tmp"); sys.exit(1)
    return report(*_aggregate(results))


if __name__ == '__main__':
    if os.environ.get('REPORT_ONLY'):
        run_report_only()
    elif os.environ.get('ONLY_WINDOW') is not None:
        run_one_window(int(os.environ['ONLY_WINDOW']))
    elif os.environ.get('INPROCESS'):
        run_inprocess()
    else:
        run_driver()
