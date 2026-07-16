# magnet.py — FVG-magnet quality tag + size multiplier (ISOLATED add-on, same pattern as select_tag.py)
#
# WHAT: a REVERSAL whose target reaches into an unfilled OPPOSING FVG (the "draw/magnet") is a
#       validated higher-quality cohort. 4yr MNQ, forward-tested (train 2022-24 -> hold-out 2025-26):
#         reversal + into FVG ........... +0.44R -> +0.38R OOS  (~62/yr)   -> MAGNET   (size x1.25)
#         + last-5 candidates opposite .. +0.55R -> +0.64R OOS  (~35/yr)   -> PREMIUM  (size x1.50)
#       vs baseline +0.28R. Continuation does NOT qualify. F.P.FVG excluded. Keeps the 2R target.
#
# READ-ONLY: this only TAGS a setup and SUGGESTS a size multiple. It NEVER changes entry / SL / TP /
#            direction / whether a trade fires. Safe to wire in and out. Fails closed (returns
#            magnet=False) on any problem. Wiring at the bottom.
import bisect

MINFVG            = 3.0     # min gap size (points) to count an FVG
LOOKBACK          = 120     # bars back to find a recent unfilled opposing FVG
PREV5_OPP_PREMIUM = 3       # >= this many of the last 5 candidates opposite => PREMIUM tier
SIZE_MAGNET       = 1.25    # size multiple for the broad tier   (set = SIZE_PREMIUM to size the whole 62/yr uniformly)
SIZE_PREMIUM      = 1.50    # size multiple for the counter-trend tier
EXCLUDE_CATS      = ('F.P.FVG',)   # this catalyst's magnet is dead (+0.06R) — don't upsize it


