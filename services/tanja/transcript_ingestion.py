"""Continuous permitted audio -> existing Store -> Shadow transcript observations.

No trading imports. Source manifests must be supplied by an authorized publisher;
YouTube watch/stream extraction is intentionally unsupported. GPT timestamps are
capture intervals, not word timestamps or proof of trade execution.
"""
import argparse
import base64
from collections import deque
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import urlsplit
import urllib.request
import uuid

from store import Store

ROOT = Path(__file__).resolve().parent
RATE = 24000
MODEL = 'gpt-live-transcribe'
TRANSCRIPT_TYPES = {
    'conversation.item.input_audio_transcription.delta': 'delta',
    'conversation.item.input_audio_transcription.completed': 'final',
}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def secure_audio_url(url):
    p = urlsplit(url)
    host = (p.hostname or '').lower()
    if (p.scheme != 'https' or not host or p.username or p.password
            or any(host == x or host.endswith('.'+x) for x in
                   ('youtube.com', 'youtu.be', 'googlevideo.com', 'youtube-nocookie.com'))):
        raise ValueError('AUTHORIZED_DIRECT_HTTPS_SOURCE_REQUIRED')
    return url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def detect_source(url):
    """Publisher manifest: id, live, audio_url, started_at, authorization."""
    secure_audio_url(url)
    with urllib.request.build_opener(NoRedirect()).open(url, timeout=10) as r:
        raw = r.read(16385)
    if len(raw) > 16384:
        raise ValueError('SOURCE_MANIFEST_TOO_LARGE')
    source = json.loads(raw)
    if source.get('live') is not True:
        return None
    if (not isinstance(source.get('id'), str) or not 1 <= len(source['id']) <= 200
            or not isinstance(source.get('authorization'), str) or not source['authorization'].strip()
            or type(source.get('started_at')) not in (int, float)
            or not math.isfinite(source['started_at']) or source['started_at'] > time.time()+5):
        raise ValueError('AUTHORIZED_SESSION_METADATA_REQUIRED')
    secure_audio_url(source['audio_url'])
    return source


