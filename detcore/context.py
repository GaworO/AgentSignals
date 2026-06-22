# detcore/context.py
# Ctx = the single run context passed to every stage. It holds the market arrays,
# the derived catalyst levels, the config, and the two mutable accumulators (out, cur_break).
# Nothing in detcore reads module globals — everything reads from a Ctx instance — so each
# stage (catalysts / confirmation / entries / exits / emit) can be edited in isolation.


class Ctx:
    """Mutable per-run state.

    Built by detcore.data.load(); enriched by detcore.catalysts.build_levels();
    consumed by detcore.scaffolding.run_all() and detcore.pipeline.

    Attributes are attached by the stages that own them:
      data.load        -> df, ts, o, hi, lo, cl, T, H, Mi, mins, dates, n,
                          days, dayi, day_first_idx, day_last_idx, day_idx,
                          ATR, S, inst, sessinst, dD, dH, dL
      catalysts.build  -> fpfvg, day_hl, gaplev, eqH, eqL, vis, bigvi
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.out = []        # accumulated confirmed setups (mutated by emit.emit)
        self.cur_break = 1   # break # of the active trigger (set by scaffolding.run_*)
        self._TRC = []       # debug-trace rows (collected only when cfg.debug_trace)
