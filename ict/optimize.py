"""Parameter optimization: grid / random search + walk-forward + sensitivity.

A word of caution baked into the design: `walk_forward` is the only mode whose
output should be trusted for a go/no-go decision. In-sample grid results are
reported with their rank so overfit configs are visible, not hidden.
"""
from __future__ import annotations

import itertools
import random
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .backtest import Backtester
from .metrics import summarize
from .utils import deep_merge


def _set_path(cfg_over: dict, dotted: str, value) -> dict:
    node = cfg_over
    keys = dotted.split(".")
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value
    return cfg_over


def run_config(md, cfg: dict, start_idx=0, end_idx=None) -> pd.DataFrame:
    bt = Backtester(md, cfg)
    return bt.run(start_idx, end_idx)


def grid_search(md, base_cfg: dict, space: dict[str, list], max_evals: int | None = None,
                mode: str = "grid", seed: int = 42,
                start_idx: int = 0, end_idx: int | None = None) -> pd.DataFrame:
    """space: {"cisd.definition": ["A","B"], "stop.buffer_ticks": [4, 8, 16], ...}"""
    keys = list(space.keys())
    combos = list(itertools.product(*(space[k] for k in keys)))
    if mode == "random" and max_evals and len(combos) > max_evals:
        rng = random.Random(seed)
        combos = rng.sample(combos, max_evals)
    elif max_evals:
        combos = combos[:max_evals]

    rows = []
    for combo in combos:
        over: dict = {}
        for k, v in zip(keys, combo):
            _set_path(over, k, v)
        cfg = deep_merge(base_cfg, over)
        trades = run_config(md, cfg, start_idx, end_idx)
        s = summarize(trades) if len(trades) else {"n": 0}
        s.update({k: v for k, v in zip(keys, combo)})
        rows.append(s)
    out = pd.DataFrame(rows)
    if "expectancy_r" in out:
        out = out.sort_values("expectancy_r", ascending=False)
    return out


def walk_forward(md, base_cfg: dict, space: dict[str, list],
                 train_days: int = 252, test_days: int = 63,
                 objective: str = "net_r") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Anchored-window walk-forward. Optimize on train, trade next test window
    with the chosen params, stitch OOS segments together."""
    tdays = np.unique(md.tday)
    day_start = {d: int(np.searchsorted(md.tday, d)) for d in tdays}

    oos_trades = []
    windows = []
    pos = 0
    while pos + train_days + test_days <= len(tdays):
        tr_d0, tr_d1 = tdays[pos], tdays[pos + train_days - 1]
        te_d1 = tdays[min(pos + train_days + test_days - 1, len(tdays) - 1)]
        i_tr0 = day_start[tr_d0]
        i_tr1 = day_start.get(tdays[pos + train_days], len(md.c))
        i_te1 = day_start.get(tdays[pos + train_days + test_days], len(md.c)) \
            if pos + train_days + test_days < len(tdays) else len(md.c)

        res = grid_search(md, base_cfg, space, mode="grid",
                          start_idx=i_tr0, end_idx=i_tr1)
        res = res[res["n"] >= 8] if "n" in res else res
        if len(res) == 0:
            pos += test_days
            continue
        best = res.sort_values(objective, ascending=False).iloc[0]
        over: dict = {}
        for k in space:
            _set_path(over, k, best[k])
        cfg = deep_merge(base_cfg, over)
        te = run_config(md, cfg, i_tr1, i_te1)
        if len(te):
            oos_trades.append(te)
        windows.append({"train_end": str(md.df.index[i_tr1 - 1])[:10],
                        "params": {k: best[k] for k in space},
                        "is_exp": best.get("expectancy_r"),
                        "oos_n": len(te),
                        "oos_exp": round(float(te["r_net"].mean()), 3) if len(te) else np.nan})
        pos += test_days

    oos = pd.concat(oos_trades, ignore_index=True) if oos_trades else pd.DataFrame()
    return oos, pd.DataFrame(windows)


def sensitivity(md, base_cfg: dict, param: str, values: list,
                start_idx=0, end_idx=None) -> pd.DataFrame:
    """One-at-a-time parameter sweep around the frozen config."""
    return grid_search(md, base_cfg, {param: values}, mode="grid",
                       start_idx=start_idx, end_idx=end_idx)
