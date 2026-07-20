#!/usr/bin/env python3
"""
det_forex.py — EURUSD-calibrated detector for the "forex" service.

The forex service is your EXISTING agent.py run as a SECOND Railway service (same second-service
pattern strategy_f documents), pointed at this detector via DET_FILE and fed a EURUSD bar stream.
agent.py's _detect() does: subprocess `python3 DET_FILE` with env DATA_CSV/OUT_PKL/CUTOFF, then
pickle.load(OUT_PKL). This file honors that exact contract — it just:

  1. reads the agent's EURUSD buffer (raw prices ~1.07),
  2. multiplies prices x100000 so the detector's hard-coded round(price,1) is harmless,
  3. applies EURUSD volatility-scaled thresholds + pooled equal-H/L (else slow FX OOMs),
  4. runs the SAME detcore engine (all catalysts — "does all the current service does"),
  5. scales the price fields in each setup BACK DOWN /100000 so the agent alerts real EURUSD prices,
  6. pickles the list in the identical schema det_v11.py emits.

Imports detcore READ-ONLY. Touches no production file. R/expectancy are scale-invariant.
"""
import os, sys, pickle, tempfile, numpy as np, pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE); sys.path.insert(0, ROOT)
from instruments import INSTRUMENTS, MNQ_REF_RANGE, MNQ_THRESHOLDS
import detcore.catalysts as _cat
from detcore.config import Config
from detcore.pipeline import detect

INST = os.environ.get('FOREX_INSTRUMENT', 'eurusd').lower()
spec = INSTRUMENTS[INST]; MULT = spec.get('price_mult', 1)
DATA = os.environ['DATA_CSV']; OUT = os.environ.get('OUT_PKL', '/tmp/det_forex.pkl')

df = pd.read_csv(DATA)
for c in ('open', 'high', 'low', 'close'):
    if c in df.columns: df[c] = df[c] * MULT
scaled = os.path.join(tempfile.gettempdir(), f'_forex_{INST}.csv'); df.to_csv(scaled, index=False)
mr = float(np.median((df.high - df.low).values)); scale = mr / MNQ_REF_RANGE
TH = {k: v * scale for k, v in MNQ_THRESHOLDS.items()}

def _equals_pooled(sw, tol=None):                 # one liquidity pool per tol-bucket (slow FX would OOM otherwise)
    tol = TH['equals_tol']; e = {}
    for i in range(len(sw)):
        for j in range(i + 1, len(sw)):
            if sw[j][1] - sw[i][1] > 86400: break
            if abs(sw[i][0] - sw[j][0]) <= tol:
                lvl = round((sw[i][0] + sw[j][0]) / 2, 1); t = sw[j][1]; b = round(lvl / max(tol, 1e-9))
                if b not in e or t > e[b][1]: e[b] = (lvl, t)
    return list(e.values())
_cat._equals = _equals_pooled

cfg = Config.from_env()                            # honor agent's MODE / DISPWIN / CUTOFF / etc.
cfg.data_csv = scaled; cfg.disp_mode = 'chain'
cfg.tol, cfg.buf, cfg.vimin, cfg.vibig, cfg.max_stop_r = TH['tol'], TH['buf'], TH['vimin'], TH['vibig'], TH['max_stop_r']
finals, ded, ctx = detect(cfg)

PRICE_FIELDS = ('entry', 'SL', 'TP', 'risk', 'fvg_lo', 'fvg_hi', 'ce')   # everything else is bars/dirs/labels
for r in finals:
    for f in PRICE_FIELDS:
        if isinstance(r.get(f), (int, float)): r[f] = round(r[f] / MULT, 6)
pickle.dump(finals, open(OUT, 'wb'))
try:      # /candidates diagnostics: detcore wrote the trace with SCALED prices (xMULT) — rescale the
          # fvg fields to real quotes so the candidates page shows 1.16234, not 116234.5
    if cfg.debug_trace and os.path.exists(cfg.trace_out):
        import json as _json
        _tr = _json.load(open(cfg.trace_out))
        for _r in _tr:
            if isinstance(_r.get('fvg'), list):
                _r['fvg'] = [round(float(v) / MULT, 6) for v in _r['fvg']]
        _json.dump(_tr, open(cfg.trace_out, 'w'))
except Exception as _te:
    print('[det_forex] trace rescale err', _te, flush=True)
print(f"[det_forex] {INST} scale={scale:.4f} max_stop_r={TH['max_stop_r']/MULT:.5f} setups={len(finals)} -> {OUT}", flush=True)
