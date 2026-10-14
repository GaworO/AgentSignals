#!/usr/bin/env python3
"""Bounded local BlackHole -> existing GPT ingestion -> interpreter -> Shadow."""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import live_transcriber as local
import transcript_ingestion as ingestion
from store import Store
from transcript_trade_interpreter import Interpreter
from transcript_entry_bridge import EntryBridge

MAX_AUDIO_SECONDS = 110  # Leave time for final text within the 120s wall limit.
WALL_SECONDS = 120


class MacCapture:
    """Adapt the existing AVFoundation input to GPT's PCM16/24kHz/mono stream."""
    def __init__(self, device):
        local.RATE = ingestion.RATE
        local.BLOCK_BYTES = ingestion.RATE // 5  # 100 ms, signed 16-bit mono.
        self.input = local.AudioInput(argparse.Namespace(
            device=device, file=None, stdin_pcm=False))
        self.epoch = None
        self.bytes_sent = 0

    def block(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                return self.input.queue.get(timeout=.1)
            except queue.Empty:
                if self.input.done.is_set():
                    raise RuntimeError(self.input.error or 'AUDIO_CAPTURE_ENDED')
        raise RuntimeError('AUDIO_CAPTURE_TIMEOUT')

    def close(self):
        self.input.close()
        proc = self.input.process
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=1)

    def preflight(self):
        import numpy as np
        self.input.start()
        peak = rms = 0
        try:
            for _ in range(30):
                samples = np.frombuffer(self.block(), dtype='<i2').astype(float) / 32768
                peak = max(peak, float(np.max(np.abs(samples))))
                rms = max(rms, float(np.sqrt(np.mean(samples ** 2))))
        finally:
            self.close()
        if rms < .0001:
            raise RuntimeError('BLACKHOLE_SILENT: select a Multi-Output Device containing BlackHole and play Chrome audio')
        return dict(peak=round(peak, 6), max_block_rms=round(rms, 6))

    def pcm(self, source, offset=0, duration=MAX_AUDIO_SECONDS, stop=None):
        if offset:
            raise RuntimeError('LOCAL_TEST_DOES_NOT_RECONNECT_OR_REPLAY')
        limit = int(min(duration, MAX_AUDIO_SECONDS) * ingestion.RATE) * 2
        self.input.start()
        try:
            while self.bytes_sent < limit and not (stop and stop.is_set()):
                block = self.block()[:limit - self.bytes_sent]
                if self.epoch is None:
                    self.epoch = time.monotonic() - len(block) / (ingestion.RATE * 2)
                self.bytes_sent += len(block)
                yield block
        finally:
            self.close()


def event_label(event):
    if event.get('reported_fill'):
        return 'FILL'
    action = event['action']
    return ('MARKET' if action == 'MARKET_ORDER' else
            'LIMIT' if action.startswith('LIMIT_') else
            'PARTIAL' if action == 'PARTIAL_EXIT' else
            'STOP' if action.startswith('STOP_') else
            'EXIT' if action == 'FULL_EXIT' else action)


