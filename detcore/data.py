# detcore/data.py
# Stage 1: load the CSV and build every market array the rest of the pipeline reads.
# This is the old det_v11 LOADER + 5m-ATR + SESSIONS + daily-bias-frame blocks, unchanged,
# writing results onto a fresh Ctx instead of module globals.
import pandas as pd
import numpy as np
from collections import defaultdict

from .context import Ctx


def _sess(h, m):
    """TFO session label for an (hour, minute) in fixed UTC-4. Verbatim from det_v11."""
    if h >= 20: return 'ASIA'
    if 2 <= h < 5: return 'LO'
    if (h == 9 and m >= 30) or (10 <= h < 12): return 'NYAM'
    if h == 12: return 'NYL'
    if (h == 13 and m >= 30) or (14 <= h < 16): return 'NYPM'
    if 16 <= h < 20: return 'PM_AH'
    return 'PREM'


def load(cfg):
    """Read cfg.data_csv and return a populated Ctx."""
    c = Ctx(cfg)

    # ---- LOADER ----
    df = pd.read_csv(cfg.data_csv)
    ts = pd.to_datetime(df.ts_event, utc=True).dt.as_unit('ns')   # pandas3 = us -> force ns
    df = df.assign(ts=ts).sort_values('ts').reset_index(drop=True); ts = df.ts
    df['dt'] = ts.dt.tz_convert('Etc/GMT+4')   # fixed UTC-4 (like TFO), no DST
    c.df = df; c.ts = ts
    c.o, c.hi, c.lo, c.cl = df.open.values, df.high.values, df.low.values, df.close.values
    c.T = (ts.astype('int64') // 10**9).values
    c.H = df.dt.dt.hour.values; c.Mi = df.dt.dt.minute.values
    df['date'] = df.dt.dt.date.values; c.dates = df.date.values; c.n = len(df)
    c.mins = c.H * 60 + c.Mi
    c.days = sorted(df.date.unique()); c.dayi = {d: i for i, d in enumerate(c.days)}

    # first/last/all bar indices per day (fast lookups)
    dates, n = c.dates, c.n
    day_first_idx = {}; day_last_idx = {}; day_idx = defaultdict(list)
    for i in range(n):
        d = dates[i]
        if d not in day_first_idx: day_first_idx[d] = i
        day_last_idx[d] = i; day_idx[d].append(i)
    c.day_first_idx = day_first_idx; c.day_last_idx = day_last_idx; c.day_idx = day_idx

    # ---- 5m ATR mapped to 1m ----
    b5 = c.T // 300
    g5 = df.assign(b5=b5).groupby('b5').agg(h5=('high', 'max'), l5=('low', 'min'))
    g5['atr'] = (g5.h5 - g5.l5).rolling(20).mean().shift(1)
    ATR = df.assign(b5=b5).merge(g5[['atr']], left_on='b5', right_index=True, how='left')['atr'].values
    c.ATR = np.where(np.isnan(ATR), 0.0, ATR)

    # ---- SESSIONS (TFO windows, UTC-4) ----
    H, Mi, hi, lo = c.H, c.Mi, c.hi, c.lo
    S = np.array([_sess(h, m) for h, m in zip(H, Mi)])
    inst = []; cid = -1; prev = None
    for s in S:
        if s != prev: cid += 1
        inst.append(cid); prev = s
    inst = np.array(inst)
    sessinst = []
    for cc in np.unique(inst):
        ix = np.where(inst == cc)[0]
        sessinst.append((S[ix[0]], int(ix[0]), int(ix[-1]), float(hi[ix].max()), float(lo[ix].min())))
    c.S = S; c.inst = inst; c.sessinst = sessinst

    # ---- daily frame for bias (v0 flag) ----
    dd = df.set_index(ts).resample('1D').agg(h=('high', 'max'), l=('low', 'min'), c=('close', 'last')).dropna()
    c.dD = dd.index.tz_convert('Etc/GMT+4').date
    c.dH, c.dL, c.dC = dd.h.values, dd.l.values, dd.c.values

    return c