def load_buffer(buf_path):
    """Return (highs, lows, ts_ms) arrays from the live buffer CSV, or None on any failure."""
    try:
        import pandas as pd
        df = pd.read_csv(buf_path)
        hcol = 'high' if 'high' in df.columns else ('hi' if 'hi' in df.columns else None)
        lcol = 'low'  if 'low'  in df.columns else ('lo' if 'lo' in df.columns else None)
        tcol = 'ts_event' if 'ts_event' in df.columns else ('ts' if 'ts' in df.columns else None)
        if not (hcol and lcol and tcol): return None
        hi = df[hcol].astype(float).values
        lo = df[lcol].astype(float).values
        ts = (pd.to_datetime(df[tcol], utc=True).astype('int64').values // 10**6)
        return hi, lo, ts
    except Exception:
        return None


def _bar_index(rep, ts):
    """Index of the setup's entry bar in the buffer: prefer entry_bar, else align by timestamp."""
    eb = rep.get('entry_bar')
    try:
        if eb is not None and 0 < int(eb) < len(ts): return int(eb)
    except Exception:
        pass
    for key in ('entry_ms', 'bos_ms', 'trig_ms'):
        v = rep.get(key)
        if v:
            i = bisect.bisect_right(ts, int(v)) - 1
            if 0 < i < len(ts): return i
    return None


def _nearest_unfilled_opposing_fvg(hi, lo, sb, entry, is_long):
    """Nearest unfilled OPPOSING FVG ahead of entry, formed within LOOKBACK bars.
       long -> bearish gap ABOVE (lo[i] > hi[i+2]); short -> bullish gap BELOW (hi[i] < lo[i+2])."""
    best = None
    for i in range(max(0, sb - LOOKBACK), sb - 1):
        if i + 2 > sb: break
        if is_long:
            if lo[i] > hi[i + 2] and (lo[i] - hi[i + 2]) >= MINFVG:
                flo, fhi = hi[i + 2], lo[i]
                if flo > entry:
                    mid = (flo + fhi) / 2.0
                    if not any(hi[j] >= mid for j in range(i + 3, sb + 1)):
                        d = flo - entry
                        if best is None or d < best[0]: best = (d, flo, fhi)
        else:
            if hi[i] < lo[i + 2] and (lo[i + 2] - hi[i]) >= MINFVG:
                flo, fhi = hi[i], lo[i + 2]
                if fhi < entry:
                    mid = (flo + fhi) / 2.0
                    if not any(lo[j] <= mid for j in range(i + 3, sb + 1)):
                        d = entry - fhi
                        if best is None or d < best[0]: best = (d, flo, fhi)
    return best


def check(rep, hi, lo, ts, recent_dirs):
    """rep: setup/trace dict (needs 'model','dir','entry' and entry_bar OR a *_ms time).
       hi/lo/ts: from load_buffer(). recent_dirs: last up-to-5 emitted dirs, oldest..newest.
       Returns dict; magnet=False if it doesn't qualify."""
    out = dict(magnet=False, premium=False, size_mult=1.0, fvg_lo=None, fvg_hi=None, prev5_opp=0, tag='', badge='')
    if hi is None or str(rep.get('model')) != 'Reversal': return out
    if any(x in str(rep.get('cat', '')) for x in EXCLUDE_CATS): return out
    try: entry = float(rep['entry'])
    except Exception: return out
    sb = _bar_index(rep, ts)
    if sb is None or sb < 3 or sb >= len(hi): return out
    is_long = str(rep.get('dir')) == 'LONG'
    b = _nearest_unfilled_opposing_fvg(hi, lo, sb, entry, is_long)
    if not b: return out
    d, flo, fhi = b
    prev5_opp = sum(1 for x in (recent_dirs or [])[-5:] if x and x != rep.get('dir'))
    premium = prev5_opp >= PREV5_OPP_PREMIUM
    out.update(magnet=True, premium=premium, prev5_opp=prev5_opp,
               fvg_lo=round(flo, 2), fvg_hi=round(fhi, 2),
               size_mult=(SIZE_PREMIUM if premium else SIZE_MAGNET),
               badge=('\U0001F9F2\U0001F9F2' if premium else '\U0001F9F2'))
    if premium:
        out['tag'] = ('\U0001F9F2\U0001F9F2 MAGNET+ — reversal into unfilled FVG @ %.1f-%.1f, counter-trend '
                      '(last %d opp). Hist +0.64R OOS. Sugerowany rozmiar ×%.2f' % (flo, fhi, prev5_opp, SIZE_PREMIUM))
    else:
        out['tag'] = ('\U0001F9F2 MAGNET — reversal into unfilled FVG @ %.1f-%.1f (the draw). '
                      'Hist +0.38R OOS. Sugerowany rozmiar ×%.2f' % (flo, fhi, SIZE_MAGNET))
    return out


def tagline(rep, buf_path, recent_dirs):
    """One-line alert prefix ('' if not a magnet). Loads the buffer itself. Mirrors select_tag.tagline()."""
    buf = load_buffer(buf_path)
    if not buf: return ''
    r = check(rep, buf[0], buf[1], buf[2], recent_dirs)
    return (r['tag'] + '\n') if r['magnet'] else ''

# ================================ WIRING (2 spots in agent.py) ================================
#
# A) ALERT TAG — in _process_new, right after the select_tag block (~line 309):
#
#     try:                                                        # 🧲 magnet size-up tag (isolated, read-only)
#         import magnet as _mag, sqlite3
#         _recent = [r[0] for r in sqlite3.connect(DB).execute(
#             "SELECT dir FROM signals ORDER BY logged_at DESC LIMIT 5").fetchall()][::-1]
#         _mres = _mag.check(repx, *(_mag.load_buffer(BUF) or (None,None,None)), _recent)
#         if _mres['magnet']:
#             txt = _mres['tag'] + '\n' + txt
#             repx['_size_mult'] = _mres['size_mult']            # for the OPTIONAL auto-size-up below
#     except Exception as _me: print('magnet err', _me, flush=True)
#
#   OPTIONAL auto-size-up — in _exec_order, right after `qty = int(_sf[0]) if _sf else 1` (~line 105):
#         qty = int(round(qty * float(x.get('_size_mult', 1.0))))   # 🧲 magnet size-up (EXEC_MAX_QTY cap still applies below)
#   (Leave this out to ship tag-only first: you size up manually on the 1-click approve.)
#
# B) CANDIDATES BADGE — in the /candidates route, right after `rec = [...]` (~line 709):
#
#     try:                                                        # 🧲 magnet badge on confirmed candidates
#         import magnet as _mag, sqlite3
#         _buf = _mag.load_buffer(BUF)
#         _recent = [r[0] for r in sqlite3.connect(DB).execute(
#             "SELECT dir FROM signals ORDER BY logged_at DESC LIMIT 5").fetchall()][::-1]
#         if _buf:
#             for _r in rec:
#                 if _r.get('stage') == 'POTWIERDZONY':
#                     _m = _mag.check(_r, _buf[0], _buf[1], _buf[2], _recent)
#                     if _m['magnet']: _r['magnet'] = _m['badge']
#     except Exception as _e: print('[candidates] magnet err', _e, flush=True)
#
#   then add 'magnet' to _PREF (line 412) so it shows as a column, and add one legend line:
#     "🧲 = FVG-magnet (reversal into an unfilled FVG); 🧲🧲 = also counter-trend (premium size-up)."
