"""Trade visualization — one annotated candle chart per trade."""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Rectangle


def _candles(ax, idx0, o, h, l, c):
    x = np.arange(idx0, idx0 + len(o))
    up = c >= o
    ax.vlines(x, l, h, color="#666", lw=0.7, zorder=2)
    ax.bar(x[up], (c - o)[up], 0.65, bottom=o[up], color="#26a69a", zorder=3)
    ax.bar(x[~up], (c - o)[~up], 0.65, bottom=o[~up], color="#ef5350", zorder=3)
    return x


def plot_trade(md, trade_row: dict, out_path: str | Path, pad_before: int = 90,
               pad_after: int = 40) -> None:
    """Render a full annotated chart for one trade (dict from results frame)."""
    t = trade_row
    i0 = max(0, int(t["sweep_idx"]) - pad_before)
    i1 = min(len(md.c), int(t["exit_idx"]) + pad_after)
    o, h, l, c = md.o[i0:i1], md.h[i0:i1], md.l[i0:i1], md.c[i0:i1]

    fig, ax = plt.subplots(figsize=(15, 8))
    x = _candles(ax, i0, o, h, l, c)

    # HTF FVG zone
    ax.add_patch(Rectangle((i0, t["fvg_bottom"]), i1 - i0, t["fvg_top"] - t["fvg_bottom"],
                           facecolor="#42a5f5", alpha=0.13, zorder=1,
                           label=f'HTF FVG ({t["fvg_tf"]})'))
    # IFVG zone
    ax.add_patch(Rectangle((t["ifvg_created"], t["ifvg_bottom"]),
                           i1 - t["ifvg_created"], t["ifvg_top"] - t["ifvg_bottom"],
                           facecolor="#ab47bc", alpha=0.22, zorder=1, label="IFVG"))

    # liquidity level + sweep
    ax.axhline(t["sweep_level"], color="#ff9800", lw=1.4, ls="--",
               label=f'liquidity ({t["sweep_kind"]}) {t["sweep_level"]:.2f}')
    ax.plot(t["sweep_idx"], t["sweep_extreme"], "v" if t["direction"] == "short" else "^",
            color="#ff9800", ms=13, zorder=5, label="sweep")

    # CISD
    if t.get("cisd_ref") is not None and not (isinstance(t["cisd_ref"], float) and np.isnan(t["cisd_ref"])):
        ax.axhline(t["cisd_ref"], color="#8d6e63", lw=1.0, ls=":", label="CISD ref")
    ax.axvline(t["cisd_idx"], color="#8d6e63", lw=0.8, ls=":")

    # entry / SL / TP / exit
    ax.axhline(t["stop_px"], color="#d32f2f", lw=1.3, label=f'SL {t["stop_px"]:.2f}')
    if t.get("tp_px"):
        ax.axhline(t["tp_px"], color="#2e7d32", lw=1.3, label=f'TP {t["tp_px"]:.2f}')
    mk = "^" if t["direction"] == "long" else "v"
    ax.plot(t["entry_idx"], t["entry_px"], mk, color="#1565c0", ms=14, zorder=6,
            label=f'entry {t["entry_px"]:.2f}')
    ax.plot(t["exit_idx"], t["exit_px"], "X", color="#000", ms=12, zorder=6,
            label=f'exit {t["exit_px"]:.2f} ({t["exit_reason"]})')

    res = "WIN" if t["r_net"] > 0.05 else ("LOSS" if t["r_net"] < -0.05 else "SCRATCH")
    ax.set_title(
        f'{t["direction"].upper()} {t["entry_ts"]} | {t["session"]} | sweep {t["sweep_kind"]} '
        f'| {res} {t["r_net"]:+.2f}R | MAE {t["mae_r"]:.2f} MFE {t["mfe_r"]:.2f}',
        fontsize=11)
    ax.legend(loc="best", fontsize=8, ncol=2)
    ax.set_xlim(i0, i1)
    seg_lo, seg_hi = l.min(), h.max()
    pad = (seg_hi - seg_lo) * 0.05
    ax.set_ylim(min(seg_lo, t["stop_px"]) - pad, max(seg_hi, t.get("tp_px") or seg_hi) + pad)
    ticks = np.linspace(i0, i1 - 1, 8).astype(int)
    ax.set_xticks(ticks)
    ax.set_xticklabels([str(md.df.index[j])[5:16] for j in ticks], fontsize=8)
    ax.grid(alpha=0.15)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def plot_equity(trades, out_path: str | Path, title: str = "Equity (R)") -> None:
    import pandas as pd
    s = trades.sort_values("exit_ts").reset_index(drop=True)["r_net"].cumsum()
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(s.values, lw=1.2)
    ax.set_title(title)
    ax.set_xlabel("trade #")
    ax.set_ylabel("cumulative R (net)")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
