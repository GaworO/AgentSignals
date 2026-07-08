"""Performance analytics: full metric suite + Monte Carlo."""
from __future__ import annotations

import numpy as np
import pandas as pd


def summarize(trades: pd.DataFrame, label: str = "") -> dict:
    """All headline metrics from a trades frame (needs r_net, pnl_usd, entry_ts)."""
    if trades is None or len(trades) == 0:
        return {"label": label, "n": 0}
    trades = trades.copy()
    for col in ("entry_ts", "exit_ts"):
        if col in trades and not pd.api.types.is_datetime64_any_dtype(trades[col]):
            trades[col] = pd.to_datetime(trades[col], utc=True)
    r = trades["r_net"].to_numpy()
    wins = r[r > 0.05]
    losses = r[r < -0.05]
    gross_win = wins.sum() if len(wins) else 0.0
    gross_loss = -losses.sum() if len(losses) else 0.0

    daily = trades.set_index("entry_ts")["r_net"].groupby(pd.Grouper(freq="1D")).sum()
    daily = daily[daily != 0]
    eq = r.cumsum()
    dd = eq - np.maximum.accumulate(eq)

    def _ratio(series: pd.Series, downside_only=False) -> float:
        if len(series) < 20 or series.std() == 0:
            return float("nan")
        s = series[series < 0] if downside_only else series
        denom = s.std()
        if denom == 0 or np.isnan(denom):
            return float("nan")
        return float(series.mean() / denom * np.sqrt(252))

    t_stat = float("nan")
    if len(daily) > 5 and daily.std() > 0:
        t_stat = float(daily.mean() / (daily.std() / np.sqrt(len(daily))))

    years = max((trades["entry_ts"].max() - trades["entry_ts"].min()).days / 365.25, 1e-9)
    monthly = trades.set_index("entry_ts")["r_net"].groupby(pd.Grouper(freq="1ME")).sum()

    return {
        "label": label,
        "n": int(len(r)),
        "trades_per_year": round(len(r) / years, 1),
        "net_r": round(float(r.sum()), 1),
        "net_usd": round(float(trades["pnl_usd"].sum()), 0),
        "expectancy_r": round(float(r.mean()), 3),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "win_rate": round(100 * len(wins) / len(r), 1),
        "avg_win_r": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss_r": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "max_dd_r": round(float(dd.min()), 1),
        "sharpe": round(_ratio(daily), 2),
        "sortino": round(_ratio(daily, downside_only=True), 2),
        "calmar": round(float(r.sum() / years / -dd.min()), 2) if dd.min() < 0 else float("nan"),
        "t_stat_daily": round(t_stat, 2),
        "avg_duration_min": round(float(trades["duration_min"].mean()), 0),
        "avg_mae_r": round(float(trades["mae_r"].mean()), 2),
        "avg_mfe_r": round(float(trades["mfe_r"].mean()), 2),
        "monthly_mean_r": round(float(monthly.mean()), 2) if len(monthly) else float("nan"),
        "pct_months_positive": round(100 * (monthly > 0).mean(), 0) if len(monthly) else float("nan"),
    }


def by_group(trades: pd.DataFrame, col: str) -> pd.DataFrame:
    rows = []
    for key, g in trades.groupby(col):
        s = summarize(g, str(key))
        rows.append(s)
    return pd.DataFrame(rows).set_index("label")


def by_year(trades: pd.DataFrame) -> pd.DataFrame:
    t = trades.copy()
    t["year"] = pd.to_datetime(t["entry_ts"]).dt.year
    return by_group(t, "year")


def monte_carlo(trades: pd.DataFrame, n_sims: int = 2000, seed: int = 42) -> dict:
    """Resample trade order with replacement; distribution of outcomes."""
    if len(trades) < 10:
        return {}
    r = trades["r_net"].to_numpy()
    rng = np.random.default_rng(seed)
    finals, maxdds = np.empty(n_sims), np.empty(n_sims)
    for s in range(n_sims):
        samp = rng.choice(r, size=len(r), replace=True)
        eq = np.cumsum(samp)
        finals[s] = eq[-1]
        maxdds[s] = (eq - np.maximum.accumulate(eq)).min()
    return {
        "p_profit": round(float((finals > 0).mean()), 3),
        "final_r_p5": round(float(np.percentile(finals, 5)), 1),
        "final_r_p50": round(float(np.percentile(finals, 50)), 1),
        "final_r_p95": round(float(np.percentile(finals, 95)), 1),
        "maxdd_p5": round(float(np.percentile(maxdds, 5)), 1),
        "maxdd_p50": round(float(np.percentile(maxdds, 50)), 1),
        "maxdd_p95": round(float(np.percentile(maxdds, 95)), 1),
    }


def equity_series(trades: pd.DataFrame) -> pd.Series:
    s = trades.sort_values("exit_ts").set_index("exit_ts")["r_net"].cumsum()
    return s
