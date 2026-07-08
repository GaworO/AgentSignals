"""Trade review reports and the summary performance report."""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def trade_report(md, row: dict, chart_rel: str | None = None) -> str:
    """Markdown review for one trade."""
    idx = md.df.index
    lines = [
        f"# Trade {row['entry_ts']} — {row['direction'].upper()} "
        f"({'WIN' if row['r_net'] > 0.05 else 'LOSS' if row['r_net'] < -0.05 else 'SCRATCH'} "
        f"{row['r_net']:+.2f}R)",
        "",
        "## Why this trade triggered",
        f"1. **Liquidity sweep** at `{idx[row['sweep_idx']]}` — "
        f"{row['sweep_kind']} at {row['sweep_level']:.2f} swept "
        f"(extreme {row['sweep_extreme']:.2f}).",
        f"2. **HTF FVG** — price delivering from a {row['fvg_tf']} gap "
        f"[{row['fvg_bottom']:.2f}, {row['fvg_top']:.2f}].",
        f"3. **1m IFVG** — created bar `{idx[row['ifvg_created']]}`, inverted "
        f"`{idx[row['ifvg_inverted']]}` zone [{row['ifvg_bottom']:.2f}, {row['ifvg_top']:.2f}].",
        f"4. **CISD** at `{idx[row['cisd_idx']]}`"
        + (f" (ref {row['cisd_ref']:.2f})." if row.get("cisd_ref") else "."),
        "",
        "## Execution",
        f"| | |",
        f"|---|---|",
        f"| Entry ({row['entry_mode']}) | {row['entry_px']:.2f} @ `{row['entry_ts']}` |",
        f"| Stop | {row['stop_px']:.2f} ({row['risk_pts']:.1f} pts risk) |",
        f"| Target | {row['tp_px']:.2f} |" if row.get("tp_px") else "| Target | dynamic |",
        f"| Exit | {row['exit_px']:.2f} @ `{row['exit_ts']}` ({row['exit_reason']}) |",
        f"| Contracts | {row['contracts']} |",
        f"| PnL | {row['pnl_usd']:+.0f} USD ({row['r_net']:+.2f}R net) |",
        f"| Duration | {row['duration_min']:.0f} min |",
        f"| MAE / MFE | {row['mae_r']:.2f}R / {row['mfe_r']:.2f}R |",
        f"| Session | {row['session']} |",
    ]
    if chart_rel:
        lines += ["", f"![chart]({chart_rel})"]
    return "\n".join(lines)


def summary_report(overall: dict, per_year: pd.DataFrame, per_session: pd.DataFrame,
                   mc: dict, funnel: dict, cfg: dict, verdict: str) -> str:
    def dic2md(d: dict) -> str:
        return "\n".join(f"| {k} | {v} |" for k, v in d.items())

    md = [
        "# ICT Four-Confirmation Model — Performance Report",
        "",
        f"**Verdict: {verdict}**",
        "",
        "## Headline (net of costs, realistic fills)",
        "| metric | value |", "|---|---|", dic2md(overall),
        "",
        "## Confirmation funnel",
        "| stage | count |", "|---|---|", dic2md(funnel),
        "",
        "## By year",
        per_year.to_markdown(),
        "",
        "## By session",
        per_session.to_markdown(),
        "",
        "## Monte Carlo (2000 resamples)",
        "| metric | value |", "|---|---|", dic2md(mc),
        "",
        "## Key config",
        "```yaml",
        f"cisd definition: {cfg['cisd']['definition']}",
        f"entry mode: {cfg['entry']['mode']}",
        f"stop: {cfg['stop']['model']} + {cfg['stop']['buffer_ticks']} ticks",
        f"exit: {cfg['exit']['model']} ({cfg['exit'].get('fixed_r')}R), "
        f"BE at {cfg['exit']['breakeven']['trigger_r']}R = {cfg['exit']['breakeven']['enabled']}",
        f"costs: {cfg['costs']['slippage_ticks_per_side']} tick slip/side, "
        f"${cfg['costs']['commission_usd_per_side']}/side, limit fill = "
        f"{cfg['costs']['limit_fill_rule']}",
        "```",
    ]
    return "\n".join(md)