class TranscriptJournal:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS transcript_sessions (
                id TEXT PRIMARY KEY, source_kind TEXT NOT NULL, source_hash TEXT NOT NULL,
                source_started_at REAL NOT NULL, status TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                audio_seconds REAL NOT NULL DEFAULT 0, authorization TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS transcript_attempts (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, started_at REAL NOT NULL,
                ended_at REAL, status TEXT NOT NULL, gap_note TEXT);
              CREATE TABLE IF NOT EXISTS transcript_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL, event_id TEXT NOT NULL, item_id TEXT NOT NULL,
                kind TEXT NOT NULL, received_at REAL NOT NULL, audio_start_s REAL,
                audio_end_s REAL, raw_text TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(session_id,event_id));
              CREATE INDEX IF NOT EXISTS transcript_events_session
                ON transcript_events(session_id,sequence);
              CREATE TABLE IF NOT EXISTS shadow_transcript_observations (
                session_id TEXT NOT NULL, item_id TEXT NOT NULL, event_sequence INTEGER NOT NULL,
                recorded_at REAL NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(session_id,item_id));
            ''')

    def recover(self):
        with self.store.connect() as db:
            db.execute("UPDATE transcript_attempts SET status='INTERRUPTED',ended_at=?,gap_note='Process restart; unfinished provider output is UNKNOWN; audio is not replayed' WHERE status='RUNNING'", (time.time(),))
            db.execute("UPDATE transcript_sessions SET status='INTERRUPTED' WHERE status='RUNNING'")

    def start(self, source, kind):
        ident = hashlib.sha256((kind+':'+source['id']).encode()).hexdigest()
        source_hash = hashlib.sha256(source['audio_url'].encode()).hexdigest()
        now = time.time()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM transcript_sessions WHERE id=?', (ident,)).fetchone()
            if old and old['source_hash'] != source_hash:
                raise ValueError('SESSION_SOURCE_CONFLICT')
            if old and old['status'] == 'COMPLETED':
                return ident, None, old['audio_seconds']
            db.execute('INSERT OR IGNORE INTO transcript_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                       (ident, kind, source_hash, source['started_at'], 'RUNNING', now, now, 0, source['authorization']))
            db.execute("UPDATE transcript_sessions SET status='RUNNING',updated_at=? WHERE id=?", (now, ident))
            attempt = uuid.uuid4().hex
            db.execute('INSERT INTO transcript_attempts VALUES(?,?,?,?,?,?)',
                       (attempt, ident, now, None, 'RUNNING', 'Reconnect gap; no replay' if old else None))
            offset = old['audio_seconds'] if old else 0
        self.store.note('transcript_session', 'Audio transcription session started; observation only')
        return ident, attempt, offset

    def checkpoint(self, session, seconds):
        with self.store.connect() as db:
            db.execute('UPDATE transcript_sessions SET audio_seconds=MAX(audio_seconds,?),updated_at=? WHERE id=?',
                       (seconds, time.time(), session))

    def finish(self, session, attempt, status, note=None):
        with self.store.connect() as db:
            db.execute('UPDATE transcript_attempts SET status=?,ended_at=?,gap_note=COALESCE(?,gap_note) WHERE id=?',
                       (status, time.time(), note, attempt))
            db.execute('UPDATE transcript_sessions SET status=?,updated_at=? WHERE id=?', (status, time.time(), session))
        self.store.note('transcript_session', 'Audio transcription session '+status)

    def ingest(self, session, attempt, event, bounds=None):
        kind = TRANSCRIPT_TYPES.get(event.get('type'))
        if not kind:
            return False
        item = event.get('item_id')
        eid = event.get('event_id')
        if not isinstance(item, str) or not item:
            raise ValueError('ITEM_ID_REQUIRED')
        # Missing delta identity cannot safely distinguish replay from repeated speech.
        if not eid and kind == 'final':
            eid = 'final:'+item
        if not isinstance(eid, str) or not eid:
            raise ValueError('DELTA_EVENT_ID_REQUIRED')
        text = event.get('delta' if kind == 'delta' else 'transcript')
        if not isinstance(text, str) or len(text) > 100000:
            raise ValueError('INVALID_TRANSCRIPT_TEXT')
        payload = encoded(event)
        now = time.time()
        start, end = bounds if bounds else (None, None)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM transcript_attempts WHERE id=? AND session_id=?', (attempt, session)).fetchone():
                raise ValueError('UNKNOWN_SESSION_ATTEMPT')
            old = db.execute('SELECT payload FROM transcript_events WHERE session_id=? AND event_id=?', (session,eid)).fetchone()
            if old:
                if old['payload'] != payload:
                    raise ValueError('CONFLICTING_TRANSCRIPT_EVENT')
                return False
            if kind == 'final':
                prior = db.execute('SELECT payload FROM shadow_transcript_observations WHERE session_id=? AND item_id=?', (session,item)).fetchone()
                if prior:
                    if json.loads(prior['payload'])['raw_text'] != text:
                        raise ValueError('CONFLICTING_FINAL_TRANSCRIPT')
                    return False
            cur = db.execute('INSERT INTO transcript_events(session_id,attempt_id,event_id,item_id,kind,received_at,audio_start_s,audio_end_s,raw_text,payload) VALUES(?,?,?,?,?,?,?,?,?,?)',
                             (session,attempt,eid,item,kind,now,start,end,text,payload))
            if kind == 'final':
                observation = dict(session_id=session, attempt_id=attempt, item_id=item,
                    event_sequence=cur.lastrowid, raw_text=text, received_at=now,
                    audio_start_s=start, audio_end_s=end, timestamp_basis='CAPTURE_INTERVAL_NOT_WORD_TIMING',
                    evidence_status='TRANSCRIPT_ONLY_UNVERIFIED_ACTION', executable=False,
                    orders_enabled=False, order_state='UNKNOWN', price='UNKNOWN')
                db.execute('INSERT INTO shadow_transcript_observations VALUES(?,?,?,?,?)',
                           (session,item,cur.lastrowid,now,encoded(observation)))
        return True

    def record_decision(self, session, attempt, decision):
        """Store historical structured decisions through the existing Shadow journal.

        These are interpreter observations, never provider transcripts or orders.
        Identity and payload remain stable across repeated historical replays.
        """
        payload = encoded(decision)
        eid = 'decision:'+decision['event_id']
        now = time.time()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM transcript_attempts WHERE id=? AND session_id=?', (attempt, session)).fetchone():
                raise ValueError('UNKNOWN_SESSION_ATTEMPT')
            old = db.execute('SELECT payload FROM transcript_events WHERE session_id=? AND event_id=?', (session,eid)).fetchone()
            if old:
                if old[0] != payload:
                    raise ValueError('CONFLICTING_DECISION_EVENT')
                return False
            cur = db.execute('INSERT INTO transcript_events(session_id,attempt_id,event_id,item_id,kind,received_at,audio_start_s,audio_end_s,raw_text,payload) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (session,attempt,eid,eid,'decision',now,decision['source_seconds'],None,decision['quote'],payload))
            observation = dict(session_id=session, item_id=eid, event_sequence=cur.lastrowid,
                raw_text=decision['quote'], received_at=now, structured_decision=decision,
                evidence_status='HISTORICAL_TRANSCRIPT_NOT_BROKER_VERIFIED',
                executable=False, orders_enabled=False)
            db.execute('INSERT INTO shadow_transcript_observations VALUES(?,?,?,?,?)',
                (session,eid,cur.lastrowid,now,encoded(observation)))
        return True

    def state(self, after=0, limit=100):
        with self.store.connect() as db:
            sessions = [dict(x) for x in db.execute('SELECT id,source_kind,source_started_at,status,created_at,updated_at,audio_seconds FROM transcript_sessions ORDER BY created_at DESC LIMIT 20')]
            events = [dict(x) for x in db.execute('SELECT sequence,session_id,attempt_id,event_id,item_id,kind,received_at,audio_start_s,audio_end_s,raw_text FROM transcript_events WHERE sequence>? ORDER BY sequence LIMIT ?', (after,limit))]
        return dict(sessions=sessions, events=events, next_sequence=events[-1]['sequence'] if events else after,
                    mode='OBSERVE_ONLY', orders_enabled=False)

    def shadow(self, after=0, limit=100):
        with self.store.connect() as db:
            return [json.loads(x[0]) for x in db.execute('SELECT payload FROM shadow_transcript_observations WHERE event_sequence>? ORDER BY event_sequence LIMIT ?', (after,limit))]


def pcm(source, offset=0, duration=600, stop=None):
    """FFmpeg capture is paced in real time; never download ahead of playback."""
    kind = source.get('kind', 'authorized_live')
    args = ['ffmpeg', '-nostdin', '-v', 'error', '-re']
    if kind == 'fixture' and offset:
        args += ['-ss', str(offset)]
    if kind != 'fixture':
        secure_audio_url(source['audio_url'])
        args += ['-rw_timeout', '10000000', '-protocol_whitelist', 'https,tls,tcp,crypto']
    args += ['-i', source['audio_url'], '-t', str(duration), '-vn', '-f', 's16le', '-ar', str(RATE), '-ac', '1', 'pipe:1']
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while not stop or not stop.is_set():
            block = proc.stdout.read(RATE//5)  # 100 ms PCM16
            if not block:
                break
            if len(block) % 2:
                raise ValueError('TRUNCATED_PCM')
            yield block
        if proc.poll() not in (None, 0):
            raise RuntimeError('AUDIO_SOURCE_FAILED')
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()
        proc.stdout.close()


def open_gpt(key):
    import websocket
    return websocket.create_connection('wss://api.openai.com/v1/realtime?intent=transcription',
        header=['Authorization: Bearer '+key], timeout=15, enable_multithread=True)


def gpt_session(journal, source, key, *, duration=45, stop=None, connector=open_gpt):
    """One socket; simultaneous capture/send and receive; no automatic audio replay."""
    if not key:
        raise ValueError('OPENAI_API_KEY_REQUIRED')
    session, attempt, offset = journal.start(source, source.get('kind','authorized_live'))
    if attempt is None:
        return journal.state()
    ws = None
    closed = threading.Event(); ready = threading.Event(); failures = []
    pending = deque(); bounds = {}; lock = threading.Lock()
    sent = offset; turn_start = offset
    def send(obj):
        ws.send(encoded(obj))
    def receive():
        try:
            while not closed.is_set():
                event = json.loads(ws.recv())
                typ = event.get('type')
                if typ == 'error':
                    raise RuntimeError('GPT_EVENT_ERROR')
                if typ == 'session.updated':
                    ready.set()
                if typ == 'input_audio_buffer.committed':
                    with lock:
                        if not pending:
                            raise RuntimeError('UNMATCHED_AUDIO_COMMIT')
                        bounds[event['item_id']] = pending.popleft()
                if typ in TRANSCRIPT_TYPES:
                    with lock:
                        interval = bounds.get(event.get('item_id'))
                    if journal.ingest(session, attempt, event, interval):
                        print(event.get('delta', event.get('transcript','')), flush=True)
        except Exception as e:
            if not closed.is_set():
                failures.append(type(e).__name__)
                ready.set()
    try:
        ws = connector(key)
        vocabulary = json.loads((ROOT/'live_transcription'/'tanja_vocabulary.json').read_text())
        terms = [x['term'] for group in vocabulary['categories'].values() for x in group]
        send(dict(type='session.update', session=dict(type='transcription', audio=dict(input=dict(
            format=dict(type='audio/pcm', rate=RATE), transcription=dict(model=MODEL,
            languages=['en'], keywords=terms, delay='low'), turn_detection=None)))))
        thread = threading.Thread(target=receive, daemon=True); thread.start()
        if not ready.wait(15) or failures:
            raise RuntimeError('GPT_SESSION_NOT_READY')
        for block in pcm(source, offset, duration, stop):
            if failures:
                raise RuntimeError('GPT_DISCONNECTED')
            send(dict(type='input_audio_buffer.append', audio=base64.b64encode(block).decode()))
            sent += len(block)/(RATE*2)
            journal.checkpoint(session, sent)
            # Bound finalized turns to five seconds, including sustained speech.
            if sent-turn_start >= 5-1e-6:
                with lock:
                    pending.append((turn_start,sent))
                send(dict(type='input_audio_buffer.commit'))
                turn_start = sent
        if sent == offset:
            raise RuntimeError('NO_AUDIO_RECEIVED')
        if sent-turn_start >= .1:
            with lock:
                pending.append((turn_start,sent))
            send(dict(type='input_audio_buffer.commit'))
        deadline = time.monotonic()+20
        while time.monotonic() < deadline and not failures:
            with lock:
                all_committed = not pending
                items = set(bounds)
            with journal.store.connect() as db:
                finals = {x[0] for x in db.execute('SELECT item_id FROM transcript_events WHERE attempt_id=? AND kind=\'final\'', (attempt,))}
            if all_committed and items and items <= finals:
                break
            time.sleep(.1)
        else:
            raise RuntimeError('FINAL_TRANSCRIPT_TIMEOUT')
        journal.finish(session, attempt, 'COMPLETED' if source.get('kind')=='fixture' else 'PAUSED',
                       'Capture interval ended; no audio replay on reconnect')
    except Exception:
        journal.finish(session, attempt, 'INTERRUPTED', 'Connection/capture interruption; unfinished text is UNKNOWN; no automatic audio replay')
        raise
    finally:
        closed.set()
        if ws:
            ws.close()
    return journal.state()


class TranscriptIngestion:
    def __init__(self, store, config):
        self.journal = TranscriptJournal(store)
        self.enabled = config.get('TANJA_TRANSCRIPT_ENABLED','false').lower() == 'true'
        self.approved = config.get('TANJA_TRANSCRIPT_PAID_APPROVED','false').lower() == 'true'
        self.manifest = config.get('TANJA_TRANSCRIPT_SOURCE_MANIFEST_URL','')
        self.key = config.get('OPENAI_API_KEY','')

    def state(self):
        result = self.journal.state()
        result['input_status'] = ('DISABLED' if not self.enabled else
            'NO_AUTHORIZED_SOURCE_CONFIGURED' if not self.manifest else
            'PAID_APPROVAL_REQUIRED' if not self.approved else
            'MISSING_API_KEY' if not self.key else 'SOURCE_CONFIGURED_NOT_PROOF_OF_AUDIO')
        return result

    def run(self, stop):
        self.journal.recover()
        while not stop.is_set():
            if self.enabled and self.approved and self.manifest and self.key:
                try:
                    source = detect_source(self.manifest)
                    if source:
                        # Persisted ten-minute ceiling per logical source session,
                        # including reconnects, bounds initial spend (~$0.17).
                        ident = hashlib.sha256(('authorized_live:'+source['id']).encode()).hexdigest()
                        with self.journal.store.connect() as db:
                            row = db.execute('SELECT audio_seconds FROM transcript_sessions WHERE id=?',(ident,)).fetchone()
                        left = 600-(row['audio_seconds'] if row else 0)
                        if left > .1:
                            gpt_session(self.journal, source, self.key, duration=left, stop=stop)
                except Exception as e:
                    self.journal.store.note('transcript_error', type(e).__name__)
            stop.wait(30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=38)
    parser.add_argument('--paid-approved', action='store_true')
    args = parser.parse_args()
    if not args.paid_approved:
        parser.error('--paid-approved is required before any paid call')
    if not 0 < args.seconds <= 45:
        parser.error('Fixture API test is capped at 45 seconds (~$0.013)')
    source = dict(id=hashlib.sha256(args.fixture.read_bytes()).hexdigest(), kind='fixture',
                  audio_url=str(args.fixture.resolve()), started_at=time.time(),
                  authorization='User-authorized local speech fixture')
    journal = TranscriptJournal(Store(args.data_dir)); journal.recover()
    result = gpt_session(journal, source, os.environ.get('OPENAI_API_KEY',''), duration=args.seconds)
    print(encoded(result), flush=True)


if __name__ == '__main__':
    main()
