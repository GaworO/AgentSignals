"""Position sizing and account-level risk guards."""
from __future__ import annotations

import math
from dataclasses import dataclass, field


def contracts_for(cfg: dict, risk_points: float, point_value: float,
                  atr_now: float | None = None,
                  win_rate: float | None = None, payoff: float | None = None) -> int:
    r = cfg["risk"]
    mode = r["sizing"]
    if mode == "fixed_contracts":
        return int(r["fixed_contracts"])
    if mode == "fixed_risk_usd":
        usd = r["risk_usd"]
    elif mode == "fixed_pct":
        usd = r["account_size"] * r["risk_pct"] / 100.0
    elif mode == "atr" and atr_now:
        usd = r["risk_usd"] * min(2.0, max(0.5, atr_now and 1.0))
    elif mode == "kelly" and win_rate is not None and payoff:
        f = max(0.0, win_rate - (1 - win_rate) / payoff)
        usd = r["account_size"] * min(0.02, f / 4)  # quarter-Kelly, capped
    else:
        usd = r["risk_usd"]
    if risk_points <= 0:
        return 0
    n = math.floor(usd / (risk_points * point_value))
    return max(1, n)


@dataclass
class RiskGuard:
    """Daily loss cap, max trades/day, consecutive-loss stop, kill switch."""
    cfg: dict
    day: int = -1
    day_r: float = 0.0
    day_trades: int = 0
    consec_losses: int = 0
    cum_r: float = 0.0
    peak_r: float = 0.0
    killed: bool = False
    blocks: list = field(default_factory=list)

    def new_bar_day(self, tday: int) -> None:
        if tday != self.day:
            self.day = tday
            self.day_r = 0.0
            self.day_trades = 0
            self.consec_losses = 0  # streak guard is per-day

    def can_enter(self) -> tuple[bool, str]:
        r = self.cfg["risk"]
        if self.killed:
            return False, "kill switch"
        if self.day_r <= -r["max_daily_loss_r"]:
            return False, "daily loss cap"
        if self.day_trades >= r["max_daily_trades"]:
            return False, "max daily trades"
        if self.consec_losses >= r["max_consecutive_losses"]:
            return False, "max consecutive losses"
        return True, ""

    def register(self, r_net: float) -> None:
        r = self.cfg["risk"]
        self.day_trades += 1
        self.day_r += r_net
        self.cum_r += r_net
        self.peak_r = max(self.peak_r, self.cum_r)
        if r_net < -0.05:
            self.consec_losses += 1
        elif r_net > 0.05:
            self.consec_losses = 0
        if self.peak_r - self.cum_r >= r["kill_switch_dd_r"]:
            self.killed = True
