"""The four-confirmation state machine.

IDLE --sweep+HTF-FVG--> AWAIT_IFVG --inversion--> AWAIT_CISD --fired--> signal

The machine never looks forward: every transition happens on the close of the
bar that produced the evidence. It emits `EntrySignal`s; execution (fills,
stops, targets) lives in backtest.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .cisd import CISDDetector
from .ifvg import IFVG, IFVGTracker
from .liquidity import SweepEvent


@dataclass
class EntrySignal:
    direction: str
    signal_idx: int            # bar whose close completed confirmation 4
    sweep: SweepEvent
    htf_fvg: object            # FVG
    ifvg: IFVG
    cisd_idx: int
    cisd_ref: float | None
    cisd_note: str
    log: list = field(default_factory=list)


@dataclass
class SetupState:
    direction: str
    sweep: SweepEvent
    htf_fvg: object
    started_idx: int
    ifvg_tracker: IFVGTracker
    ifvg: IFVG | None = None
    cisd: CISDDetector | None = None
    ifvg_confirmed_idx: int | None = None
    log: list = field(default_factory=list)


class SetupEngine:
    def __init__(self, md, cfg: dict, fvg_mgr):
        self.md = md
        self.cfg = cfg
        self.fvg_mgr = fvg_mgr
        self.setup: SetupState | None = None   # one_setup_at_a_time
        self.stats = {"sweeps": 0, "sweeps_with_fvg": 0, "ifvg_confirmed": 0,
                      "cisd_confirmed": 0, "timeout_ifvg": 0, "timeout_cisd": 0,
                      "ifvg_invalidated": 0}

    def on_sweeps(self, i: int, sweeps: list[SweepEvent]) -> None:
        """Confirmation 1 + 2: adopt a sweep if an aligned HTF FVG is in play."""
        for sw in sweeps:
            self.stats["sweeps"] += 1
            if self.setup is not None:
                continue  # already working a setup
            gap = self.fvg_mgr.gap_in_play(i, sw.direction)
            if gap is None and self.cfg["htf_fvg"].get("required", True):
                continue
            # optional confluence: the sweep extreme must print INSIDE the HTF gap
            if gap is not None and self.cfg["htf_fvg"].get("require_sweep_inside", False) and \
                    not (gap.bottom <= sw.extreme <= gap.top):
                continue
            self.stats["sweeps_with_fvg"] += 1
            tracker = IFVGTracker(self.md, self.cfg, sw.direction, i)
            st = SetupState(sw.direction, sw, gap, i, tracker)
            if not self.cfg["ifvg"].get("required", True):
                # 3-confirmation mode: skip IFVG, arm CISD straight off the sweep
                st.cisd = CISDDetector(self.md, self.cfg, sw.direction, i,
                                       anchor_idx=i)
                st.ifvg_confirmed_idx = i
            ts = self.md.df.index[i]
            st.log.append((str(ts), f"SWEEP {sw.direction} of {sw.level.kind} "
                                    f"{sw.level.price:.2f} (pen {sw.penetration_ticks:.0f}t)"))
            if gap is not None:
                st.log.append((str(ts), f"HTF FVG confirmed: {gap.tf} {gap.direction} "
                                        f"[{gap.bottom:.2f},{gap.top:.2f}] fill {gap.filled_pct:.0f}%"))
            self.setup = st

    def step(self, i: int) -> EntrySignal | None:
        """Confirmation 3 + 4. Called every bar while a setup is active."""
        st = self.setup
        if st is None:
            return None
        md, cfg = self.md, self.cfg
        ts = str(md.df.index[i])

        # ---- global timeouts / death ----
        if st.ifvg_confirmed_idx is None:
            if i - st.started_idx > cfg["ifvg"]["search_window_min"]:
                self.stats["timeout_ifvg"] += 1
                self.setup = None
                return None
        else:
            if i - st.ifvg_confirmed_idx > cfg["cisd"]["window_min"]:
                self.stats["timeout_cisd"] += 1
                self.setup = None
                return None

        ifvg_required = cfg["ifvg"].get("required", True)

        # ---- Confirmation 3: run the IFVG tracker (skipped in 3-confirm mode) ----
        newly = st.ifvg_tracker.step(i) if ifvg_required else None
        if st.ifvg is not None and st.ifvg.state == "invalidated":
            self.stats["ifvg_invalidated"] += 1
            st.log.append((ts, "IFVG invalidated — setup dead"))
            self.setup = None
            return None
        if st.ifvg is None and newly is not None:
            st.ifvg = newly
            st.ifvg_confirmed_idx = i
            st.cisd = CISDDetector(md, cfg, st.direction, i,
                                   anchor_idx=st.started_idx)
            self.stats["ifvg_confirmed"] += 1
            st.log.append((ts, f"IFVG inverted [{newly.bottom:.2f},{newly.top:.2f}] "
                               f"strength {newly.strength:.2f} — waiting for CISD"))
            if cfg["cisd"]["require_after_ifvg"]:
                return None  # CISD can only fire on a LATER bar

        # ---- Confirmation 4: CISD ----
        if st.cisd is not None and (st.ifvg is not None or not ifvg_required):
            fired, ref, note = st.cisd.check(i)
            if fired:
                self.stats["cisd_confirmed"] += 1
                st.log.append((ts, f"CISD confirmed ({cfg['cisd']['definition']}): {note}"))
                sig_ifvg = st.ifvg
                if sig_ifvg is None and getattr(st.cisd, "disp_fvg", None):
                    # def-E: expose the displacement FVG so entry limits /
                    # records / charts can use it like an IFVG zone
                    from types import SimpleNamespace
                    f = st.cisd.disp_fvg
                    sig_ifvg = SimpleNamespace(
                        top=f["top"], bottom=f["bottom"],
                        ce=(f["top"] + f["bottom"]) / 2,
                        created_idx=f["created_idx"], inverted_idx=None)
                sig = EntrySignal(st.direction, i, st.sweep, st.htf_fvg, sig_ifvg,
                                  i, ref, note, log=list(st.log))
                self.setup = None
                return sig
        return None

    def abort(self, reason: str = "") -> None:
        self.setup = None
