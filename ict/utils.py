"""Config loading, logging, small shared helpers."""
from __future__ import annotations

import copy
import logging
import sys
from pathlib import Path

import numpy as np
import yaml


def load_config(path: str | Path, overrides: dict | None = None) -> dict:
    """Load YAML config; optionally deep-merge an overrides dict on top."""
    with open(path) as f:
        cfg = yaml.safe_load(f)
    if overrides:
        cfg = deep_merge(cfg, overrides)
    return cfg


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def get_logger(name: str = "ict", level: str = "INFO",
               logfile: str | Path | None = None) -> logging.Logger:
    log = logging.getLogger(name)
    if log.handlers:  # already configured
        return log
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    if logfile:
        fh = logging.FileHandler(logfile, mode="w")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    return log


def hhmm_to_min(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_window(minute_of_day: int, start: str, end: str) -> bool:
    """True if ET minute-of-day falls in [start, end), handling midnight wrap."""
    a, b = hhmm_to_min(start), hhmm_to_min(end)
    if a <= b:
        return a <= minute_of_day < b
    return minute_of_day >= a or minute_of_day < b


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 20) -> np.ndarray:
    """Simple rolling-mean ATR, causal (value at i uses bars <= i)."""
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    out = np.convolve(tr, np.ones(n) / n, mode="full")[: len(tr)]
    out[: n - 1] = np.nan
    return out
