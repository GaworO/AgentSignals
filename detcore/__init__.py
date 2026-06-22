# detcore/ — v11 detector, split by responsibility.
#
# Pipeline order (each stage is one editable file):
#   config       -> all tunables (env + structural constants)
#   data         -> load CSV, build arrays / ATR / sessions / daily bias frame
#   catalysts    -> WHERE the levels are (F.P.FVG, PDH/PDL, session H/L, BSL/SSL, NDOG/NWOG, VI)
#   scaffolding  -> HOW levels arm/fire/die (v10-time vs v11-sweep) + the driver run_all()
#   confirmation -> displacement, rejection, setup/BOS, bias flag
#   entries      -> FVG-edge / OTE entry + SL
#   exits        -> take-profit (rr*R) + risk cap
#   emit         -> assemble a setup record into ctx.out
#   pipeline     -> dedup + cutoff + pickle  (run / detect)
#
# Typical use:
#   from detcore import run        # CLI/subprocess: env -> pickle (what det_v11.py calls)
#   from detcore import detect, Config
#   finals, ded, ctx = detect(Config(mode='sweep', cutoff=''))   # in-process, no disk
from .config import Config
from .context import Ctx
from .pipeline import run, detect

__all__ = ['Config', 'Ctx', 'run', 'detect']
