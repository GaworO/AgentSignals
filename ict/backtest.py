"""Event-driven backtester with live-realistic fills.

Fill model (deliberately pessimistic — matches the standards used to validate
the live A/B system):
  * market entries fill at the NEXT bar's open, slipped against you
  * limit entries fill only if price trades THROUGH the limit (touch != fill)
  * stop-losses fill at stop price slipped against you (or the open, if the
    bar gaps through)
  * take-profit limits also require trade-through
  * if a bar touches both stop and target, the STOP is assumed first
  * breakeven moves are applied at bar close, effective the next bar
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .data import MarketData
from .fvg import HTFFVGManager
from .liquidity import PoolManager, build_levels
from .risk import RiskGuard, contracts_for
from .strategy import EntrySignal, SetupEngine
from .utils import hhmm_to_min


@dataclass
class Trade:
    direction: str
    signal_idx: int
    entry_idx: int
    entry_px: float
    stop_px: float
    tp_px: float | None
    risk_pts: float
    contracts: int
    session: str
    sweep_idx: int
    sweep_level: float
    sweep_kind: str
    sweep_extreme: float
    fvg_tf: str
    fvg_top: float
    fvg_bottom: float
    ifvg_top: float
    ifvg_bottom: float
    ifvg_created: int
    ifvg_inverted: int
    cisd_idx: int
    cisd_ref: float | None
    entry_mode: str
    log: list = field(default_factory=list)
    # filled on exit
    exit_idx: int | None = None
    exit_px: float | None = None
    exit_reason: str = ""
    pnl_pts: float = 0.0
    r_gross: float = 0.0
    r_net: float = 0.0
    mae_r: float = 0.0
    mfe_r: float = 0.0
    be_moved: bool = False
    # runtime
    _stop_now: float = 0.0
    _pending_stop: float | None = None

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()
             if not k.startswith("_") and k != "log"}
        return d


class Backtester:
    def __init__(self, md: MarketData, cfg: dict, log=None):
        self.md = md
        self.cfg = cfg
        self.log = log
        self.tick = cfg["data"]["tick_size"]
        self.pv = cfg["data"]["point_value"]
        self.slip = cfg["costs"]["slippage_ticks_per_side"] * self.tick
        self.comm = cfg["costs"]["commission_usd_per_side"]
        self.trades: list[Trade] = []
        self.decision_log: list[tuple[str, str]] = []

    # ------------------------------------------------------------ main loop
    def run(self, start_idx: int = 0, end_idx: int | None = None) -> pd.DataFrame:
        md, cfg = self.md, self.cfg
        n = end_idx if end_idx is not None else len(md.c)
        pools = PoolManager(md, cfg, build_levels(md, cfg))
        fvgs = HTFFVGManager(md, cfg)
        engine = SetupEngine(md, cfg, fvgs)
        guard = RiskGuard(cfg)
        allowed = set(cfg["sessions"]["allowed"])
        no_entry_after = hhmm_to_min(cfg["sessions"]["no_new_entries_after"])
        flatten_at = hhmm_to_min(cfg["sessions"]["flatten_at"])
        entry_cfg, stop_cfg, exit_cfg = cfg["entry"], cfg["stop"], cfg["exit"]
        dir_filter = cfg["entry"].get("direction_filter", "both")  # both|long|short (gate-neutral default)
        accum_on = cfg.get("accumulation", {}).get("enabled", False)

        open_trade: Trade | None = None
        pending: dict | None = None  # working entry order

        for i in range(start_idx, n):
            guard.new_bar_day(int(md.tday[i]))
            mod = int(md.minute_of_day[i])

            # ---- update context managers (causal) ----
            fvgs.step(i)
            sweeps = pools.step(i)

            # ---- manage open trade ----
            if open_trade is not None:
                open_trade = self._manage(open_trade, i, mod, flatten_at, guard)

            # ---- resolve working entry order ----
            if pending is not None and open_trade is None:
                pending, open_trade = self._try_fill(pending, i, guard)
                if open_trade is not None:
                    # the fill bar itself can stop you out — manage it now
                    open_trade = self._manage(open_trade, i, mod, flatten_at, guard)

            # ---- feed the state machine ----
            if open_trade is None and pending is None:
                engine.on_sweeps(i, sweeps)
                sig = engine.step(i)
                if sig is not None:
                    ok, why = guard.can_enter()
                    sess = md.session[i]
                    if not ok:
                        self._dlog(i, f"signal blocked: {why}")
                    elif dir_filter != "both" and sig.direction != dir_filter:
                        self._dlog(i, f"signal blocked: dir {sig.direction}")
                    elif accum_on and not md.accum_ok[i]:
                        self._dlog(i, "signal blocked: not an accumulation day")
                    elif sess not in allowed or mod >= no_entry_after and mod < 1080:
                        self._dlog(i, f"signal blocked: session {sess}")
                    else:
                        pending = self._make_order(sig, i)
            else:
                # a setup can't run while an order/trade is live
                engine.abort()

            self.trades = self.trades  # no-op, clarity

        # close anything still open at data end
        if open_trade is not None:
            self._exit(open_trade, n - 1, self.md.c[n - 1], "data_end", guard)
        self.stats = engine.stats
        return self.results_frame()

    # ------------------------------------------------------------ orders
    def _make_order(self, sig: EntrySignal, i: int) -> dict:
        entry_cfg = self.cfg["entry"]
        mode = entry_cfg["mode"]
        limit = None
        if mode == "limit_ifvg":
            limit = sig.ifvg.top if sig.direction == "long" else sig.ifvg.bottom
        elif mode == "limit_ifvg_mid":
            limit = sig.ifvg.ce
        elif mode == "limit_fvg_ce":
            limit = sig.htf_fvg.ce
        self._dlog(i, f"entry order ({mode}) {sig.direction} after CISD")
        return {"sig": sig, "mode": mode, "limit": limit,
                "placed": i, "ttl": entry_cfg["limit_ttl_bars"]}

    def _try_fill(self, pending: dict, i: int, guard: RiskGuard):
        md = self.md
        sig: EntrySignal = pending["sig"]
        mode, limit = pending["mode"], pending["limit"]
        d = sig.direction
        fill_px = None

        if mode == "market":
            if i > pending["placed"]:
                fill_px = md.o[i] + self.slip if d == "long" else md.o[i] - self.slip
        else:
            if i > pending["placed"]:
                if i - pending["placed"] > pending["ttl"]:
                    self._dlog(i, "limit order expired unfilled")
                    return None, None
                rule = self.cfg["costs"]["limit_fill_rule"]
                if d == "long":
                    hit = md.l[i] < limit if rule == "trade_through" else md.l[i] <= limit
                else:
                    hit = md.h[i] > limit if rule == "trade_through" else md.h[i] >= limit
                if hit:
                    fill_px = limit
                    # gap through the limit -> better price
                    if d == "long" and md.o[i] < limit:
                        fill_px = md.o[i]
                    if d == "short" and md.o[i] > limit:
                        fill_px = md.o[i]

        if fill_px is None:
            return pending, None

        trade = self._open_trade(sig, i, float(fill_px))
        if trade is None:
            return None, None
        return None, trade

    def _open_trade(self, sig: EntrySignal, i: int, entry_px: float) -> Trade | None:
        md, cfg = self.md, self.cfg
        d = sig.direction
        stop_px = self._initial_stop(sig, entry_px)
        risk = (entry_px - stop_px) if d == "long" else (stop_px - entry_px)
        risk_pts = float(risk)
        s = cfg["stop"]
        if risk_pts < s["min_stop_points"] or risk_pts > s["max_stop_points"]:
            self._dlog(i, f"skip: stop {risk_pts:.1f}pt outside "
                          f"[{s['min_stop_points']},{s['max_stop_points']}]")
            return None
        tp_px = self._initial_tp(sig, entry_px, risk_pts)
        ncon = contracts_for(cfg, risk_pts, self.pv, md.atr1m[i])
        if ncon == 0:
            return None
        t = Trade(
            direction=d, signal_idx=sig.signal_idx, entry_idx=i, entry_px=entry_px,
            stop_px=stop_px, tp_px=tp_px, risk_pts=risk_pts, contracts=ncon,
            session=str(md.session[i]), sweep_idx=sig.sweep.confirm_idx,
            sweep_level=sig.sweep.level.price, sweep_kind=sig.sweep.level.kind,
            sweep_extreme=sig.sweep.extreme,
            fvg_tf=sig.htf_fvg.tf if sig.htf_fvg else "none",
            fvg_top=sig.htf_fvg.top if sig.htf_fvg else float("nan"),
            fvg_bottom=sig.htf_fvg.bottom if sig.htf_fvg else float("nan"),
            ifvg_top=sig.ifvg.top if sig.ifvg else None,
            ifvg_bottom=sig.ifvg.bottom if sig.ifvg else None,
            ifvg_created=sig.ifvg.created_idx if sig.ifvg else None,
            ifvg_inverted=sig.ifvg.inverted_idx if sig.ifvg else None,
            cisd_idx=sig.cisd_idx, cisd_ref=sig.cisd_ref,
            entry_mode=cfg["entry"]["mode"], log=list(sig.log),
        )
        t._stop_now = stop_px
        t.log.append((str(md.df.index[i]),
                      f"ENTRY {d} {ncon}x @ {entry_px:.2f} SL {stop_px:.2f} "
                      f"TP {tp_px if tp_px else 'dyn'} risk {risk_pts:.1f}pt"))
        self._dlog(i, f"entry executed {d} @ {entry_px:.2f}")
        return t

    # ------------------------------------------------------------ levels
    def _initial_stop(self, sig: EntrySignal, entry_px: float) -> float:
        s = self.cfg["stop"]
        buf = s["buffer_ticks"] * self.tick
        d = sig.direction
        md = self.md
        if s["model"] == "sweep":
            anchor = sig.sweep.extreme
        elif s["model"] == "ifvg":
            anchor = sig.ifvg.bottom if d == "long" else sig.ifvg.top
        elif s["model"] == "swing":
            k = 5
            j0 = max(0, sig.signal_idx - 45)
            seg_l = md.l[j0: sig.signal_idx + 1]
            seg_h = md.h[j0: sig.signal_idx + 1]
            anchor = float(seg_l.min()) if d == "long" else float(seg_h.max())
        elif s["model"] == "atr":
            a = md.atr1m[sig.signal_idx]
            a = 10.0 if np.isnan(a) else a
            return entry_px - s["atr_mult"] * a if d == "long" else entry_px + s["atr_mult"] * a
        else:
            anchor = sig.sweep.extreme
        return anchor - buf if d == "long" else anchor + buf

    def _initial_tp(self, sig: EntrySignal, entry_px: float, risk_pts: float) -> float | None:
        e = self.cfg["exit"]
        d = sig.direction
        md = self.md
        i = sig.signal_idx
        if e["model"] == "fixed_r":
            return entry_px + e["fixed_r"] * risk_pts if d == "long" \
                else entry_px - e["fixed_r"] * risk_pts
        if e["model"] == "prev_day_level":
            lvl = md.pdh[i] if d == "long" else md.pdl[i]
            if not np.isnan(lvl) and ((d == "long" and lvl > entry_px + risk_pts)
                                      or (d == "short" and lvl < entry_px - risk_pts)):
                return float(lvl)
        if e["model"] == "opposing_fvg":
            pass  # falls through to fixed-R fallback
        # next_liquidity / session_liquidity need the pool state; the engine
        # passes a fallback of fixed 2R when no valid pool target exists.
        return entry_px + 2.0 * risk_pts if d == "long" else entry_px - 2.0 * risk_pts

    # ------------------------------------------------------------ management
    def _manage(self, t: Trade, i: int, mod: int, flatten_at: int, guard: RiskGuard):
        md = self.md
        d = t.direction
        e = self.cfg["exit"]
        if i < t.entry_idx:
            return t

        # pending breakeven from previous close becomes live now
        if t._pending_stop is not None:
            t._stop_now = t._pending_stop
            t._pending_stop = None

        o, h, l, c = md.o[i], md.h[i], md.l[i], md.c[i]

        # ---- excursions (before exit checks, using this bar) ----
        if d == "long":
            t.mae_r = min(t.mae_r, (l - t.entry_px) / t.risk_pts)
            t.mfe_r = max(t.mfe_r, (h - t.entry_px) / t.risk_pts)
        else:
            t.mae_r = min(t.mae_r, (t.entry_px - h) / t.risk_pts)
            t.mfe_r = max(t.mfe_r, (t.entry_px - l) / t.risk_pts)

        # ---- stop first (conservative) ----
        if d == "long" and l <= t._stop_now:
            px = min(t._stop_now, o) - self.slip
            return self._exit(t, i, px, "stop" if not t.be_moved else "breakeven", guard)
        if d == "short" and h >= t._stop_now:
            px = max(t._stop_now, o) + self.slip
            return self._exit(t, i, px, "stop" if not t.be_moved else "breakeven", guard)

        # ---- target (trade-through) ----
        if t.tp_px is not None:
            if d == "long" and (h > t.tp_px or o > t.tp_px):
                return self._exit(t, i, max(t.tp_px, o if o > t.tp_px else t.tp_px), "target", guard)
            if d == "short" and (l < t.tp_px or o < t.tp_px):
                return self._exit(t, i, min(t.tp_px, o if o < t.tp_px else t.tp_px), "target", guard)

        # ---- time stop / flatten ----
        if i - t.entry_idx >= e["time_stop_min"]:
            return self._exit(t, i, c - self.slip if d == "long" else c + self.slip, "time", guard)
        if flatten_at <= mod < 1080:
            return self._exit(t, i, c - self.slip if d == "long" else c + self.slip, "flatten", guard)

        # ---- trailing stop ----
        if e["trailing"]["enabled"]:
            a = md.atr1m[i]
            if not np.isnan(a):
                if d == "long":
                    t._pending_stop = max(t._stop_now, c - e["trailing"]["atr_mult"] * a)
                else:
                    t._pending_stop = min(t._stop_now, c + e["trailing"]["atr_mult"] * a)

        # ---- breakeven (applied at close, live next bar) ----
        be = e["breakeven"]
        if be["enabled"] and not t.be_moved:
            trig = be["trigger_r"] * t.risk_pts
            off = be["offset_ticks"] * self.tick
            if d == "long" and h >= t.entry_px + trig:
                t._pending_stop = t.entry_px + off
                t.be_moved = True
                t.log.append((str(md.df.index[i]), "stop moved to breakeven"))
            elif d == "short" and l <= t.entry_px - trig:
                t._pending_stop = t.entry_px - off
                t.be_moved = True
                t.log.append((str(md.df.index[i]), "stop moved to breakeven"))
        return t

    def _exit(self, t: Trade, i: int, px: float, reason: str, guard: RiskGuard):
        md = self.md
        t.exit_idx = i
        t.exit_px = float(px)
        t.exit_reason = reason
        pts = (px - t.entry_px) if t.direction == "long" else (t.entry_px - px)
        t.pnl_pts = float(pts)
        t.r_gross = pts / t.risk_pts
        comm_r = (2 * self.comm) / (t.risk_pts * self.pv)  # commission per contract, in R
        t.r_net = t.r_gross - comm_r
        t.log.append((str(md.df.index[i]),
                      f"EXIT {reason} @ {px:.2f} -> {t.r_net:+.2f}R net"))
        guard.register(t.r_net)
        self.trades.append(t)
        self._dlog(i, f"exit {reason} {t.r_net:+.2f}R")
        return None

    # ------------------------------------------------------------ output
    def _dlog(self, i: int, msg: str) -> None:
        if self.cfg["engine"].get("decision_log"):
            self.decision_log.append((str(self.md.df.index[i]), msg))
        if self.log:
            self.log.debug("%s %s", self.md.df.index[i], msg)

    def results_frame(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()
        rows = []
        idx = self.md.df.index
        for t in self.trades:
            d = t.to_dict()
            d["entry_ts"] = idx[t.entry_idx]
            d["exit_ts"] = idx[t.exit_idx] if t.exit_idx is not None else None
            d["signal_ts"] = idx[t.signal_idx]
            d["sweep_ts"] = idx[t.sweep_idx]
            d["duration_min"] = (t.exit_idx - t.entry_idx) if t.exit_idx is not None else None
            d["pnl_usd"] = (t.pnl_pts * self.pv - 2 * self.comm) * t.contracts
            d["win"] = t.r_net > 0.05
            rows.append(d)
        return pd.DataFrame(rows)
