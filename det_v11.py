#!/usr/bin/env python3
# det_v11.py — THIN ENTRY POINT. The logic now lives in the detcore/ package, split by stage:
#   detcore/config       - tunables (env knobs + structural constants)
#   detcore/data         - loader, ATR, sessions, daily bias frame
#   detcore/catalysts    - WHERE the levels are (F.P.FVG, PDH/PDL, session H/L, BSL/SSL, NDOG/NWOG, VI)
#   detcore/scaffolding  - HOW levels arm/fire/die (v10-time vs v11-sweep) + run_all driver
#   detcore/confirmation - displacement, rejection, setup/BOS, bias
#   detcore/entries      - FVG-edge / OTE entry
#   detcore/exits        - take-profit + risk cap
#   detcore/emit         - assemble a setup record
#   detcore/pipeline     - dedup + cutoff + pickle
#
# The env/pickle contract is UNCHANGED: DATA_CSV, MODE, CAP_DAYS, MAX_STOP_R, ENTRY_PRIMARY,
# CUTOFF, OUT_PKL, DEBUG_TRACE, TRACE_OUT all behave exactly as before. Output is byte-for-byte
# identical to the old monolith (kept as det_v11_monolith.py; verified by gate_check.py).
#
# To change a rule, edit the relevant file in detcore/ — not this one.
from detcore import run

if __name__ == '__main__':
    run()
