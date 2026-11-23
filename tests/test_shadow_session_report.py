import json
from pathlib import Path
import sqlite3

import pytest

from services.tanja.ict_agent.shadow_session_report import collect_report, write_report


def fixture_run(tmp_path):
    run = tmp_path / 'run'; run.mkdir()
    manifest = {'mode': 'PROSPECTIVE_SHADOW', 'orders_enabled': False, 'paid_calls_enabled': False,
                'prospective_after_bar_start': 1200, 'end': 1800}
    (run / 'manifest.json').write_text(json.dumps(manifest))
    state = {'operational_state': 'PROSPECTIVE_SHADOW_RUNNING', 'heartbeat_at': 1500,
             'stop_at': 1800, 'feed_classification': 'FEED_UNVERIFIED', 'broker_transport': 'ABSENT'}
    (run / 'status.json').write_text(json.dumps(state))
    with sqlite3.connect(run / 'observer.sqlite3') as db:
        db.executescript('''CREATE TABLE metadata(key TEXT,payload TEXT);
        CREATE TABLE passive_log(kind TEXT,key TEXT,payload TEXT);
        CREATE TABLE shadow_records(id TEXT,candidate_id TEXT,as_of INTEGER,stage TEXT,payload TEXT);
        CREATE TABLE shadow_evidence(id TEXT,payload TEXT);
        CREATE TABLE events(id TEXT,as_of INTEGER,symbol TEXT,kind TEXT,payload TEXT);
        CREATE TABLE bars(symbol TEXT,contract TEXT,ts INTEGER);
        CREATE TABLE ai_reviews(id TEXT);''')
        db.execute('INSERT INTO metadata VALUES(?,?)', ('manifest', json.dumps(manifest)))
        for key, ts, timely in [('old', 1140, False), ('new', 1200, True), ('next', 1260, True)]:
            first = ts + 61.0
            intent = {'bars': [{'ts': ts}], 'source_first_received': first,
                      'source_receipts': {'NQ': first, 'MNQ': first+1, 'ES': first}, 'session': 'NYAM'}
            durable = {'snapshot_as_of': ts + 62, 'commit_observed_at_ns': (ts + 65) * 10**9,
                       'prospective_timing_verified': timely, 'mode': 'PROSPECTIVE_SHADOW' if timely else 'CATCHUP_REPLAY',
                       'recovered_after_interruption': False, 'orders_sent': 0}
            db.execute('INSERT INTO passive_log VALUES(?,?,?)', ('INTENT', key, json.dumps(intent)))
            db.execute('INSERT INTO passive_log VALUES(?,?,?)', ('DURABLE', key, json.dumps(durable)))
            for symbol in ('NQ', 'MNQ', 'ES'):
                db.execute('INSERT INTO bars VALUES(?,?,?)', (symbol, symbol+'1!', ts))
        for i in range(2):
            db.execute('INSERT INTO passive_log VALUES(?,?,?)', ('PROCESS_START', str(i), json.dumps({'recovered_cursor': 1200 if i else None})))
        for ident, key, asof in [('catchup', 'old', 1202), ('candidate', 'new', 1262)]:
            base = {'candidate_id': ident, 'persistence_batch': key, 'as_of': asof, 'symbol': 'NQ',
                    'direction': 'LONG', 'model': 'research-geometry', 'session': 'NYAM',
                    'evidence_packet_id': 'ep', 'source_rule_ids': ['rule1'], 'source_qualified': False,
                    'execution_eligible': False, 'orders_enabled': False, 'semantic_status': 'PENDING_LLM'}
            for stage, more in [('DETECTED', {}), ('ASSESSMENT', {'decision': 'WAIT', 'missing_requirements': ['SOURCE_CONTEXT']})]:
                row = dict(base, stage=stage, **more)
                db.execute('INSERT INTO shadow_records VALUES(?,?,?,?,?)', (ident+stage, ident, asof, stage, json.dumps(row)))
        db.execute('INSERT INTO shadow_evidence VALUES(?,?)', ('ep', '{}'))
        outcome = dict(base, stage='OUTCOME', persistence_batch='next', as_of=1322,
                       prospective_outcome_eligible=True, forecast_commit_observed_at_ns=1265*10**9,
                       outcome_first_source_receipt=1321.0, status='UNRESOLVED', added_confirmations=['FVG'])
        db.execute('INSERT INTO shadow_records VALUES(?,?,?,?,?)', ('outcome', 'candidate', 1322, 'OUTCOME', json.dumps(outcome)))
        for ident, asof, tf in [('old-event', 1201, 1), ('m1', 1261, 1), ('m5', 1321, 5)]:
            row = {'id': ident, 'available_at': asof, 'timeframe': tf, 'level_timeframe': tf,
                   'symbol': 'NQ', 'kind': 'SWEEP', 'evidence_ids': ['candle'], 'direction': 'LONG'}
            db.execute('INSERT INTO events VALUES(?,?,?,?,?)', (ident, asof, 'NQ', 'SWEEP', json.dumps(row)))
    with sqlite3.connect(run / 'intake-mirror.sqlite3') as db:
        for table in ('bars', 'collection_bars'):
            db.execute('CREATE TABLE '+table+'(symbol TEXT,start INTEGER,end INTEGER,received REAL)')
        for symbol in ('NQ', 'MNQ', 'ES'):
            table = 'collection_bars' if symbol == 'NQ' else 'bars'
            for ts in (1140, 1200, 1260):
                db.execute('INSERT INTO '+table+' VALUES(?,?,?,?)', (symbol, ts, ts+60, ts+61.0))
    return run


