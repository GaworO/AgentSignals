"""Read-only, bounded reporting of the existing passive observer journal.

No observer, feed, broker, credential or provider is instantiated here. Reports
are projections; the source journals and frozen runtime are never modified.
"""
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import time
from zoneinfo import ZoneInfo

VERSION = 'prospective-session-report-v1'
SYMBOLS = ('NQ', 'MNQ', 'ES')


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _stamp(value):
    if value is None:
        return None
    return {'epoch': value, 'utc': datetime.fromtimestamp(value, timezone.utc).isoformat(),
            'new_york': datetime.fromtimestamp(value, ZoneInfo('America/New_York')).isoformat(),
            'berlin': datetime.fromtimestamp(value, ZoneInfo('Europe/Berlin')).isoformat()}


@contextmanager
def _read(path, seconds=8):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    deadline = time.monotonic() + seconds
    db.execute('PRAGMA query_only=ON')
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    try:
        db.execute('BEGIN')
        yield db
    finally:
        db.rollback()
        db.close()


def _latency(values):
    return {'count': len(values), 'median_seconds': statistics.median(values) if values else None,
            'maximum_seconds': max(values) if values else None,
            'minimum_seconds': min(values) if values else None}


def collect_report(run_dir, *, now=None, event_sink=None, candidate_limit=100):
    """Read two short local SQLite snapshots. event_sink receives every compact
    event record; summary samples are bounded, counts always cover the full run.
    Receipt times and durable receipts, rather than report time, prove prospectivity.
    """
    now = time.time() if now is None else float(now)
    run = Path(run_dir).resolve()
    manifest_raw = (run / 'manifest.json').read_bytes()
    manifest = json.loads(manifest_raw)
    if manifest.get('mode') != 'PROSPECTIVE_SHADOW':
        raise ValueError('SEPARATE_PROSPECTIVE_JOURNAL_REQUIRED')
    if manifest.get('orders_enabled') is not False or manifest.get('paid_calls_enabled') is not False:
        raise ValueError('NO_ORDER_NO_PROVIDER_MANIFEST_REQUIRED')
    status = json.loads((run / 'status.json').read_text())
    start = manifest['prospective_after_bar_start']
    stop = float(manifest['end'])
    cutoff = min(now, stop)
    violations = []
    receipts = {}
    prospective = set()
    all_batches = 0
    counts = Counter()
    candidate_rows = {}
    latest = {}
    outcome_counts = Counter()
    confirmation_counts = Counter()
    missing = Counter()
    missing_links = 0
    event_counts = Counter()
    event_by_symbol = defaultdict(Counter)
    event_by_session = defaultdict(Counter)
    event_samples = []
    event_digest = hashlib.sha256()
    m1_sweeps = []
    candidate_stages = Counter()
    with _read(run / 'observer.sqlite3') as db:
        stored_manifest = db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()
        if not stored_manifest or json.loads(stored_manifest[0]) != manifest:
            raise ValueError('MANIFEST_JOURNAL_MISMATCH')
        for key, raw, intent_raw in db.execute("SELECT d.key,d.payload,i.payload FROM passive_log d JOIN passive_log i ON i.kind='INTENT' AND i.key=d.key WHERE d.kind='DURABLE' ORDER BY d.rowid"):
            receipt, intent = json.loads(raw), json.loads(intent_raw)
            if receipt['commit_observed_at_ns'] / 1e9 > cutoff:
                continue
            all_batches += 1
            bar_start = intent['bars'][0]['ts']
            receipt['bar_start'] = bar_start
            receipt['source_first_received'] = intent['source_first_received']
            receipt['session'] = intent['session']
            receipt['source_receipts'] = intent['source_receipts']
            receipts[key] = receipt
            if receipt.get('orders_sent', 0) != 0:
                violations.append('UNEXPECTED_RECORDED_ORDER:' + key)
            timely = receipt.get('prospective_timing_verified') is True
            if timely and not (bar_start >= start and receipt.get('mode') == 'PROSPECTIVE_SHADOW'
                               and not receipt.get('recovered_after_interruption')
                               and receipt['commit_observed_at_ns'] < (bar_start + 120) * 10**9):
                violations.append('INCONSISTENT_PROSPECTIVE_RECEIPT:' + key)
                timely = False
            if timely:
                prospective.add(key)
        # Detector events carry their own instrument's availability timestamp;
        # the paired batch uses the maximum receipt across all three instruments.
        # Joining only to snapshot_as_of would silently omit faster instruments.
        available = {(symbol, math.ceil(received)): (key, r)
                     for key, r in receipts.items() if r['bar_start'] >= start
                     for symbol, received in r['source_receipts'].items()}
        for raw, in db.execute("SELECT payload FROM shadow_records WHERE stage='DETECTED' ORDER BY as_of,id"):
            r = json.loads(raw)
            if r.get('persistence_batch') not in receipts:
                continue
            counts['all_detected'] += 1
            if r['persistence_batch'] in prospective:
                candidate_rows[r['candidate_id']] = r
        for raw, in db.execute('SELECT payload FROM shadow_records ORDER BY as_of,id'):
            r = json.loads(raw)
            if r['candidate_id'] not in candidate_rows or r.get('persistence_batch') not in receipts:
                continue
            d = receipts[r['persistence_batch']]
            candidate_stages[r['stage']] += 1
            if r.get('orders_enabled') or r.get('execution_eligible'):
                violations.append('UNEXPECTED_EXECUTION_FLAG:' + r['candidate_id'])
            if r['stage'] == 'ASSESSMENT':
                latest[r['candidate_id']] = r
                if r['persistence_batch'] in prospective:
                    counts['timely_assessments'] += 1
                if not db.execute('SELECT 1 FROM shadow_evidence WHERE id=?', (r['evidence_packet_id'],)).fetchone():
                    missing_links += 1
            if r['stage'] == 'OUTCOME':
                if r.get('prospective_outcome_eligible'):
                    prior = r.get('forecast_commit_observed_at_ns')
                    first = r.get('outcome_first_source_receipt')
                    if prior is None or first is None or prior >= int(first * 10**9) or first != d['source_first_received']:
                        violations.append('OUTCOME_TIMING_NOT_PROVEN:' + r['candidate_id'])
                        continue
                    outcome_counts[r['status']] += 1
                    confirmation_counts.update(r.get('added_confirmations', []))
                    counts['source_setup_confirmations'] += int(r.get('source_setup_confirmed') is True)
                else:
                    counts['unproven_or_catchup_outcomes_excluded'] += 1
        for ident, at, symbol, kind, raw in db.execute('SELECT id,as_of,symbol,kind,payload FROM events WHERE as_of>=? AND as_of<=? ORDER BY as_of,id', (start + 60, cutoff)):
            if (symbol, at) not in available:
                counts['events_not_yet_joined_to_durable_batch'] += 1
                continue  # no completed observation transaction at this checkpoint
            key, durable = available[symbol, at]
            e = json.loads(raw)
            level_tf = max(e.get('timeframe', 0), e.get('level_timeframe', 0))
            selection = ('M1_REFERENCE_BELOW_RESEARCH_SELECTOR_MINIMUM_M5' if kind == 'SWEEP' and level_tf < 5
                         else 'DETECTOR_OBSERVATION_NOT_SOURCE_QUALIFICATION')
            row = {'id': ident, 'as_of': at, 'symbol': symbol, 'kind': kind,
                   'timeframe': e.get('timeframe'), 'level_timeframe': e.get('level_timeframe'),
                   'direction': e.get('direction'), 'session': durable['session'],
                   'prospective_timing_verified': key in prospective, 'persistence_batch': key,
                   'selection_note': selection, 'observation_sha256': _sha(raw.encode()),
                   'evidence_ids': e.get('evidence_ids', []), 'payload': e}
            encoded = _encoded(row) + '\n'
            event_digest.update(encoded.encode())
            if event_sink:
                event_sink(encoded)
            event_counts[kind] += 1
            event_by_symbol[symbol][kind] += 1
            event_by_session[durable['session']][kind] += 1
            counts['timely_detector_events'] += int(key in prospective)
            if len(event_samples) < 30:
                event_samples.append({k: v for k, v in row.items() if k != 'payload'})
            if kind == 'SWEEP' and level_tf < 5:
                counts['m1_sweeps_excluded_by_selector'] += 1
                if len(m1_sweeps) < 100:
                    m1_sweeps.append({k: v for k, v in row.items() if k != 'payload'})
        gap_rows = [json.loads(raw) for raw, in db.execute("SELECT payload FROM passive_log WHERE kind='SOURCE_GAP' AND CAST(json_extract(payload,'$.ts') AS INTEGER)>=? AND CAST(json_extract(payload,'$.ts') AS INTEGER)<? ORDER BY key", (start, cutoff))]
        starts = [json.loads(raw) for raw, in db.execute("SELECT payload FROM passive_log WHERE kind='PROCESS_START' ORDER BY rowid")]
        unreceipted = db.execute("SELECT count(*) FROM passive_log a WHERE kind='INTENT' AND NOT EXISTS(SELECT 1 FROM passive_log b WHERE b.kind='DURABLE' AND b.key=a.key)").fetchone()[0]
        duplicate_checks = {}
        for table, keys in (('passive_log', 'kind,key'), ('shadow_records', 'candidate_id,as_of,stage'), ('bars', 'symbol,contract,ts'), ('events', 'id')):
            duplicate_checks[table] = db.execute('SELECT count(*) FROM (SELECT ' + keys + ' FROM ' + table + ' GROUP BY ' + keys + ' HAVING count(*)>1)').fetchone()[0]
        reviews = db.execute('SELECT count(*) FROM ai_reviews').fetchone()[0]
        latest_cursor = db.execute('SELECT max(ts) FROM bars').fetchone()[0]
    feeds = {}
    newly_received = 0
    with _read(run / 'intake-mirror.sqlite3') as db:
        for symbol in SYMBOLS:
            table = 'collection_bars' if symbol == 'NQ' else 'bars'
            latency, starts_seen, receipt_times, close_times = [], [], [], []
            for ts, end, received in db.execute('SELECT start,end,received FROM ' + table + ' WHERE symbol=? AND start>=? AND received<=? ORDER BY start', (symbol, start, cutoff)):
                starts_seen.append(ts); receipt_times.append(received); close_times.append(end); latency.append(received - end)
            newly_received += len(latency)
            feeds[symbol] = {'newly_received_bars': len(latency), 'first_bar_start': _stamp(starts_seen[0]) if starts_seen else None,
                             'latest_close': _stamp(close_times[-1]) if close_times else None,
                             'first_actual_receiver_timestamp': _stamp(receipt_times[0]) if receipt_times else None,
                             'latest_actual_receiver_timestamp': _stamp(receipt_times[-1]) if receipt_times else None,
                             'close_to_receipt': _latency(latency),
                             'observed_nonconsecutive_intervals': sum(b != a + 60 for a, b in zip(starts_seen, starts_seen[1:])),
                             'continuous_contract': True, 'entitlement': 'NOT_VERIFIED'}
    candidate_by_symbol = Counter({s: 0 for s in SYMBOLS})
    candidate_by_model, candidate_by_session, decisions, hypothesis = Counter(), Counter(), Counter(), Counter()
    summaries = []
    for ident, original in candidate_rows.items():
        r = latest.get(ident, original)
        d = receipts[original['persistence_batch']]
        candidate_by_symbol[original['symbol']] += 1
        candidate_by_model[original['model']] += 1
        candidate_by_session[original['session']] += 1
        decisions[r.get('decision', 'DETECTED')] += 1
        hypothesis[r.get('hypothesis_status', 'UNASSESSED')] += 1
        missing.update(r.get('missing_requirements', []))
        if len(summaries) < candidate_limit:
            summaries.append({'candidate_id': ident, 'symbol': original['symbol'], 'direction': original['direction'],
                              'model': original['model'], 'model_scope': 'ENGINEERING_RESEARCH_NOT_SOURCE_QUALIFIED_ICT_MODEL',
                              'origin_session': original['session'], 'detected_at': _stamp(original['as_of']),
                              'decision_committed_no_later_than': _stamp(d['commit_observed_at_ns'] / 1e9),
                              'latest_at': _stamp(r['as_of']), 'decision': r.get('decision', 'DETECTED'),
                              'hypothesis_status': r.get('hypothesis_status'), 'evidence_packet_id': r['evidence_packet_id'],
                              'evidence_ids': r.get('evidence_ids', []), 'source_rule_ids': r.get('source_rule_ids', []),
                              'source_qualified': r.get('source_qualified', False), 'missing_requirements': r.get('missing_requirements', []),
                              'semantic_status': r.get('semantic_status', 'PENDING_LLM'),
                              'next_confirmation': r.get('next_confirmation', []), 'invalidation': r.get('invalidation'),
                              'chart_url': '/ict-shadow?view=ictai&shadow_run=prospective&shadow_candidate=' + ident})
    phase = status.get('operational_state', 'UNKNOWN')
    terminal = phase == 'COMPLETED_BOUNDED_RUN' and status.get('heartbeat_at', 0) >= stop and status.get('stop_at') == stop
    final = now >= stop and terminal and unreceipted == 0 and not violations
    label = 'FINAL_COMPLETED_SESSION' if final else 'INTERIM_SESSION_CHECKPOINT' if now < stop else 'INCOMPLETE_SESSION_REPORT'
    return {'version': VERSION, 'report_status': label, 'is_final': final, 'generated_at': _stamp(now), 'cutoff': _stamp(cutoff),
            'authorized_stop': _stamp(stop), 'activation_bar_start': _stamp(start), 'operational_state': phase,
            'status_heartbeat': _stamp(status.get('heartbeat_at')), 'status_age_seconds': now - status.get('heartbeat_at', now),
            'mode': 'PROSPECTIVE_SHADOW', 'feed_classification': status.get('feed_classification', 'FEED_UNVERIFIED'),
            'llm_status': 'PENDING_LLM', 'manifest_sha256': _sha(manifest_raw),
            'bars': {'newly_received_total': newly_received, 'timely_prospective_total': len(prospective) * 3,
                     'timely_paired_batches': len(prospective), 'all_durable_batches_including_warmup': all_batches, 'feeds': feeds,
                     'latest_engine_bar_start': _stamp(latest_cursor), 'boundary_policy': 'Only receipts before the authorized deadline; the candle closing exactly at stop may arrive afterward and is not presumed received.'},
            'events': {'total': sum(event_counts.values()), 'timely_total': counts['timely_detector_events'], 'by_kind': dict(event_counts),
                       'not_yet_joined_to_durable_batch': counts['events_not_yet_joined_to_durable_batch'],
                       'by_instrument': {s: dict(event_by_symbol[s]) for s in SYMBOLS}, 'by_session': dict(event_by_session),
                       'inventory_sha256': event_digest.hexdigest(), 'bounded_sample': event_samples,
                       'm1_sweeps_excluded_by_selector': counts['m1_sweeps_excluded_by_selector'], 'm1_sweep_sample': m1_sweeps},
            'candidates': {'total': len(candidate_rows), 'catchup_excluded': counts['all_detected'] - len(candidate_rows),
                           'by_instrument': dict(candidate_by_symbol), 'by_model': dict(candidate_by_model), 'by_origin_session': dict(candidate_by_session),
                           'latest_decisions': dict(decisions), 'hypothesis_statuses': dict(hypothesis), 'stages': dict(candidate_stages),
                           'timely_assessments': counts['timely_assessments'], 'source_qualified': sum(r.get('source_qualified') is True for r in latest.values()),
                           'missing_requirements': dict(missing), 'rows': summaries, 'rows_truncated': len(candidate_rows) > candidate_limit},
            'outcomes': {'eligible_observations': sum(outcome_counts.values()), 'statuses': dict(outcome_counts),
                         'added_confirmations': dict(confirmation_counts), 'source_setup_confirmations': counts['source_setup_confirmations'],
                         'unproven_or_catchup_excluded': counts['unproven_or_catchup_outcomes_excluded'], 'fills': None, 'pnl': None},
            'gaps': {'recorded_count': len(gap_rows), 'records': gap_rows[:100], 'records_truncated': len(gap_rows) > 100},
            'recovery': {'process_starts': len(starts), 'last_recovered_cursor': starts[-1].get('recovered_cursor') if starts else None,
                         'unreceipted_intents': unreceipted, 'duplicate_key_counts': duplicate_checks,
                         'missing_evidence_packets': missing_links, 'prior_restart_verified_without_new_restart': len(starts) > 1 and unreceipted == 0 and not any(duplicate_checks.values())},
            'safety': {'recorded_llm_reviews': reviews, 'manifest_paid_calls_enabled': False, 'manifest_orders_enabled': False,
                       'broker_transport': status.get('broker_transport'), 'semantic_or_timing_violations': violations,
                       'source_qualification_is_not_geometry': True, 'no_fill_or_profit_inference': True},
            'limitations': ['CME real-time entitlement remains unverified.', 'Engineering selector retains M5-or-higher reference threshold.',
                            'Earlier completed D1 context is not present in this bounded run warmup.', 'Retrieved source rules are not source-qualified setups.',
                            'Observer is local and stops at the authorized deadline; persistent hosting requires separate approval.']}


