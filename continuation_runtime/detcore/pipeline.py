# detcore/pipeline.py
# Stage 6: wire the stages together, dedup, apply the CUTOFF filter, and (for the script
# contract) dump the pickle. This is the old tail of det_v11 (DEDUP + CUTOFF + pickle/print).
import pickle
import json
from collections import Counter

import pandas as pd

from .config import Config
from .data import load
from .catalysts import build_levels
from .scaffolding import run_all


def detect(cfg):
    """Run the full pipeline in-process. Returns (finals, ded, ctx) without touching disk.
    Use this from compare_v11 / notebooks / tests; use run() for the CLI/subprocess contract."""
    ctx = load(cfg)
    build_levels(ctx)
    run_all(ctx)

    # ---- v31: resolve orphaned-FVG re-arms (zones the price ran away from) ----
    if getattr(ctx, 'orphans', None):
        from .emit import emit_orphan
        seen_o = set()
        for o in ctx.orphans:
            k = (o['dr'], round(o['disp']['fvg'][0], 2), round(o['disp']['fvg'][1], 2), int(o['disp']['fvg_bar']))
            if k in seen_o: continue                     # same physical zone logged once per catalyst
            seen_o.add(k)
            emit_orphan(ctx, o)

    # ---- DEDUP (same model/cat/dir within a 30-bar bucket) ----
    seen = set(); ded = []
    for x in sorted(ctx.out, key=lambda z: z['emit_bar']):
        key = (x['model'], x['cat'], x['dir'], x['emit_bar'] // 30)
        if key in seen: continue
        seen.add(key); ded.append(x)

    # ---- CUTOFF filter (empty cutoff => no filter, agent/backtest mode) ----
    if cfg.cutoff:
        cut = pd.Timestamp(cfg.cutoff, tz='Etc/GMT+4')
        finals = [x for x in ded if ctx.df.dt[x['emit_bar']] >= cut]
    else:
        finals = list(ded)
    finals = sorted(finals, key=lambda z: z['emit_bar'])
    return finals, ded, ctx


def run(cfg=None):
    """CLI / subprocess entry point. Reproduces det_v11's env -> pickle + stdout contract."""
    if cfg is None:
        cfg = Config.from_env()
    finals, ded, ctx = detect(cfg)

    pickle.dump(finals, open(cfg.out_pkl, 'wb'))
    if cfg.debug_trace:
        json.dump(ctx._TRC, open(cfg.trace_out, 'w'))

    print('MODE:', cfg.mode, '| CAP_DAYS:', cfg.cap_days)
    print('CALOSC:', len(ded), '| Model:', dict(Counter(x['model'] for x in ded)))
    print('wynik (po filtrze):', len(finals), '| Model:', dict(Counter(x['model'] for x in finals)))
    print('po katalizatorze:', dict(Counter(x['cat'] for x in finals)))
    return finals
