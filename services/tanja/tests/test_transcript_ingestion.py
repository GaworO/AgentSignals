"""Focused storage/transport tests plus actual permitted audio -> local ASR -> DB.

LocalSpeechSocket emulates ONLY the GPT wire protocol; recognition uses cached
base.en, not GPT. No canned transcripts/manual labels enter recognition.
"""
import base64
import hashlib
import importlib.util
import io
import json
import queue
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from store import Store
from transcript_ingestion import TranscriptJournal, gpt_session, secure_audio_url, MODEL


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name); self.j=TranscriptJournal(self.store)
        self.source=dict(id='session-a',audio_url='/permitted-fixture.wav',started_at=time.time(),authorization='test fixture')
        self.sid,self.attempt,_=self.j.start(self.source,'fixture')
    def tearDown(self):self.tmp.cleanup()
    def event(self,typ='delta',text='hold',eid='e1',item='item1'):
        d=dict(type='conversation.item.input_audio_transcription.'+('completed' if typ=='final' else 'delta'),event_id=eid,item_id=item)
        d['transcript' if typ=='final' else 'delta']=text
        return d
    def test_exact_duplicate_and_repeated_spoken_words(self):
        e=self.event();self.assertTrue(self.j.ingest(self.sid,self.attempt,e));self.assertFalse(self.j.ingest(self.sid,self.attempt,e))
        self.assertTrue(self.j.ingest(self.sid,self.attempt,self.event(eid='e2')))
        self.assertEqual([x['raw_text'] for x in self.j.state()['events']],['hold','hold'])
        self.assertEqual(self.j.shadow(),[])
    def test_conflicting_duplicate(self):
        self.j.ingest(self.sid,self.attempt,self.event())
        with self.assertRaisesRegex(ValueError,'CONFLICTING'):self.j.ingest(self.sid,self.attempt,self.event(text='buy'))
    def test_restart_final_dedup_and_shadow(self):
        e=self.event('final','I have my limit in.','final1')
        self.j.ingest(self.sid,self.attempt,e,(0,5)); self.j.checkpoint(self.sid,5)
        restarted=TranscriptJournal(self.store);restarted.recover()
        sid,attempt,offset=restarted.start(self.source,'fixture')
        self.assertEqual(sid,self.sid);self.assertEqual(offset,5)
        self.assertFalse(restarted.ingest(sid,attempt,e,(0,5)))
        self.assertFalse(restarted.ingest(sid,attempt,self.event('final','I have my limit in.','new-provider-id')))
        obs=restarted.shadow();self.assertEqual(len(obs),1)
        self.assertFalse(obs[0]['executable']);self.assertEqual(obs[0]['order_state'],'UNKNOWN')
        self.assertEqual(obs[0]['audio_end_s'],5)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM transcript_attempts WHERE id=?',(self.attempt,)).fetchone()[0],'INTERRUPTED')
    def test_out_of_order_completion_and_cursor(self):
        self.j.ingest(self.sid,self.attempt,self.event('final','second','e2','i2'),(5,10))
        self.j.ingest(self.sid,self.attempt,self.event('final','first','e1','i1'),(0,5))
        rows=self.j.state()['events'];self.assertEqual(len(rows),2)
        self.assertEqual(self.j.state(after=rows[0]['sequence'])['events'][0]['item_id'],'i1')
        self.assertEqual({x['audio_start_s'] for x in self.j.shadow()},{0,5})
    def test_completed_fixture_is_not_replayed(self):
        self.j.finish(self.sid,self.attempt,'COMPLETED')
        self.assertIsNone(self.j.start(self.source,'fixture')[1])
    def test_watch_urls_and_missing_delta_ids_rejected(self):
        for url in ['https://www.youtube.com/watch?v=x','https://r1.googlevideo.com/a','http://provider.test/a']:
            with self.assertRaises(ValueError):secure_audio_url(url)
        e=self.event();del e['event_id']
        with self.assertRaises(ValueError):self.j.ingest(self.sid,self.attempt,e)
    def test_existing_database_and_shadow_interface(self):
        with self.store.connect() as db:db.execute("INSERT INTO metadata VALUES('fixture_preserved','yes')")
        from ai_review import AIReview
        from plan_observer import PlanObserver
        AIReview(self.store,{})
        observer=PlanObserver(self.store)
        self.j.ingest(self.sid,self.attempt,self.event('final','Hold.','f'))
        self.assertEqual(observer.state()['transcript_observations'][0]['raw_text'],'Hold.')
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='fixture_preserved'").fetchone()[0],'yes')
    def test_http_transcript_auth_and_disabled_startup(self):
        from app import App
        app=App(dict(DATA_DIR=self.tmp.name,TANJA_FEED_TOKEN='a'*40,TANJA_DASHBOARD_PASSWORD='test-password-at-least-16'))
        def request(auth=False):
            status=[];env=dict(PATH_INFO='/api/transcripts',REQUEST_METHOD='GET',QUERY_STRING='', **{'wsgi.input':io.BytesIO()})
            if auth:env['HTTP_AUTHORIZATION']='Basic '+base64.b64encode(b'tanja:test-password-at-least-16').decode()
            raw=b''.join(app(env,lambda s,h:status.append(s)))
            return status[0],json.loads(raw)
        self.assertTrue(request()[0].startswith('401'))
        self.assertTrue(request(True)[0].startswith('200'))
        self.assertEqual(app.transcripts.state()['input_status'],'DISABLED')


