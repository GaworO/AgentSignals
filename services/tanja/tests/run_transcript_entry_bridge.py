"""Actual historical-entry rejection and one optional synthetic TradersPost TEST receipt.

No audio capture, GPT call, broker order, Railway mutation or deployment.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from store import Store
from transcript_ingestion import TranscriptJournal, encoded
from transcript_trade_interpreter import Interpreter
from transcript_entry_bridge import EntryBridge

SYNTHETIC_QUOTE = 'I placed my limit order: buy 1 MNQ, limit at 20000, SL at 19990, TP at 20020.'


def configuration(railway):
    keys = ('TANJA_TRADERSPOST_TEST_WEBHOOK_URL', 'TANJA_TEST_CONTRACT')
    config = {key: os.environ.get(key, '') for key in keys}
    if railway:
        result = subprocess.run(['railway', 'variable', 'list',
            '--project', '26df4462-061e-48e5-ad8a-b364909a1b8f',
            '--service', '187a0737-d7a7-4f7c-b00d-b31a6f7e60ad',
            '--environment', 'production', '--json'], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError('TANJA_TEST_CONFIGURATION_READ_FAILED')
        variables = json.loads(result.stdout)
        config = {key: variables.get(key, '') for key in keys}
    return config


def record(journal, session, attempt, event, source_kind):
    item = 'decision:' + event['event_id']
    if attempt is not None:
        journal.record_decision(session, attempt, event)
    if source_kind == 'synthetic_fixture':
        with journal.store.connect() as db:
            row = db.execute('SELECT payload FROM shadow_transcript_observations WHERE session_id=? AND item_id=?', (session,item)).fetchone()
            observation = json.loads(row[0])
            observation['evidence_status'] = 'SYNTHETIC_TEXT_FIXTURE_NOT_TANJA_NOT_BROKER_VERIFIED'
            db.execute('UPDATE shadow_transcript_observations SET payload=? WHERE session_id=? AND item_id=?', (encoded(observation),session,item))
    return item


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--railway-config', action='store_true', help='Read existing dedicated test webhook and contract; never print credentials')
    parser.add_argument('--contract', help='Override preview contract explicitly')
    parser.add_argument('--send-test', action='store_true', help='Send one synthetic entry with test:true; never a broker order')
    parser.add_argument('--output-dir', type=Path, default=ROOT/'live_transcription/output/entry_bridge_demo')
    args = parser.parse_args()
    config = configuration(args.railway_config)
    contract = args.contract or config['TANJA_TEST_CONTRACT']
    directory = args.output_dir
    directory.mkdir(parents=True, exist_ok=True)
    journal = TranscriptJournal(Store(directory/'journal'))
    bridge = EntryBridge(journal, contract, config['TANJA_TRADERSPOST_TEST_WEBHOOK_URL'])
    historical = ROOT/'live_transcription/output/transcript_trade_demo/events.jsonl'
    source = dict(id='entry-bridge-historical-v1', audio_url=str(historical), started_at=0,
                  authorization='Existing user-authorized historical transcript replay')
    session, attempt, _ = journal.start(source, 'historical_transcript')
    historical_previews = []
    for line in historical.read_text().splitlines():
        event = json.loads(line)
        item = record(journal,session,attempt,event,'historical_transcript')
        historical_previews.append(bridge.preview(session,item,event,None,'historical_transcript'))
    if attempt:
        journal.finish(session,attempt,'COMPLETED')

    # These exact prices/quantity are test data, never source-derived live parameters.
    source = dict(id='entry-bridge-synthetic-text-v1', audio_url='synthetic-text:entry-bridge-v1',
                  started_at=time.time(), authorization='Original synthetic text solely for no-order transport validation')
    session, attempt, _ = journal.start(source, 'synthetic_fixture')
    captured_at = time.time()
    statement = dict(quote=SYNTHETIC_QUOTE, source_seconds=0, source_timestamp='00:00:00', source_line=1, sentence_index=0)
    event = Interpreter().interpret(statement,hashlib.sha256(SYNTHETIC_QUOTE.encode()).hexdigest())[0]
    event.update(historical_replay=False, executable=False, orders_enabled=False,
                 timestamp_basis='SYNTHETIC_TEXT_TEST_NOT_AUDIO_TIMING')
    item = record(journal,session,attempt,event,'synthetic_fixture')
    preview = bridge.preview(session,item,event,captured_at,'synthetic_fixture')
    delivery = None
    if preview['payload']:
        (directory/'synthetic_test_payload.json').write_text(json.dumps(preview['payload'],indent=2)+'\n')
    if args.send_test:
        try:
            delivery = bridge.deliver_test(session,event['event_id'])
        except ValueError as exc:
            delivery = dict(status='blocked',error=str(exc),orders_enabled=False,test_only=True)
    if attempt:
        journal.finish(session,attempt,'COMPLETED')
    with journal.store.connect() as db:
        shadow = [json.loads(row[0]) for row in db.execute('SELECT payload FROM shadow_transcript_observations ORDER BY event_sequence')]
    report = dict(mode='LOCAL_PREVIEW_AND_OPTIONAL_TRADERSPOST_TEST_ONLY', contract=contract,
        historical_events=len(historical_previews), historical_statuses=dict(Counter(x['status'] for x in historical_previews)),
        historical_previews=historical_previews, synthetic_fixture_quote=SYNTHETIC_QUOTE,
        synthetic_preview=preview, test_delivery=delivery, shadow_observations=len(shadow),
        database=journal.store.path, orders_enabled=False, broker_orders_sent=0,
        paid_api_calls=0, railway_changes=0, deployments=0,
        limitations=['Synthetic entry tests text interpretation and webhook delivery, not GPT recognition of a real Tanja entry',
            'Historical entries lack complete executable order fields; no ES/NQ to MNQ conversion',
            'Test receipt is not account-routing or broker-fill verification',
            'Live capture creates previews only; account sizing and live execution are disabled'])
    (directory/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    (directory/'shadow_observations.jsonl').write_text(''.join(encoded(x)+'\n' for x in shadow))
    print(json.dumps({key:value for key,value in report.items() if key not in ('historical_previews','synthetic_preview')},indent=2),flush=True)
    print('Report: '+str(directory/'result.json'),flush=True)
    return int(bool(args.send_test and (not delivery or delivery['status'] != 'test_received')))


if __name__ == '__main__':
    sys.exit(main())