class LiveJournal(ingestion.TranscriptJournal):
    """Keep provider ingestion unchanged; annotate its local Shadow observations."""
    def __init__(self, directory, capture, contract=''):
        super().__init__(Store(directory))
        self.directory, self.capture = directory, capture
        self.engine = Interpreter()
        self.waiting, self.first_delta = {}, {}
        self.next_start = 0
        self.tail, self.tail_start = '', 0
        self.sentence = 0
        self.decisions, self.latency = [], []
        self.last = None
        self.mutex = threading.RLock()
        self.entry_bridge = EntryBridge(self, contract=contract)

    def ingest(self, session, attempt, event, bounds=None):
        with self.mutex:
            return self._ingest(session, attempt, event, bounds)

    def _ingest(self, session, attempt, event, bounds=None):
        if not super().ingest(session, attempt, event, bounds):
            return False
        now = time.monotonic()
        item = event['item_id']
        if event['type'].endswith('.delta'):
            self.first_delta.setdefault(item, now)
            print('[delta]', end=' ', flush=True)
        else:
            if bounds is None:
                raise RuntimeError('FINAL_CAPTURE_INTERVAL_REQUIRED')
            start, end = bounds
            epoch = self.capture.epoch
            self.latency.append(dict(item_id=item, audio_start_s=start, audio_end_s=end,
                final_after_window_end_s=round(now-epoch-end, 4),
                first_delta_after_window_start_s=round(self.first_delta[item]-epoch-start, 4)
                if item in self.first_delta else None))
            self.waiting[round(start, 6)] = (session, item, event['transcript'], start, end)
            # GPT can finalize turns out of order. Preserve causal interpreter state.
            while round(self.next_start, 6) in self.waiting:
                self.last = self.waiting.pop(round(self.next_start, 6))
                sid, iid, text, begin, finish = self.last
                self.consume(sid, iid, text, begin)
                self.next_start = finish
            print('[final]', end=' ', flush=True)
        return True

    def consume(self, session, item, text, start, flush=False):
        if not self.tail:
            self.tail_start = start
        combined = (self.tail + ' ' + text).strip()
        pieces = re.split(r'(?<=[.!?])\s+', combined) if combined else []
        self.tail = ''
        if pieces and not flush and not re.search(r'[.!?]$', pieces[-1]):
            self.tail = pieces.pop()
        decisions = []
        for quote in pieces:
            self.sentence += 1
            statement = dict(quote=quote, source_seconds=self.tail_start,
                source_timestamp=time.strftime('%H:%M:%S', time.gmtime(self.tail_start)),
                source_line=self.sentence, sentence_index=0)
            for event in self.engine.interpret(statement, session):
                event.update(historical_replay=False, executable=False, orders_enabled=False,
                    timestamp_basis='CAPTURE_INTERVAL_NOT_WORD_TIMING', label=event_label(event))
                decisions.append(event)
                self.decisions.append(event)
                print('\n[EVENT %s] %s | %s | %s' % (event['label'], event['action'],
                      event['evidence_status'], event['quote']), flush=True)
        if decisions:
            with self.store.connect() as db:
                row = db.execute('SELECT payload FROM shadow_transcript_observations WHERE session_id=? AND item_id=?', (session, item)).fetchone()
                observation = json.loads(row[0])
                observation.setdefault('structured_decisions', []).extend(decisions)
                db.execute('UPDATE shadow_transcript_observations SET payload=? WHERE session_id=? AND item_id=?',
                           (ingestion.encoded(observation), session, item))
            for event in decisions:
                captured_at = time.time()-time.monotonic()+self.capture.epoch+event['source_seconds']
                self.entry_bridge.preview(session, item, event, captured_at, 'macos_device')
        if not self.tail:
            self.tail_start = start

    def flush(self):
        with self.mutex:
            if self.tail and self.last:
                self.consume(self.last[0], self.last[1], '', self.tail_start, flush=True)