class LocalSpeechSocket:
    def __init__(self,model):
        self.model=model;self.messages=queue.Queue();self.jobs=queue.Queue();self.buffer=bytearray();self.number=0
        self.closed=False;self.received_bytes=0;self.config=None
        threading.Thread(target=self.recognize,daemon=True).start()
    def send(self,raw):
        e=json.loads(raw)
        if e['type']=='session.update':
            self.config=e;self.messages.put(dict(type='session.updated'));return
        if e['type']=='input_audio_buffer.append':
            b=base64.b64decode(e['audio']);self.buffer.extend(b);self.received_bytes+=len(b);return
        if e['type']=='input_audio_buffer.commit':
            self.number+=1;item='local-fixture-'+str(self.number)
            self.messages.put(dict(type='input_audio_buffer.committed',item_id=item))
            self.jobs.put((item,bytes(self.buffer)));self.buffer.clear()
    def recognize(self):
        import wave
        from faster_whisper.audio import decode_audio
        while True:
            job=self.jobs.get()
            if job is None:return
            item,b=job
            # Resample actual received 24k PCM to the cached Whisper model's 16k.
            wav=io.BytesIO()
            with wave.open(wav,'wb') as w:
                w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b)
            wav.seek(0)
            audio=decode_audio(wav,sampling_rate=16000)
            segments,_=self.model.transcribe(audio,language='en',beam_size=3,temperature=0,
                condition_on_previous_text=False,vad_filter=True)
            text=''.join(x.text for x in segments).strip()
            words=text.split(' ');cut=max(1,len(words)//2)
            for idx,delta in enumerate([' '.join(words[:cut]),(' '+' '.join(words[cut:])) if words[cut:] else '']):
                if delta:self.messages.put(dict(type='conversation.item.input_audio_transcription.delta',event_id=item+'-d'+str(idx),item_id=item,delta=delta,provider_model='base.en_LOCAL_FIXTURE_TEST'))
            self.messages.put(dict(type='conversation.item.input_audio_transcription.completed',event_id=item+'-f',item_id=item,transcript=text,provider_model='base.en_LOCAL_FIXTURE_TEST'))
    def recv(self):return json.dumps(self.messages.get(timeout=20))
    def close(self):self.closed=True;self.jobs.put(None)


class AudioDatabaseTest(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('faster_whisper') is not None and
        any((ROOT/'live_transcription/models').rglob('model.bin')),
        'Optional local-ASR integration requires faster_whisper and a cached base.en model')
    def test_real_permitted_audio_recognition_to_database(self):
        from faster_whisper import WhisperModel
        fixture=ROOT/'live_transcription/tests/synthetic_speech.wav'
        permission=json.loads((fixture.parent/'source.json').read_text())
        sha=hashlib.sha256(fixture.read_bytes()).hexdigest()
        self.assertEqual(sha,permission['audio_sha256'])
        model=WhisperModel('base.en',device='cpu',compute_type='int8',cpu_threads=4,
                          download_root=str(ROOT/'live_transcription/models'),local_files_only=True)
        output=ROOT/'live_transcription/output/ingestion_fixture_test'
        output.mkdir(parents=True,exist_ok=True)
        # Isolated test volume of the EXISTING Store/database schema; not a new
        # production database or service. Each run retains its own evidence.
        data=output/uuid.uuid4().hex;store=Store(data);j=TranscriptJournal(store)
        source=dict(id=sha,kind='fixture',audio_url=str(fixture),started_at=time.time(),authorization=permission['permission'])
        socket=LocalSpeechSocket(model);snapshots=[];done=threading.Event()
        def poll():
            while not done.wait(.2):
                state=j.state()
                snapshots.append(dict(at=time.time(),events=len(state['events']),bytes_received=socket.received_bytes))
        thread=threading.Thread(target=poll,daemon=True);thread.start()
        try:
            result=gpt_session(j,source,'LOCAL_PROTOCOL_TEST_NO_API',duration=38,connector=lambda key:socket)
        finally:
            done.set();thread.join(2)
        events=result['events'];final=[x for x in events if x['kind']=='final'];deltas=[x for x in events if x['kind']=='delta']
        self.assertGreater(socket.received_bytes,0);self.assertGreater(len(final),0);self.assertGreater(len(deltas),0)
        self.assertTrue(any(s['events']>0 and s['bytes_received']<socket.received_bytes for s in snapshots))
        self.assertTrue(any(x['raw_text'].strip() for x in final));self.assertEqual(len(j.shadow()),len(final))
        self.assertTrue(all(x['audio_start_s'] is not None and x['audio_end_s']>x['audio_start_s'] for x in final))
        self.assertEqual(socket.config['session']['audio']['input']['transcription']['model'],MODEL)
        counts=len(events);j.recover()
        gpt_session(j,source,'LOCAL_PROTOCOL_TEST_NO_API',duration=38,connector=lambda key:self.fail('Completed source must not reconnect'))
        self.assertEqual(len(j.state()['events']),counts)
        evidence=dict(result='PASS',recognition_backend='cached local base.en; GPT protocol emulated; NOT an OpenAI test',
            source_kind=permission['source_kind'],fixture_sha256=sha,audio_bytes=socket.received_bytes,
            database=str(Path(store.path).relative_to(ROOT)),deltas=len(deltas),finals=len(final),
            shadow_observations=len(j.shadow()),updates_while_audio_continued=True,restart_duplicate_events=0,
            paid_api_calls=0,orders_sent=0,events=events,snapshots=snapshots)
        (output/'result.json').write_text(json.dumps(evidence,indent=2)+'\n')
        print('\nAUDIO_TO_DATABASE_EVIDENCE '+json.dumps({k:v for k,v in evidence.items() if k not in ('events','snapshots')}),flush=True)


if __name__=='__main__':unittest.main(verbosity=2)
