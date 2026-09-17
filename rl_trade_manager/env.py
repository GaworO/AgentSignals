"""One filled trade per episode. Gymnasium API; no policy learning here."""
from __future__ import annotations

import numpy as np
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

try:
    import gymnasium as gym
    from gymnasium import spaces
except ImportError:  # Keeps Phase-1 replay verifiable without installing PPO dependencies.
    gym = None

    class _Discrete:
        def __init__(self, n): self.n = n
        def contains(self, x): return isinstance(x, (int, np.integer)) and 0 <= x < self.n

    class _Box:
        def __init__(self, low, high, shape, dtype):
            self.low, self.high, self.shape, self.dtype = low, high, shape, dtype
        def contains(self, x):
            a = np.asarray(x)
            return a.shape == self.shape and np.isfinite(a).all()

    class spaces:
        Discrete = _Discrete
        Box = _Box

from .features import NAMES, relative, status_code
from .m1_state import build_m1_state

BaseEnv = gym.Env if gym is not None else object
HOLD, TAKE_50_PERCENT, MOVE_SL_TO_BREAKEVEN, MOVE_SL_TO_PROTECTED_STRUCTURE, CLOSE_FULL = range(5)
TICK_VALUE = 2.0
ROUND_TRIP_COST = 2.24


class TradeManagementEnv(BaseEnv):
    """Actions occur immediately after the recorded fill and at later bar boundaries.

    The action is applied before the next bar; that bar then resolves stop before
    target. The mask excludes unavailable actions during policy operation;
    direct invalid calls remain defensively converted to HOLD and reported in
    info. Protected structure uses only causally confirmed M1 swings.
    """

    metadata = {"render_modes": []}

    def __init__(self, trade):
        self.trade = trade
        self.action_space = spaces.Discrete(5)
        bound = np.finfo(np.float32).max
        self.observation_space = spaces.Box(-bound, bound, shape=(2 * len(NAMES),), dtype=np.float32)
        self.reset()

    def reset(self, *, seed=None, options=None):
        if gym is not None:
            super().reset(seed=seed)
        self.i = 0
        self.remaining = 1.0
        self.realized_gross_r = 0.0
        self.cost_r = ROUND_TRIP_COST / (2 * self.trade.risk) / 2  # entry-side cost
        self.sl = self.trade.initial_sl
        self.partial_taken = False
        self.terminated = False
        self.last_price = self.trade.entry
        self.mfe_r = 0.0
        self.mae_r = 0.0
        self.execution_reached = False
        self.reason = None
        self.previous_equity_r = 0.0
        return self._observation(), self._info()

    def _protected(self):
        return self._m1()["protected_price"]

    def _m1(self):
        return build_m1_state(self.trade, self.i, self.last_price, self.mfe_r)

    def _valid_stop(self, candidate):
        t = self.trade
        return (candidate is not None and np.isfinite(candidate)
                and t.direction * (candidate - self.sl) > 0
                and t.direction * (self.last_price - candidate) > 0)

    def action_masks(self):
        """Causal validity at the current decision, in action-space order."""
        if self.terminated:
            return np.zeros(self.action_space.n, dtype=np.bool_)
        t = self.trade
        partial_qty = t.qty // 2
        partial_valid = (self.remaining > 0.5 and not self.partial_taken
                         and self.remaining == 1.0 and partial_qty >= 1
                         and partial_qty / t.qty <= self.remaining)
        return np.array((
            True,
            partial_valid,
            self._valid_stop(t.entry),
            self._valid_stop(self._protected()),
            self.remaining > 0,
        ), dtype=np.bool_)

    def _unrealized(self):
        t = self.trade
        return self.remaining * t.direction * (self.last_price - t.entry) / t.risk

    def _equity(self):
        return self.realized_gross_r + self._unrealized() - self.cost_r

    def _close(self, fraction, price):
        t = self.trade
        self.realized_gross_r += fraction * t.direction * (price - t.entry) / t.risk
        self.cost_r += fraction * ROUND_TRIP_COST / (2 * t.risk) / 2  # exit-side cost
        self.remaining -= fraction
        if abs(self.remaining) < 1e-12:
            self.remaining = 0.0

    def _observation(self):
        t = self.trade
        values = np.zeros(len(NAMES), dtype=np.float32)
        available = np.zeros(len(NAMES), dtype=np.float32)

        def put(name, value):
            k = NAMES.index(name)
            if value is not None and np.isfinite(value):
                values[k], available[k] = value, 1.0

        put("direction", t.direction)
        put("unrealized_r", self._unrealized())
        put("mfe_r", self.mfe_r)
        put("mae_r", self.mae_r)
        put("fraction_remaining", self.remaining)
        put("sl_distance_r", t.direction * (self.last_price - self.sl) / t.risk)
        put("tp_distance_r", t.direction * (t.target - self.last_price) / t.risk)
        put("minutes_since_entry", self.i)
        # The source execution profile flattens at 15:55 America/New_York.
        # This is calendar state, independent of the future trade outcome.
        now = datetime.fromtimestamp((t.fill_ms + self.i * 60_000) / 1000, timezone.utc).astimezone(ZoneInfo("America/New_York"))
        minute_ny = now.hour * 60 + now.minute
        put("session_remaining", max(0, 955 - minute_ny) / 655.0)
        dol = t.dol_states[self.i] if self.i < len(t.dol_states) else None
        execution_price = None
        if dol is not None:
            htf_price = dol["htf_price"]
            execution_price = dol["execution_price"]
            put("htf_direction_relative", relative(dol["htf_direction"], t.direction))
            put("htf_status", status_code(dol["htf_status"]))
            put("htf_distance_r", t.direction * (htf_price - self.last_price) / t.risk if htf_price is not None else None)
            put("execution_direction_relative", relative(dol["execution_direction"], t.direction))
            put("execution_status", status_code(dol["execution_status"]))
            put("execution_distance_r", t.direction * (execution_price - self.last_price) / t.risk if execution_price is not None else None)
            put("execution_reached", float(self.execution_reached) if t.execution_price is not None else None)

        m1 = self._m1()
        put("m1_structure_relative", m1["structure_relative"])
        put("protected_m1_swing_price", m1["protected_price"])
        put("protected_m1_distance_r", m1["protected_distance_r"])
        put("opposite_m1_mss_bos", m1["opposite_break"])
        put("fvg_hold_valid", m1["fvg_hold_valid"])
        put("fvg_boundary_distance_r", m1["fvg_boundary_distance_r"])
        put("return_1_r", m1["return_1_r"])
        put("return_3_r", m1["return_3_r"])
        put("return_5_r", m1["return_5_r"])
        if execution_price is not None:
            put("execution_dol_progress_since_entry_r",
                (abs(execution_price - t.entry) - abs(execution_price - self.last_price)) / t.risk)
            if m1["close_3_back"] is not None:
                put("execution_dol_progress_3_r",
                    (abs(execution_price - m1["close_3_back"]) - abs(execution_price - self.last_price)) / t.risk)
        put("bars_since_favorable_extreme", m1["bars_since_favorable_extreme"])
        put("giveback_from_mfe_r", m1["giveback_from_mfe_r"])
        return np.concatenate((values, available))

    def _info(self):
        return {
            "trade_id": self.trade.key, "fraction_remaining": self.remaining,
            "realized_gross_r": self.realized_gross_r, "unrealized_r": self._unrealized(),
            "cost_r": self.cost_r, "equity_r": self._equity(),
            "current_sl": self.sl, "partial_taken": self.partial_taken,
            "reason": self.reason,
        }

    def step(self, action):
        if self.terminated:
            raise RuntimeError("Episode has terminated")
        if not self.action_space.contains(action):
            raise ValueError("Action must be an integer from 0 through 4")
        requested = int(action)
        invalid = False
        t = self.trade
        if requested == TAKE_50_PERCENT:
            partial_qty = t.qty // 2
            if self.partial_taken or self.remaining != 1.0 or partial_qty < 1:
                invalid = True
            else:
                self._close(partial_qty / t.qty, self.last_price)
                self.partial_taken = True
        elif requested == MOVE_SL_TO_BREAKEVEN:
            if self._valid_stop(t.entry):
                self.sl = t.entry
            else:
                invalid = True
        elif requested == MOVE_SL_TO_PROTECTED_STRUCTURE:
            candidate = self._protected()
            if self._valid_stop(candidate):
                self.sl = float(candidate)
            else:
                invalid = True
        elif requested == CLOSE_FULL:
            self._close(self.remaining, self.last_price)
            self.terminated = True
            self.reason = "CLOSE_FULL"

        if not self.terminated:
            if self.i >= len(t.bars):
                raise RuntimeError("Bar path exhausted")
            bar = t.bars[self.i]
            flatten_now = self.i == len(t.bars) - 1 and t.exit_reason == "EOD"
            stop_hit = (bar.low <= self.sl if t.direction == 1 else bar.high >= self.sl) and not flatten_now
            target_hit = (bar.high >= t.target if t.direction == 1 else bar.low <= t.target) and not flatten_now
            # Source bracket replay permits no target on fill bar. Stop remains live.
            if self.i == 0:
                target_hit = False
            if stop_hit:
                price = min(self.sl, bar.open) if t.direction == 1 else max(self.sl, bar.open)
                self._close(self.remaining, price)
                self.terminated, self.reason = True, "STOP"
            elif target_hit:
                self._close(self.remaining, t.target)
                self.terminated, self.reason = True, "TARGET"
            elif flatten_now:
                # Canonical EOD flatten happens at this bar's OPEN, before its
                # high/low is relevant. This branch applies to EOD only.
                self._close(self.remaining, bar.open)
                self.terminated, self.reason = True, "SESSION_END"
            else:
                if self.i == len(t.bars) - 1:
                    raise RuntimeError("Canonical path ended without SL/TP")
                self.last_price = bar.close
            # A terminal fill can precede the bar's eventual high/low. Never
            # expose those later extremes, even in a terminal observation.
            if not self.terminated:
                favorable = (bar.high - t.entry) / t.risk if t.direction == 1 else (t.entry - bar.low) / t.risk
                adverse = (t.entry - bar.low) / t.risk if t.direction == 1 else (bar.high - t.entry) / t.risk
                self.mfe_r = max(self.mfe_r, favorable, 0.0)
                self.mae_r = max(self.mae_r, adverse, 0.0)
                if self.i > 0 and t.execution_price is not None and t.execution_direction:
                    self.execution_reached |= (bar.high >= t.execution_price if t.execution_direction == 1 else bar.low <= t.execution_price)
            self.i += 1
        equity = self._equity()
        reward = equity - self.previous_equity_r
        self.previous_equity_r = equity
        info = self._info()
        info.update(requested_action=requested, executed_action=HOLD if invalid else requested, invalid_action=invalid)
        return self._observation(), float(reward), self.terminated, False, info
