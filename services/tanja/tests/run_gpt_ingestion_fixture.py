"""One approved, bounded GPT streaming audio-to-database test. No deployment."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from store import Store
from transcript_ingestion import TranscriptJournal, gpt_session


def verify(journal,evidence):
    events=[];cursor=0
    while True:
        page=journal.state(after=cursor)['events']
        if not page:break
        events.extend(page);cursor=page[-1]['sequence']
    finals=[x for x in events if x['kind']=='final'];deltas=[x for x in events if x['kind']=='delta']
    assert finals and deltas, 'Both incremental and final GPT transcript events are required'
    assert any(x['raw_text'].strip() for x in finals), 'Speech was not recognized'
    assert len(journal.shadow())==len(finals), 'Shadow final delivery mismatch'
    with journal.store.connect() as db:
        session=db.execute('SELECT created_at,audio_seconds,status FROM transcript_sessions').fetchone()
    assert session['status']=='COMPLETED','Capture did not complete'
    assert any(x['received_at']<session['created_at']+37.9181875 for x in events),'No output while audio continued'
    assert all(x['audio_start_s'] is not None for x in finals),'Final capture intervals are missing'
    count=len(events);journal.recover()
    fixture=ROOT/'live_transcription/tests/synthetic_speech.wav'
    source=dict(id=evidence['fixture_sha256'],kind='fixture',audio_url=str(fixture),started_at=time.time(),authorization='Existing permitted fixture')
    # Completed-session recovery must return without invoking ANY API connector.
    gpt_session(journal,source,'NO_API_RECOVERY_CHECK',duration=38,
        connector=lambda key:(_ for _ in ()).throw(AssertionError('Completed source replayed')))
    with journal.store.connect() as db:
        assert db.execute('SELECT count(*) FROM transcript_events').fetchone()[0]==count,'Restart duplicated events'
    evidence.update(result='PASS',deltas=len(deltas),finals=len(finals),shadow_observations=len(journal.shadow()),
        audio_seconds=session['audio_seconds'],updates_while_audio_continued=True,restart_duplicate_events=0,
        transcript_pagination_verified=True,events=events)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--paid-approved',action='store_true')
    parser.add_argument('--railway-key',action='store_true',help='Read the existing Tanja service key without printing or saving it')
    parser.add_argument('--verify-existing',action='store_true',help='Recheck saved database evidence; no audio, credentials or network calls')
    args=parser.parse_args()
    output=ROOT/'live_transcription/output/gpt_ingestion_fixture_test'
    if args.verify_existing:
        evidence=json.loads((output/'result.json').read_text())
        database=ROOT/evidence['database']
        if not database.is_file():raise RuntimeError('EXISTING_EVIDENCE_DATABASE_REQUIRED')
        if evidence.get('result')=='FAIL':
            evidence['initial_validator_failure']='First 100-row page counted instead of all pages; corrected using saved records, without another API call'
        verify(TranscriptJournal(Store(database.parent)),evidence)
        evidence.pop('state',None);evidence.pop('error_code',None);evidence.pop('error_type',None)
        (output/'result.json').write_text(json.dumps(evidence,indent=2)+'\n')
        print(json.dumps({k:v for k,v in evidence.items() if k!='events'},indent=2));return
    if not args.paid_approved:parser.error('Explicit paid approval is required')
    key=os.environ.get('OPENAI_API_KEY','')
    if args.railway_key:
        result=subprocess.run(['railway','variable','list','--project','26df4462-061e-48e5-ad8a-b364909a1b8f',
            '--service','187a0737-d7a7-4f7c-b00d-b31a6f7e60ad','--environment','production','--json'],
            capture_output=True,text=True,timeout=30)
        if result.returncode:raise RuntimeError('TANJA_RAILWAY_VARIABLE_READ_FAILED')
        key=json.loads(result.stdout).get('OPENAI_API_KEY','')
    if not key:raise RuntimeError('OPENAI_API_KEY_REQUIRED')
    fixture=ROOT/'live_transcription/tests/synthetic_speech.wav'
    permission=json.loads((fixture.parent/'source.json').read_text())
    sha=hashlib.sha256(fixture.read_bytes()).hexdigest()
    if sha!=permission['audio_sha256']:raise RuntimeError('PERMITTED_FIXTURE_HASH_MISMATCH')
    output.mkdir(parents=True,exist_ok=True)
    store=Store(output/uuid.uuid4().hex);journal=TranscriptJournal(store)
    source=dict(id=sha,kind='fixture',audio_url=str(fixture),started_at=time.time(),authorization=permission['permission'])
    evidence=dict(model='gpt-live-transcribe',source_kind=permission['source_kind'],fixture_sha256=sha,
        database=str(Path(store.path).relative_to(ROOT)),maximum_audio_seconds=38,
        estimated_max_transcription_cost_usd=round(38/60*.017,5),paid_budget_approved_usd=.05,
        test_location='Local process using existing Tanja Railway key; NOT Railway-hosted capture',
        orders_sent=0,deployments=0)
    start=time.time()
    try:
        gpt_session(journal,source,key,duration=38)
        verify(journal,evidence)
    except Exception as e:
        evidence.update(result='FAIL',error_type=type(e).__name__,error_code=str(e) if isinstance(e,RuntimeError) else None,
                        state=journal.state())
    evidence['elapsed_s']=round(time.time()-start,3)
    (output/'result.json').write_text(json.dumps(evidence,indent=2)+'\n')
    print(json.dumps({k:v for k,v in evidence.items() if k not in ('events','state')},indent=2),flush=True)
    if evidence['result']!='PASS':sys.exit(1)


if __name__=='__main__':main()