def write_report(run_dir, output_dir, *, now=None):
    """Content-addressed checkpoint + replaceable convenience pointer, outside run.
    Calling repeatedly never changes an existing checkpoint or journal.
    """
    run, output = Path(run_dir).resolve(), Path(output_dir).resolve()
    if output == run or output.is_relative_to(run):
        raise ValueError('REPORT_OUTPUT_MUST_BE_OUTSIDE_ACTIVE_RUN')
    output.mkdir(parents=True, exist_ok=True)
    temporary = output / ('.events-' + str(os.getpid()) + '.tmp')
    try:
        with temporary.open('x') as handle:
            report = collect_report(run, now=now, event_sink=handle.write)
            handle.flush(); os.fsync(handle.fileno())
        events_name = 'events-' + report['events']['inventory_sha256'] + '.jsonl'
        events_path = output / events_name
        if events_path.exists():
            if _sha(events_path.read_bytes()) != report['events']['inventory_sha256']:
                raise ValueError('IMMUTABLE_EVENT_EXPORT_CONFLICT')
            temporary.unlink()
        else:
            temporary.replace(events_path)
        report['events']['inventory_artifact'] = events_name
        raw = (_encoded(report) + '\n').encode()
        report_hash = _sha(raw)
        name = ('final-' if report['is_final'] else 'checkpoint-') + report_hash + '.json'
        path = output / name
        if path.exists():
            if path.read_bytes() != raw:
                raise ValueError('IMMUTABLE_REPORT_CONFLICT')
        else:
            with path.open('xb') as handle:
                handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        pointer = output / ('.latest-' + str(os.getpid()) + '.tmp')
        pointer.write_bytes(raw)
        pointer.replace(output / 'latest.json')
        return report, path
    finally:
        if temporary.exists():
            temporary.unlink()
