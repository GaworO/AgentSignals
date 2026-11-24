"""Incremental IFVG observations, never a zone selector or an entry policy.

Keep the source-tested bearish-FVG -> bullish-inversion branch on M1 and
M5->M1/M5. Closed-bar crossings and later touches are facts, not trades.
Repeated crossings do not establish a required confirmation count, and a close
back below the gap does not silently invalidate a source narrative.
"""
from .common import digest


def supported_branch(zone, execution_timeframe):
    return (zone.get('kind') == 'FVG' and zone.get('direction') == 'SHORT'
            and zone['timeframe'] in (1, 5)
            and execution_timeframe in (1, zone['timeframe']))


def close_above(zone, frame):
    return frame['close'] > zone['upper']


def subsequent_retest(zone, frame, confirmation):
    return (frame['ts'] >= confirmation['available_at']
            and frame['low'] <= zone['upper']
            and frame['high'] >= zone['lower'])


def _event(branch, frame, kind, refs):
    value = dict(kind=kind, symbol=branch['symbol'], contract=branch['contract'],
                 timeframe=branch['execution_timeframe'],
                 zone_timeframe=branch['zone_timeframe'],
                 execution_timeframe=branch['execution_timeframe'],
                 ts=frame['ts'], available_at=frame['available_at'],
                 zone_id=branch['zone_id'], lower=branch['lower'], upper=branch['upper'],
                 direction='LONG', cycle=branch['close_cross_count'],
                 evidence_ids=refs, status='GEOMETRY_ONLY_SOURCE_CHOICE_UNKNOWN',
                 fully_qualified=False, fill_verified=False, orders_sent=0)
    value['id'] = digest(value)[:24]
    return value


def observe(tracked, markets, frames, as_of):
    """Mutate transactional memory for ALL eligible zones; return new facts.

    Bounded retention follows the existing market engine's last-20-per-TF
    memory, including inverted gaps. That is an engineering memory bound, not
    ICT invalidation. Existing events remain in the immutable SQLite journal.
    """
    zones = {z['id']: z for m in markets.values() for z in m['zones']
             if supported_branch(z, 1)}
    # A data gap/roll also clears the corresponding market-engine zones.
    for key in list(tracked):
        if tracked[key]['zone_id'] not in zones:
            del tracked[key]
    for z in zones.values():
        for tf in sorted({1, z['timeframe']}):
            key = z['id'] + ':M' + str(tf)
            if key not in tracked:
                tracked[key] = dict(
                    zone_id=z['id'], symbol=z['symbol'], contract=z['contract'],
                    lower=z['lower'], upper=z['upper'], zone_timeframe=z['timeframe'],
                    execution_timeframe=tf, formed_at=z['available_at'],
                    tracking_started_at=as_of,
                    history_complete_from_formation=z['available_at'] == as_of,
                    first_inversion=None, confirmation=None,
                    first_retest=None, last_retest=None, cycle_retest=None,
                    close_cross_count=0, retest_cycle_count=0,
                    last_frame_end=None, last_close_relation=None,
                    observation_state='WAITING_CLOSE_ABOVE',
                    trade_eligibility='UNKNOWN', selected_for_entry=False)
    emitted = []
    for f in sorted(frames, key=lambda x: (x['available_at'], x['timeframe'], x['id'])):
        if f['available_at'] > as_of or f['available_at'] < f['end']:
            raise ValueError('IFVG_FRAME_NOT_AVAILABLE')
        for b in tracked.values():
            if ((f['symbol'], f['contract'], f['timeframe']) !=
                    (b['symbol'], b['contract'], b['execution_timeframe'])
                    or f['ts'] < b['formed_at']):
                continue
            if b['last_frame_end'] is not None and f['ts'] != b['last_frame_end']:
                raise ValueError('IFVG_NONCONTIGUOUS_FRAME')
            # Consume only a confirmation known before this candle opened.
            anchor = b['confirmation']
            if anchor and b['cycle_retest'] is None and subsequent_retest(b, f, anchor):
                ev = _event(b, f, 'IFVG_RETEST_OBSERVED',
                            [b['zone_id'], anchor['id'], f['id']])
                emitted.append(ev)
                point = dict(id=ev['id'], bar_id=f['id'], available_at=f['available_at'])
                b['first_retest'] = b['first_retest'] or point
                b['last_retest'] = b['cycle_retest'] = point
                b['retest_cycle_count'] += 1
                b['observation_state'] = 'RETEST_OBSERVED_PARAMETERS_UNRESOLVED'
            above = close_above(b, f)
            # A new cross is recorded even after an earlier retest. The zone
            # and its first confirmation/touch are never reset to manufacture it.
            if above and b['last_close_relation'] != 'ABOVE':
                first = b['first_inversion'] is None
                b['close_cross_count'] += 1
                ev = _event(b, f, 'IFVG_INVERSION_OBSERVED' if first else 'IFVG_RECROSS_OBSERVED',
                            [b['zone_id'], f['id']])
                emitted.append(ev)
                point = dict(id=ev['id'], bar_id=f['id'], available_at=f['available_at'])
                b['first_inversion'] = b['first_inversion'] or point
                b['confirmation'] = point
                b['cycle_retest'] = None
                b['observation_state'] = 'WAITING_SUBSEQUENT_RETEST'
            b['last_close_relation'] = ('ABOVE' if above else
                                        'BELOW' if f['close'] < b['lower'] else 'WITHIN')
            b['last_frame_end'] = f['end']
    return emitted


def assessment(tracked):
    return dict(scope='ALL_SUPPORTED_ZONES_OBSERVATION_ONLY',
                selection_rule='NONE; ALL_ELIGIBLE_ZONES_ARE_TRACKED',
                selected_zone_id=None, action='UNKNOWN',
                position_state='NOT_MANAGED_BY_OBSERVER',
                first_entry='UNKNOWN', add_to_position='UNKNOWN',
                stop='UNKNOWN', target='UNKNOWN',
                first_blocker='SOURCE_ZONE_SELECTION_AND_ENTRY_RULE_UNRESOLVED',
                tracked_branches=len(tracked), orders_sent=0)