def test_only_timely_candidates_and_actual_outcomes(tmp_path):
    run = fixture_run(tmp_path)
    r = collect_report(run, now=1500)
    assert r['report_status'] == 'INTERIM_SESSION_CHECKPOINT'
    assert r['bars']['newly_received_total'] == r['bars']['timely_prospective_total'] == 6
    assert r['candidates']['total'] == 1
    assert r['candidates']['catchup_excluded'] == 1
    assert r['candidates']['latest_decisions'] == {'WAIT': 1}
    assert r['outcomes']['eligible_observations'] == 1
    assert r['outcomes']['added_confirmations'] == {'FVG': 1}
    assert r['events']['total'] == 2
    assert r['events']['m1_sweeps_excluded_by_selector'] == 1
    # Own-instrument receipt=1261 is earlier than paired snapshot_as_of=1262.
    assert r['events']['bounded_sample'][0]['as_of'] == 1261
    assert r['recovery']['prior_restart_verified_without_new_restart'] is True
    assert r['safety']['recorded_llm_reviews'] == 0


def test_cannot_claim_final_without_completed_deadline_receipt(tmp_path):
    run = fixture_run(tmp_path)
    assert not collect_report(run, now=2000)['is_final']
    status = json.loads((run/'status.json').read_text())
    status.update(operational_state='COMPLETED_BOUNDED_RUN', heartbeat_at=1801)
    (run/'status.json').write_text(json.dumps(status))
    assert not collect_report(run, now=1799)['is_final']
    assert collect_report(run, now=2000)['is_final']
    with sqlite3.connect(run/'observer.sqlite3') as db:
        db.execute('INSERT INTO passive_log VALUES(?,?,?)', ('INTENT', 'interrupted', '{}'))
    assert not collect_report(run, now=2000)['is_final']


def test_bad_outcome_timing_never_counts(tmp_path):
    run = fixture_run(tmp_path)
    with sqlite3.connect(run/'observer.sqlite3') as db:
        row = json.loads(db.execute("SELECT payload FROM shadow_records WHERE stage='OUTCOME'").fetchone()[0])
        row['forecast_commit_observed_at_ns'] = 1400*10**9
        db.execute("UPDATE shadow_records SET payload=? WHERE stage='OUTCOME'", (json.dumps(row),))
    r = collect_report(run, now=1500)
    assert r['outcomes']['eligible_observations'] == 0
    assert r['safety']['semantic_or_timing_violations'] == ['OUTCOME_TIMING_NOT_PROVEN:candidate']


def test_report_is_idempotent_and_journal_bytes_unchanged(tmp_path):
    run = fixture_run(tmp_path); output = tmp_path/'reports'
    before = {p.name: p.read_bytes() for p in run.iterdir() if p.is_file()}
    r, first = write_report(run, output, now=1500)
    _, second = write_report(run, output, now=1500)
    assert first == second
    assert before == {p.name: p.read_bytes() for p in run.iterdir() if p.is_file()}
    assert len((output/r['events']['inventory_artifact']).read_text().splitlines()) == 2
    assert len(list(output.glob('checkpoint-*.json'))) == 1
    assert json.loads((output/'latest.json').read_text()) == r


def test_refuses_reports_inside_active_journal(tmp_path):
    run = fixture_run(tmp_path)
    with pytest.raises(ValueError, match='OUTSIDE_ACTIVE_RUN'):
        write_report(run, run/'reports', now=1500)


def test_false_prospective_timing_does_not_promote_candidate(tmp_path):
    run = fixture_run(tmp_path)
    with sqlite3.connect(run/'observer.sqlite3') as db:
        row = json.loads(db.execute("SELECT payload FROM passive_log WHERE kind='DURABLE' AND key='new'").fetchone()[0])
        row['commit_observed_at_ns'] = 1400*10**9
        db.execute("UPDATE passive_log SET payload=? WHERE kind='DURABLE' AND key='new'", (json.dumps(row),))
    r = collect_report(run, now=1500)
    assert r['candidates']['total'] == 0
    assert r['bars']['timely_prospective_total'] == 3
    assert 'INCONSISTENT_PROSPECTIVE_RECEIPT:new' in r['safety']['semantic_or_timing_violations']
