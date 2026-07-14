#!/usr/bin/env python3
"""
build_shadow_seed.py - one-off: re-score historical A/B trades under the SAME fixed+BE rules
shadow.py uses live, and write shadow_seed.json (the backfill merged on startup).

Source signals: shadow_trades.json (12-mo A/B book, has entry/sl/tp/ms/dir/sess).
Bars: _mnq_sess_chunks/c6.csv + c7.csv (cover 2025-05 .. 2026-06, the whole A/B window).
Run from the Trading folder:  python3 build_shadow_seed.py
"""
import json, glob, importlib.util, os
import pandas as pd, numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('shadow', os.path.join(HERE, 'shadow.py'))
sh = importlib.util.module_from_spec(spec); spec.loader.exec_module(sh)

# --- assemble bars for the window from the chunk set (dedup overlaps, sort) ---
frames = []
for f in sorted(glob.glob(os.path.join(HERE, '_mnq_sess_chunks', 'c*.csv'))):
    d = pd.read_csv(f, usecols=['ts_event', 'high', 'low'])
    frames.append(d)
bars = pd.concat(frames, ignore_index=True)
bars['ms'] = pd.to_datetime(bars['ts_event'], utc=True, errors='coerce').astype('int64') // 10**6
bars = bars.dropna(subset=['ms']).drop_duplicates('ms').sort_values('ms').reset_index(drop=True)
MS = bars['ms'].to_numpy(); HI = bars['high'].to_numpy(float); LO = bars['low'].to_numpy(float)
print('bars assembled:', len(MS), 'range',
      pd.to_datetime(MS[0], unit='ms'), '->', pd.to_datetime(MS[-1], unit='ms'))

# --- re-score every historical A/B signal ---
src = [x for x in json.load(open(os.path.join(HERE, 'shadow_trades.json'))) if x.get('strategy') == 'A/B']
seed = []
import collections
cc = collections.Counter()
for x in src:
    r = sh.score(x['dir'], x['entry'], x['sl'], x['tp'], x['ms'], MS, HI, LO)
    oc = r.get('outcome')
    cc[oc] += 1
    rec = dict(key=sh._key('A/B', x['dir'], x['ms'], x['entry']),
               strategy='A/B', dir=x['dir'], sess=x['sess'], week=x['week'], dow=x['dow'],
               et=x['et'], date=x['date'], entry=x['entry'], sl=x['sl'], tp=x['tp'], ms=x['ms'],
               src='backfill', outcome=oc, R=r.get('R'), net=r.get('net'),
               outcome_fixed=r.get('outcome_fixed'), R_fixed=r.get('R_fixed'),
               net_fixed=r.get('net_fixed'), ct=r.get('ct'))
    seed.append(rec)

json.dump(seed, open(os.path.join(HERE, 'shadow_seed.json'), 'w'))

# --- report ---
fx = [s['R_fixed'] for s in seed if s.get('R_fixed') is not None]
be = [s['R'] for s in seed if s.get('R') is not None and s['outcome'] in ('win', 'loss', 'be', 'timeout')]
print('wrote shadow_seed.json:', len(seed), 'A/B trades')
print('outcomes:', dict(cc))
print('resolved fixed n=%d  mean R_fixed=%.3f' % (len(fx), sum(fx)/len(fx) if fx else 0))
print('resolved BE    n=%d  mean R_be   =%.3f' % (len(be), sum(be)/len(be) if be else 0))
