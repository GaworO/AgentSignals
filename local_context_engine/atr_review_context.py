"""Causal ATR and intraday-path facts for the existing AI context review.

This is descriptive input, never an entry rule or a market-regime classifier.
It uses only M1 bars received by the frozen review snapshot.
"""
from __future__ import annotations

import math

VERSION = 'atr-review-observations-v1'
WINDOWS = (30,)


def _path(rows, cutoff, frozen_at, minutes):
    eligible = [b for b in rows if b['bar_close_ms'] // 1000 <= cutoff
                and b.get('received_at', b['bar_close_ms'] / 1000) <= frozen_at]
    recent = eligible[-minutes:]
    ends = [b['bar_close_ms'] // 1000 for b in recent]
    if ends != list(range(cutoff - (minutes - 1) * 60, cutoff + 1, 60)):
        return dict(state='INSUFFICIENT_CONTIGUOUS_BARS', minutes=minutes,
                    observed_minutes=len(recent))
    closes = [float(b['close']) for b in recent]
    changes = [b - a for a, b in zip(closes, closes[1:])]
    travel = sum(abs(x) for x in changes)
    net = closes[-1] - closes[0]
    values = dict(state='READY', minutes=minutes,
                  net_points=round(net, 4), high_low_points=round(
                      max(float(b['high']) for b in recent) - min(float(b['low']) for b in recent), 4),
                  close_path_points=round(travel, 4),
                  directional_efficiency=round(abs(net) / travel, 4) if travel else 0.0,
                  up_closes=sum(x > 0 for x in changes), down_closes=sum(x < 0 for x in changes),
                  unchanged_closes=sum(x == 0 for x in changes))
    if not all(math.isfinite(x) for x in (values['net_points'], values['high_low_points'],
                                          values['close_path_points'], values['directional_efficiency'])):
        raise ValueError('INVALID_ATR_REVIEW_OBSERVATION')
    return values


def attach(packet, histories, frozen_at):
    """Add facts and rebind the packet hash after the original ATR attachment."""
    from .entry_rules import fingerprint

    cutoff = packet['as_of']
    if type(cutoff) is not int or frozen_at < cutoff:
        raise ValueError('ATR_REVIEW_REQUIRES_FROZEN_CUTOFF')
    atr = packet.get('daily_atr') or {}
    paths = {symbol: {str(minutes): _path(histories[symbol], cutoff, frozen_at, minutes)
                      for minutes in WINDOWS}
             for symbol in ('ES', 'NQ', 'MNQ') if symbol in histories}
    cross = 'UNAVAILABLE'
    if all(symbol in paths and paths[symbol]['30']['state'] == 'READY' for symbol in ('ES', 'NQ')):
        es, nq = (paths[symbol]['30']['net_points'] for symbol in ('ES', 'NQ'))
        cross = ('SAME_DIRECTION' if es * nq > 0 else
                 'OPPOSITE_DIRECTION' if es * nq < 0 else 'ONE_OR_BOTH_FLAT')
    packet['atr_review_context'] = dict(
        version=VERSION, available_at=cutoff,
        atr_usable=atr.get('usable'),
        atr_estimated=atr.get('estimated'), daily_range_to_atr=atr.get('range_to_atr'),
        daily_atr_reached=atr.get('reached'), recent_paths=paths,
        es_nq_30m_direction=cross, regime='UNCLASSIFIED_REQUIRES_AI_REVIEW',
        eligibility='INFORMATION_ONLY')
    packet['packet_id'] = fingerprint({k: v for k, v in packet.items() if k != 'packet_id'})
    return packet