def api_key(railway_key):
    key = os.environ.get('OPENAI_API_KEY', '')
    if not key and railway_key:
        # Same read-only credential lookup as the existing approved fixture runner.
        result = subprocess.run(['railway', 'variable', 'list',
            '--project', '26df4462-061e-48e5-ad8a-b364909a1b8f',
            '--service', '187a0737-d7a7-4f7c-b00d-b31a6f7e60ad',
            '--environment', 'production', '--json'], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError('TANJA_RAILWAY_KEY_READ_FAILED')
        key = json.loads(result.stdout).get('OPENAI_API_KEY', '')
    if not key:
        raise RuntimeError('OPENAI_API_KEY_REQUIRED: set it locally or use --railway-key')
    return key


def stats(values):
    values = sorted(x for x in values if x is not None)
    if not values:
        return None
    return dict(count=len(values), mean=round(sum(values)/len(values), 4),
                p95=values[max(0, math.ceil(.95*len(values))-1)], max=values[-1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', default='BlackHole 2ch')
    parser.add_argument('--railway-key', action='store_true', help='Read existing Tanja API key; no Railway writes')
    parser.add_argument('--entry-contract', default=os.environ.get('TANJA_TEST_CONTRACT', ''),
                        help='Explicit MNQ contract for local test-payload previews; never sends orders')
    parser.add_argument('--seconds', type=int, default=MAX_AUDIO_SECONDS, choices=range(1, MAX_AUDIO_SECONDS+1), metavar='1..110')
    args = parser.parse_args()
    base = HERE / 'output/local_gpt_test'
    base.mkdir(parents=True, exist_ok=True)
    lock = (base / '.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error('A local GPT test is already running')
    directory = base / (time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6])
    directory.mkdir()
    capture = MacCapture(args.device)
    journal = LiveJournal(directory, capture, args.entry_contract)
    errors, socket = [], []
    paid_started = None
    status = 'ERROR'

    def stop(signum, frame):
        raise KeyboardInterrupt()

    def hard_stop(signum, frame):
        proc = capture.input.process
        if proc and proc.poll() is None:
            proc.kill()
        os._exit(124)

    def deadline(signum, frame):
        # Close paid connection immediately; reserve two seconds to save the report.
        for ws in socket:
            ws.shutdown()
        signal.signal(signal.SIGALRM, hard_stop)
        signal.setitimer(signal.ITIMER_REAL, 2)
        raise TimeoutError('120_SECOND_WALL_LIMIT')

    def connect(key):
        nonlocal paid_started
        paid_started = time.monotonic()
        signal.signal(signal.SIGALRM, deadline)
        signal.setitimer(signal.ITIMER_REAL, WALL_SECONDS-2)
        ws = ingestion.open_gpt(key)
        socket.append(ws)
        receive = ws.recv
        def monitored_receive(*a, **kw):
            raw = receive(*a, **kw)
            event = json.loads(raw)
            if event.get('type') == 'error':
                error = event.get('error', {})
                errors.append(('GPT: %s: %s' % (error.get('code', 'UNKNOWN'),
                              error.get('message', 'Unknown provider error'))).replace(key, '[redacted]'))
            return raw
        ws.recv = monitored_receive
        return ws

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print('OBSERVE_ONLY | %s | max %ss audio / %ss connection | stop: Ctrl+C\nOutput: %s' %
          (ingestion.MODEL, args.seconds, WALL_SECONDS, directory), flush=True)
    try:
        level = capture.preflight()
        print('BlackHole audio verified: ' + json.dumps(level), flush=True)
        key = api_key(args.railway_key)
        capture = MacCapture(args.device)
        journal.capture = capture
        # Process-local adapter injection only; existing modules/files are unchanged.
        ingestion.pcm = capture.pcm
        source = dict(id=directory.name, kind='macos_device', audio_url='avfoundation:'+args.device,
            started_at=time.time(), authorization='User-authorized local system audio transcription test')
        ingestion.gpt_session(journal, source, key, duration=args.seconds, connector=connect)
        status = 'COMPLETED'
    except KeyboardInterrupt:
        status = 'STOPPED'
    except Exception as exc:
        errors.append(type(exc).__name__ + ': ' + str(exc))
    finally:
        capture.close()
        for ws in socket:
            ws.shutdown()
        connection_s = round(time.monotonic()-paid_started, 3) if paid_started else 0
        journal.flush()
        journal.recover()
        with journal.store.connect() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM transcript_events ORDER BY sequence')]
            shadow = [json.loads(r[0]) for r in db.execute('SELECT payload FROM shadow_transcript_observations ORDER BY event_sequence')]
        if journal.waiting:
            errors.append('UNINTERPRETED_OUT_OF_ORDER_FINALS: an earlier final is missing')
        result = dict(status=status, model=ingestion.MODEL, device=args.device,
            audio_seconds=round(capture.bytes_sent/(ingestion.RATE*2), 3), connection_seconds=connection_s,
            max_audio_seconds=MAX_AUDIO_SECONDS, hard_shutdown_seconds=WALL_SECONDS,
            deltas=sum(r['kind']=='delta' for r in rows), finals=sum(r['kind']=='final' for r in rows),
            shadow_observations=len(shadow), event_counts={label:sum(e['label']==label for e in journal.decisions)
                for label in ('MARKET', 'LIMIT', 'FILL', 'PARTIAL', 'STOP', 'EXIT')},
            final_after_window_end_s=stats(x['final_after_window_end_s'] for x in journal.latency),
            first_delta_after_window_start_s=stats(x['first_delta_after_window_start_s'] for x in journal.latency),
            latency_basis='Estimated local capture intervals; not word/phrase timing or YouTube broadcast latency',
            errors=errors, orders_enabled=False, traderspost_calls=0, railway_changes=0,
            database=journal.store.path, decisions=journal.decisions, latency_samples=journal.latency)
        for name, records in [('transcript.jsonl', rows), ('shadow_observations.jsonl', shadow)]:
            (directory/name).write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in records))
        (directory/'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False)+'\n')
        signal.setitimer(signal.ITIMER_REAL, 0)
        print('\n' + json.dumps({k:v for k,v in result.items() if k not in ('decisions','latency_samples')}, indent=2), flush=True)
        print('Report: ' + str(directory/'result.json'), flush=True)
        lock.close()
    return int(bool(errors))


if __name__ == '__main__':
    sys.exit(main())
