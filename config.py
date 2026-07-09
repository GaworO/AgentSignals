# detcore/config.py
# Every tunable lives here. The structural constants that used to be hardcoded in the
# det_v11 monolith (TOL, LOOKBACK, ATRMULT, ...) are now Config fields with identical
# defaults, so behaviour is unchanged unless you deliberately override them. The env-driven
# knobs (MODE, CAP_DAYS, CUTOFF, ...) keep the exact same names/contract that agent.py,
# regime.py and compare_v11.py rely on.
import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # ---- structural / confirmation params (were module-level constants in det_v11) ----
    tol: float = 3.0           # CE / FVG-edge tolerance
    lookback: int = 15         # short-term structure window for break-of-structure
    atrmult: float = 1.5       # displacement strength: sum(bodies) >= atrmult * ATR5m
    dispwin: int = 10          # bars after a trigger to search for the impulse
    maximp: int = 3            # max impulse candles
    retwin: int = 20           # window for the retrace back to 50% FVG
    boswin: int = 30           # window for BOS after the rejection
    buf: float = 3.0           # re-arm buffer (points)
    vimin: float = 10.0        # min body-to-body gap to register a Volume Imbalance
    vibig: float = 50.0        # min VI to act as a magnet (TP / bias)
    rr: float = 2.0            # take-profit reward multiple (was the hardcoded 2*risk)
    rej_frac: float = 0.5      # rejection invalidation/body-hold as frac of FVG from entry side: 0.5=CE (default, unchanged) | 0.25=stricter. env REJ_FRAC.
    session_bounds: tuple = (0, 8, 13, 18)

    # ---- env-driven knobs (unchanged names + defaults vs the monolith) ----
    mode: str = 'confirm'                 # 'confirm' (v10 multi-break) | 'sweep' (die on 1st breach)
    cap_days: int = 10                    # backstop: level dies after N trading days if never swept
    eod_intraday: bool = False            # v12: session H/L tapped-but-unconfirmed expires end of day
    disp_mode: str = 'chain'              # displacement: 'chain' = V1 (start>=minimp, extend whole same-colour run) | 'orig' = V0 (1-3 candle)
    minimp: int = 3                       # V1: min candles to start the chain
    maxext: int = 40                      # V1: safety cap on chain length
    max_stop_r: float = 40.0              # risk cap in points
    entry_primary: str = 'fvg'            # 'fvg' | 'fibo'
    cutoff: str = '2026-05-17'            # '' => no date filter (agent / backtest mode)
    data_csv: str = '/mnt/user-data/uploads/MNQ_databento_1m.csv'
    out_pkl: str = '/home/claude/det_new.pkl'
    trace_out: str = '/home/claude/trace.json'
    debug_trace: bool = False

    @classmethod
    def from_env(cls, env=None):
        """Build config from environment variables, matching det_v11's os.environ.get calls
        exactly (same keys, same defaults). Structural params stay at their dataclass defaults."""
        e = os.environ if env is None else env
        return cls(
            mode=e.get('MODE', 'confirm'),
            cap_days=int(e.get('CAP_DAYS', '10')),
            eod_intraday=bool(e.get('EOD_INTRADAY')),
            disp_mode=e.get('DISP_MODE', 'chain'),
            minimp=int(e.get('MINIMP', '3')),
            maxext=int(e.get('MAXEXT', '40')),
            dispwin=int(e.get('DISPWIN', '10')),
            rej_frac=float(e.get('REJ_FRAC', '0.5')),
            atrmult=float(e.get('ATRMULT', '1.5')),   # displacement-strength gate; 1.0 = ~+65% more setups (WF-validated), 1.5 = current
            max_stop_r=float(e.get('MAX_STOP_R', '40')),
            entry_primary=e.get('ENTRY_PRIMARY', 'fvg'),
            cutoff=e.get('CUTOFF', '2026-05-17'),
            data_csv=e.get('DATA_CSV', '/mnt/user-data/uploads/MNQ_databento_1m.csv'),
            out_pkl=e.get('OUT_PKL', '/home/claude/det_new.pkl'),
            trace_out=e.get('TRACE_OUT', '/home/claude/trace.json'),
            debug_trace=bool(e.get('DEBUG_TRACE')),
        )
