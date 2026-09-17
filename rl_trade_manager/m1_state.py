"""Causal M1-only execution state from fully closed one-minute bars."""
from __future__ import annotations

from .types import Bar, Trade

TICK = 0.25


def _continuous_tail(bars: tuple[Bar, ...]) -> tuple[Bar, ...]:
    if not bars:
        return ()
    start = len(bars) - 1
    while start > 0 and bars[start].ms - bars[start - 1].ms == 60_000:
        start -= 1
    return bars[start:]


def _confirmed_swings(bars: tuple[Bar, ...]):
    highs, lows = [], []
    for center in range(2, len(bars) - 2):
        window = bars[center - 2:center + 3]
        high = bars[center].high
        low = bars[center].low
        if sum(bar.high == high for bar in window) == 1 and high == max(bar.high for bar in window):
            highs.append((center + 2, high))
        if sum(bar.low == low for bar in window) == 1 and low == min(bar.low for bar in window):
            lows.append((center + 2, low))
    return highs, lows


def build_m1_state(trade: Trade, closed_count: int, current_price: float, mfe_r: float) -> dict:
    """`closed_count` excludes the current/future bar, including on reset."""
    closed_since_entry = trade.bars[:closed_count]
    all_closed = _continuous_tail(trade.pre_bars + closed_since_entry)
    highs, lows = _confirmed_swings(all_closed)
    break_direction = 0
    latest_opposite_break = False
    for i in range(1, len(all_closed)):
        prior, bar = all_closed[i - 1], all_closed[i]
        up = any(known < i and prior.close < price + TICK <= bar.close
                 for known, price in highs)
        down = any(known < i and prior.close > price - TICK >= bar.close
                   for known, price in lows)
        event = 1 if up and not down else -1 if down and not up else 0
        if event:
            break_direction = event
        if closed_count > 0 and i == len(all_closed) - 1:
            latest_opposite_break = event == -trade.direction

    if trade.direction == 1:
        candidates = [price for known, price in lows
                      if price < current_price and all(bar.low > price for bar in all_closed[known + 1:])]
        protected = max(candidates, default=None)
    else:
        candidates = [price for known, price in highs
                      if price > current_price and all(bar.high < price for bar in all_closed[known + 1:])]
        protected = min(candidates, default=None)

    if trade.fvg_low is None or trade.fvg_high is None:
        boundary = None
    else:
        boundary = trade.fvg_low if trade.direction == 1 else trade.fvg_high
    if trade.fvg_ce is None or trade.fvg_signal_ms is None:
        fvg_valid = None
    else:
        relevant = [bar for bar in all_closed if trade.fvg_signal_ms is not None
                    and bar.ms + 60_000 > trade.fvg_signal_ms]
        fvg_valid = not any(trade.direction * (bar.close - trade.fvg_ce) < 0 for bar in relevant)

    closes = [bar.close for bar in all_closed]
    def net_return(n):
        return (closes[-1] - closes[-n - 1]) / trade.risk if len(closes) > n else None

    if closed_since_entry:
        favorable = [bar.high if trade.direction == 1 else -bar.low for bar in closed_since_entry]
        peak = max(favorable)
        last_peak = max(i for i, value in enumerate(favorable) if value == peak)
        since_peak = len(favorable) - 1 - last_peak
    else:
        since_peak = 0

    price_r = trade.direction * (current_price - trade.entry) / trade.risk
    return {
        "structure_relative": float(break_direction * trade.direction),
        "protected_price": protected,
        "protected_distance_r": trade.direction * (current_price - protected) / trade.risk if protected is not None else None,
        "opposite_break": float(latest_opposite_break),
        "fvg_hold_valid": float(fvg_valid) if fvg_valid is not None else None,
        "fvg_boundary_distance_r": trade.direction * (current_price - boundary) / trade.risk if boundary is not None else None,
        "return_1_r": net_return(1),
        "return_3_r": net_return(3),
        "return_5_r": net_return(5),
        "bars_since_favorable_extreme": float(since_peak),
        "giveback_from_mfe_r": max(0.0, mfe_r - price_r),
        "close_3_back": closes[-4] if len(closes) > 3 else None,
    }
