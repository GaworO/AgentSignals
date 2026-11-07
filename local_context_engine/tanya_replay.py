"""Offline research primitives, not Tanya's complete strategy or an order executor.

Inputs: sorted unique one-minute OHLC bars with START timestamps in UTC seconds.
Each bar becomes observable at time + 60. Aggregates become observable only
when every constituent minute is present and closed. FVG / IFVG definitions
below are explicit research definitions; discretionary selection remains open.
"""
from dataclasses import asdict, dataclass
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
import argparse
import json

ET = ZoneInfo('America/New_York')


def false_move_extension(origin, extreme, anchors_available_at, as_of):
    """The -1 price projection observed May 26, with explicitly supplied anchors.

    This is geometric extension, not a statistical standard deviation estimate.
    The caller must establish anchor availability without future swing selection.
    It neither selects those anchors nor submits a take-profit order.
    """
    import math
    if anchors_available_at > as_of:
        raise ValueError('Anchor information is not available yet')
    if not all(math.isfinite(v) for v in (origin, extreme)) or origin == extreme:
        raise ValueError('Need two distinct finite price anchors')
    return dict(ratio=-1, origin=origin, extreme=extreme,
                price=2*origin-extreme, available_at=anchors_available_at,
                anchor_selection='external; not automated', is_order=False)


@dataclass(frozen=True)
class Bar:
    time: int
    open: float
    high: float
    low: float
    close: float


def load_bars(path):
    with open(path) as f:
        records = json.load(f)
    bars = [Bar(**{k: r[k] for k in Bar.__dataclass_fields__}) for r in records]
    validate(bars)
    return bars


def validate(bars):
    import math
    last = None
    for b in bars:
        if b.time % 60 or (last is not None and b.time <= last):
            raise ValueError('Need unique, increasing minute-start timestamps')
        if not all(math.isfinite(v) for v in [b.open, b.high, b.low, b.close]):
            raise ValueError('Nonfinite OHLC')
        if not b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high:
            raise ValueError('Invalid OHLC')
        last = b.time


def aggregate(bars, minutes, as_of):
    """Reject incomplete intervals; fixed UTC boundaries for M1/M2/M3/M5/M15 only."""
    if minutes not in (1, 2, 3, 5, 15):
        raise ValueError('HTF session anchoring is intentionally not implemented')
    groups = defaultdict(list)
    seconds = minutes * 60
    for b in bars:
        if b.time + 60 <= as_of:
            groups[(b.time // seconds) * seconds].append(b)
    result = []
    for start, group in sorted(groups.items()):
        if [b.time for b in group] != list(range(start, start + seconds, 60)):
            continue
        result.append(Bar(start, group[0].open, max(b.high for b in group),
                          min(b.low for b in group), group[-1].close))
    return result


def fvg_events(bars, minutes, as_of):
    """Three-candle wick gap, followed by first close beyond its opposite edge.

    Zones expire at the next ET calendar date for this intraday experiment.
    This expiry is an implementation scope limit, not a claimed trader rule.
    Missing aggregates reset all local zones: no stitching over unknown prices.
    Equality at a zone edge does not invert. Events are observations, not trades.
    """
    candles = aggregate(bars, minutes, as_of)
    events, zones, segment = [], [], []
    step = minutes * 60
    prev = None
    for b in candles:
        day = datetime.fromtimestamp(b.time, ET).date()
        if prev is None or b.time != prev.time + step or day != datetime.fromtimestamp(prev.time, ET).date():
            zones, segment = [], []
        known = b.time + step
        for z in zones:
            inverted = (z['side'] == 'bull' and b.close < z['lower']) or (z['side'] == 'bear' and b.close > z['upper'])
            if not z['inverted'] and inverted:
                z['inverted'] = True
                events.append(dict(kind='IFVG', direction='short' if z['side'] == 'bull' else 'long',
                                   timeframe=minutes, available_at=known, zone_id=z['id'],
                                   lower=z['lower'], upper=z['upper'], close=b.close))
        segment.append(b)
        if len(segment) >= 3:
            a = segment[-3]
            side = 'bull' if b.low > a.high else 'bear' if b.high < a.low else None
            if side:
                lo, hi = (a.high, b.low) if side == 'bull' else (b.high, a.low)
                zone = dict(id=f'{minutes}:{known}:{side}', side=side, lower=lo,
                            upper=hi, inverted=False)
                zones.append(zone)
                events.append(dict(kind='FVG', direction=side, timeframe=minutes,
                                   available_at=known, zone_id=zone['id'], lower=lo, upper=hi))
        prev = b
    return events


def smt_window(es, mnq, reference_start, reference_end, current_start, current_end, side, as_of):
    """Compare two explicitly selected paired windows, never pick extrema ex post.

    Current extrema are cumulative within the declared window: once both assets
    have swept, SMT is invalidated. Strict sweep (not equal touch); testing the
    trader's equal-low convention is left as an explicit unresolved variant.
    A call with future endpoints is rejected; all paired minute data required.
    """
    if not reference_start < reference_end <= current_start < current_end <= as_of:
        raise ValueError('Windows must be historical, disjoint and nonempty')
    if side not in ('high', 'low'):
        raise ValueError('Choose high or low')
    extrema = {}
    for name, bars in [('ES', es), ('MNQ', mnq)]:
        lookup = {b.time: b for b in bars if b.time + 60 <= as_of}
        vals = []
        for start, end in [(reference_start, reference_end), (current_start, current_end)]:
            expected = list(range(start, end, 60))
            if start % 60 or end % 60 or any(t not in lookup for t in expected):
                return dict(status='missing_paired_data', available_at=current_end)
            vals.append((max if side == 'high' else min)(getattr(lookup[t], side) for t in expected))
        extrema[name] = dict(reference=vals[0], current=vals[1],
                             swept=vals[1] > vals[0] if side == 'high' else vals[1] < vals[0])
    a, b = extrema['ES']['swept'], extrema['MNQ']['swept']
    return dict(status='divergence' if a != b else 'both_swept' if a else 'neither_swept',
                available_at=current_end, side=side, extrema=extrema,
                anchor_selection='manual research; automatic anchor selection unresolved')


def replay(bars, as_of):
    validate(bars)
    events = sorted([e for m in (1, 2, 3, 5, 15) for e in fvg_events(bars, m, as_of)],
                    key=lambda e: (e['available_at'], e['timeframe'], e['kind'], e['zone_id']))
    return dict(schema_version=1, mode='offline_feature_replay', as_of=as_of, events=events,
                trade_signals=[], executable=False,
                unresolved=['HTF bias and POI selection', 'SMT anchor selection and equal touches',
                            'which inversion to trade', 'displacement threshold',
                            'stop, partials, runners and reentry', 'sizing and execution costs'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bars', help='JSON minute OHLC bars; start timestamps')
    parser.add_argument('--as-of', type=int, required=True, help='UTC epoch observation cutoff')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    with open(args.output, 'w') as f:
        json.dump(replay(load_bars(args.bars), args.as_of), f, indent=2)
