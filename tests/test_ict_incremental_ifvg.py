"""Candle-only Observer integration; no selected zone enters the scanner."""
import json

from services.tanja.ict_agent.observer import Observer
from services.tanja.ict_agent.store import Store
from ifvg_candle_fixture import nq_august10_fixture


class NoSourceHints:
    version = 'NO_SOURCE_HINTS'
    index_version = 'NO_SOURCE_HINTS'

    def retrieve(self, *args, **kwargs):
        return dict(records=[], audit_notes=[])


def bars():
    # Feed only candles, never the fixture's annotated gap.
    for f in sorted((f for f in nq_august10_fixture().values() if 'end' in f),
                    key=lambda f: f['ts']):
        yield dict(symbol=f['symbol'], contract=f['contract'], ts=f['ts'],
                   received_at=f['available_at'], source='HISTORICAL_REGRESSION_EXCERPT',
                   **{k: f[k] for k in ('open', 'high', 'low', 'close')})


def memory(store):
    with store.readonly() as db:
        return json.loads(db.execute('SELECT payload FROM memory WHERE id=1').fetchone()[0])


def source_zone(mem):
    # Film values enter only this post-observation assertion, never Observer.
    return next(z for z in mem['ifvg_observations'].values()
                if (z['lower'], z['upper'], z['zone_timeframe'], z['execution_timeframe'])
                == (29787.5, 29804.25, 1, 1))


def test_autonomous_zone_two_retests_and_no_position_invention(tmp_path):
    store = Store(tmp_path/'observer.sqlite3', {'test': 'all-zones'})
    observer = Observer(store, NoSourceHints())
    checkpoints = {}
    for bar in bars():
        observer.push([bar])
        if bar['received_at'] in (36*60, 48*60, 49*60, 59*60, 60*60, 61*60):
            checkpoints[bar['received_at']] = source_zone(memory(store))
    assert checkpoints[36*60]['observation_state'] == 'WAITING_CLOSE_ABOVE'
    assert checkpoints[48*60]['first_inversion']['available_at'] == 48*60
    assert checkpoints[49*60]['first_retest']['available_at'] == 49*60
    final = checkpoints[61*60]
    assert final['first_retest'] == checkpoints[49*60]['first_retest']
    assert final['first_inversion'] == checkpoints[48*60]['first_inversion']
    assert final['confirmation']['available_at'] == 60*60
    assert final['last_retest']['available_at'] == 61*60
    assert final['close_cross_count'] == final['retest_cycle_count'] == 2
    assert final['trade_eligibility'] == 'UNKNOWN' and not final['selected_for_entry']
    assert final['history_complete_from_formation']
    snap = store.snapshot()
    assert snap['ifvg_assessment']['selected_zone_id'] is None
    assert snap['ifvg_assessment']['first_entry'] == 'UNKNOWN'
    assert snap['ifvg_assessment']['add_to_position'] == 'UNKNOWN'
    assert snap['ifvg_assessment']['position_state'] == 'NOT_MANAGED_BY_OBSERVER'
    assert snap['orders_sent'] == 0
    with store.readonly() as db:
        events = [json.loads(p) for p, in db.execute("SELECT payload FROM events WHERE kind='IFVG_RETEST_OBSERVED'")]
    assert [e['available_at'] for e in events if e['zone_id'] == final['zone_id']] == [49*60, 61*60]


def test_prefix_restart_and_dedup_preserve_original_zone(tmp_path):
    a = Store(tmp_path/'a.sqlite3', {'test': 'restart'})
    b = Store(tmp_path/'b.sqlite3', {'test': 'restart'})
    oa, ob = Observer(a, NoSourceHints()), Observer(b, NoSourceHints())
    for bar in bars():
        oa.push([bar])
        if bar['ts'] == 56*60:
            ob = Observer(b, NoSourceHints())  # Same journal, no reset.
        ob.push([bar])
        assert memory(a) == memory(b)
        assert a.snapshot() == b.snapshot()
    before = b.stats()
    assert ob.push([bar])['duplicate']
    assert b.stats() == before
    assert source_zone(memory(b))['retest_cycle_count'] == 2


def test_gap_does_not_bridge_an_old_confirmation(tmp_path):
    store = Store(tmp_path/'gap.sqlite3', {'test': 'gap'})
    observer = Observer(store, NoSourceHints())
    for bar in bars():
        if bar['ts'] == 48*60:
            continue
        observer.push([bar])
    # All prior market zones were invalidated technically by the missing bar,
    # not reintroduced from a source annotation or reset at the next retest.
    assert not any((z['lower'], z['upper']) == (29787.5, 29804.25)
                   for z in memory(store)['ifvg_observations'].values())
