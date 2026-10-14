#!/usr/bin/env python3
"""Incremental local speech recognition for permitted Mac audio. No trading code."""
import argparse
from collections import deque
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent
RATE = 16000
BLOCK_BYTES = RATE  # Half a second of mono signed 16-bit PCM.


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalized(text):
    """Comparison only. The text written to the transcript is never normalized."""
    return re.sub(r'[^\w]', '', text.casefold())


def new_words(words, recent, last_end, window_start=None):
    """Deduplicate only overlapping audio; genuine later repetitions survive."""
    # Ignore standalone punctuation on both sides of the comparison. Attached
    # punctuation and the spelling of emitted words remain unchanged.
    candidates = [w for w in words if normalized(w['word']) and w['end'] > last_end + .08]
    prior = [w for w in recent if normalized(w['word'])]
    for count in range(min(len(prior), len(candidates), 12), 0, -1):
        left, right = prior[-count:], candidates[:count]
        if (all(normalized(a['word']) == normalized(b['word']) for a, b in zip(left, right))
                and right[0]['start'] <= last_end + .5
                and (window_start is None or left[-1]['end'] >= window_start - .15)
                # The new hypothesis may stretch an endpoint. Compare nearby
                # spans instead, anchored to audio present in both windows.
                and all(max(a['start'], b['start']) <= min(a['end'], b['end']) + .5
                        for a, b in zip(left, right))):
            candidates = candidates[count:]
            break
    # A straddling word can shift slightly between overlapping hypotheses.
    return [w for w in candidates if w['start'] >= last_end - .15]


class AudioInput:
    def __init__(self, args):
        self.args = args
        self.queue = queue.Queue(maxsize=40)
        self.stop = threading.Event()
        self.done = threading.Event()
        self.error = None
        self.process = None
        self.thread = None
        self.errors = deque(maxlen=30)
        self.started = None
        self.started_utc = None
        self.bytes_received = 0
        self.peak_queue_blocks = 0
        self.live = args.file is None

    def start(self):
        if self.args.stdin_pcm:
            self.stream = sys.stdin.buffer
        else:
            ffmpeg = shutil.which('ffmpeg')
            if ffmpeg is None:
                raise RuntimeError('FFmpeg missing. Install once with: brew install ffmpeg')
            command = [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error']
            if self.args.file:
                path = self.args.file.expanduser().resolve()
                if not path.is_file():
                    raise ValueError('Local recording does not exist: %s' % path)
                command += ['-i', str(path)]
            else:
                if sys.platform != 'darwin':
                    raise ValueError('--device currently requires macOS; use --file or --stdin-pcm elsewhere')
                command += ['-f', 'avfoundation', '-i', ':' + self.args.device]
            command += ['-vn', '-ac', '1', '-ar', str(RATE), '-f', 's16le', 'pipe:1']
            self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.stream = self.process.stdout
            threading.Thread(target=self._read_errors, daemon=True).start()
        self.started, self.started_utc = time.monotonic(), utc_now()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read_errors(self):
        for line in iter(self.process.stderr.readline, b''):
            self.errors.append(line.decode(errors='replace').strip())

    def _read(self):
        try:
            while not self.stop.is_set():
                block = self.stream.read(BLOCK_BYTES)
                if not block:
                    break
                if len(block) % 2:
                    raise RuntimeError('Truncated PCM input: expected signed 16-bit mono samples')
                self.bytes_received += len(block)
                while not self.stop.is_set():
                    try:
                        self.queue.put(block, timeout=1)
                        self.peak_queue_blocks = max(self.peak_queue_blocks, self.queue.qsize())
                        break
                    except queue.Full:
                        if self.live:
                            raise RuntimeError('Transcription fell behind by 20 seconds; stopped instead of silently dropping audio. Try --model tiny.en.')
            if self.process:
                code = self.process.wait(timeout=5)
                if code and not self.stop.is_set():
                    raise RuntimeError('Audio input failed: ' + '\n'.join(self.errors))
            if not self.bytes_received and not self.stop.is_set():
                raise RuntimeError('No audio received. Check the input device and macOS microphone permission.')
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.done.set()

    def close(self):
        self.stop.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()


class TranscriptWriter:
    def __init__(self, directory, session):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory, self.session, self.count = directory, session, 0
        self.lock = (directory / '.transcription.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another transcriber is already writing to this output folder')
        self.jsonl = (directory / 'live_transcript.jsonl').open('a', encoding='utf-8')
        self.text = (directory / 'live_transcript.txt').open('a', encoding='utf-8')

    def append(self, record):
        self.count += 1
        record.update(session_id=self.session, sequence=self.count)
        self.jsonl.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
        line = '[%s %.2f–%.2f] %s' % (record['wall_clock_utc'], record['audio_start_s'], record['audio_end_s'], record['text'])
        self.text.write(line + '\n')
        for handle in (self.jsonl, self.text):
            handle.flush()
            os.fsync(handle.fileno())
        print(line, flush=True)

    def status(self, value):
        # Atomic status file allows a test/monitor to observe receipt during capture.
        temporary = self.directory / '.status.tmp'
        temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
        temporary.replace(self.directory / 'live_status.json')

    def close(self):
        self.jsonl.close()
        self.text.close()
        self.lock.close()


def vocabulary_prompt(path):
    data = json.loads(path.read_text())
    terms = [entry['term'] for entries in data['categories'].values() for entry in entries]
    # No historical quotes, prices, instructions, or strategy are sent as a prompt.
    return 'English trading terminology: ' + ', '.join(terms) + '.'


class Transcriber:
    def __init__(self, args, model, source, writer, np):
        self.args, self.model, self.source, self.writer, self.np = args, model, source, writer, np
        self.buffer = np.empty(0, dtype=np.float32)
        self.samples = self.new_samples = self.chunks = self.silent_chunks = 0
        self.last_end = -1.0
        self.recent = deque(maxlen=24)
        self.latencies = []
        self.peak = 0.0
        self.prompt = vocabulary_prompt(args.vocabulary)

    def add(self, block):
        audio = self.np.frombuffer(block, dtype='<i2').astype(self.np.float32) / 32768.0
        self.peak = max(self.peak, float(self.np.max(self.np.abs(audio))))
        self.buffer = self.np.concatenate((self.buffer, audio))
        self.samples += len(audio)
        self.new_samples += len(audio)
        if self.new_samples >= self.args.chunk_seconds * RATE:
            self.transcribe(final=False)

    def transcribe(self, final):
        if not len(self.buffer):
            return
        end = self.samples / RATE
        offset = end - len(self.buffer) / RATE
        rms = float(self.np.sqrt(self.np.mean(self.buffer ** 2)))
        started = time.monotonic()
        words = []
        if rms >= .002:
            segments, _ = self.model.transcribe(
                self.buffer, language='en', task='transcribe', beam_size=self.args.beam_size,
                temperature=0, condition_on_previous_text=False,
                initial_prompt=self.prompt, word_timestamps=True,
                vad_filter=True, vad_parameters={'min_silence_duration_ms': 300},
                hallucination_silence_threshold=1.0)
            for segment in segments:
                for word in segment.words or []:
                    start_s, end_s = offset + word.start, offset + word.end
                    if end_s <= end + .05 and (final or end_s <= end - self.args.holdback_seconds):
                        words.append(dict(word=word.word, start=start_s, end=end_s,
                                          probability=word.probability))
        else:
            self.silent_chunks += 1
        duration = time.monotonic() - started
        self.chunks += 1
        self.latencies.append(duration)
        fresh = new_words(words, self.recent, self.last_end, window_start=offset)
        if fresh:
            text = ''.join(w['word'] for w in fresh).strip()
            if text:
                elapsed = time.monotonic() - self.source.started
                start_s = max(0.0, self.last_end, fresh[0]['start'])
                end_s = max(start_s, fresh[-1]['end'])
                self.writer.append(dict(
                    audio_start_s=round(start_s, 3), audio_end_s=round(end_s, 3),
                    wall_clock_utc=utc_now(), text=text,
                    processing_latency_s=round(duration, 4),
                    audio_to_output_latency_s=round(max(0, elapsed - end_s), 4) if self.source.live else None,
                    emitted_elapsed_s=round(elapsed, 4), source_start_utc=self.source.started_utc,
                    source_mode='local_recording' if self.args.file else 'live_pcm' if self.args.stdin_pcm else 'macos_device',
                    model=self.args.model, beam_size=self.args.beam_size, audio_window_end_s=round(end, 3),
                    # ASR is not an order classifier. Never default an utterance
                    # to market, nor a pending order to a confirmed position.
                    order_type='UNKNOWN', order_state='UNKNOWN', price='UNKNOWN',
                    interpretation_status='UNCLASSIFIED_TRANSCRIPT',
                    uncertain_words=[w['word'].strip() for w in fresh if w['probability'] < .5]))
                self.last_end = end_s
                self.recent.extend(fresh)
        self.buffer = self.buffer[-int(self.args.overlap_seconds * RATE):]
        self.new_samples = 0
        self.save_status('running')

    def save_status(self, state, error=None):
        self.writer.status(dict(
            state=state, error=error, session_id=self.writer.session, updated_at_utc=utc_now(),
            bytes_received=self.source.bytes_received, audio_processed_s=round(self.samples / RATE, 3),
            peak_amplitude=round(self.peak, 6), chunks_processed=self.chunks,
            silent_chunks=self.silent_chunks, transcript_records=self.writer.count,
            max_queue_seconds=self.source.peak_queue_blocks * .5,
            chunk_seconds=self.args.chunk_seconds, overlap_seconds=self.args.overlap_seconds,
            holdback_seconds=self.args.holdback_seconds,
            beam_size=self.args.beam_size,
            processing_latency_mean_s=round(sum(self.latencies) / len(self.latencies), 4) if self.latencies else None,
            processing_latency_max_s=round(max(self.latencies), 4) if self.latencies else None))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument('--file', type=Path, help='Permitted local audio/video recording')
    inputs.add_argument('--stdin-pcm', action='store_true', help='Live signed 16-bit little-endian mono PCM, 16000 Hz, on stdin')
    inputs.add_argument('--device', default=None, help='Exact macOS audio input name; default: BlackHole 2ch')
    parser.add_argument('--list-devices', action='store_true')
    parser.add_argument('--model', default='base.en', help='Local faster-whisper model name/path; tiny.en uses less CPU')
    parser.add_argument('--offline', action='store_true', help='Require already-downloaded local weights')
    parser.add_argument('--chunk-seconds', type=float, default=5.0)
    parser.add_argument('--overlap-seconds', type=float, default=None,
                        help='Retained audio context (default: 1 s for 2 s chunks; otherwise 1.5 s)')
    parser.add_argument('--holdback-seconds', type=float, default=None,
                        help='Defer words at window end (default: 0.2 s for 2 s chunks; otherwise 0.5 s)')
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--beam-size', type=int, choices=[1, 3], default=3,
                        help='Local decoder beam size; keep chunks at 5 seconds')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'output')
    parser.add_argument('--vocabulary', type=Path, default=ROOT / 'tanja_vocabulary.json')
    args = parser.parse_args()
    if args.list_devices:
        if not shutil.which('ffmpeg'):
            parser.error('Install FFmpeg: brew install ffmpeg')
        subprocess.run(['ffmpeg', '-hide_banner', '-f', 'avfoundation', '-list_devices', 'true', '-i', ''], check=False)
        return 0  # FFmpeg returns 1 after printing the inventory; no capture starts.
    if not 2 <= args.chunk_seconds <= 20 or not 1 <= args.threads <= 16:
        parser.error('Use --chunk-seconds 2..20 and --threads 1..16')
    if args.overlap_seconds is None:
        args.overlap_seconds = 1.0 if args.chunk_seconds == 2 else 1.5
    if args.holdback_seconds is None:
        args.holdback_seconds = .2 if args.chunk_seconds == 2 else .5
    if not .5 <= args.overlap_seconds <= 2 or not 0 <= args.holdback_seconds < args.overlap_seconds:
        parser.error('Use --overlap-seconds 0.5..2 and --holdback-seconds >=0 and less than overlap')
    args.device = args.device or 'BlackHole 2ch'
    os.environ.setdefault('HF_HOME', str(ROOT / 'models' / 'hf-cache'))
    import numpy as np
    from faster_whisper import WhisperModel
    print('Loading local %s model on CPU...' % args.model, file=sys.stderr, flush=True)
    model = WhisperModel(args.model, device='cpu', compute_type='int8',
                         cpu_threads=args.threads, download_root=str(ROOT / 'models'),
                         local_files_only=args.offline)
    writer = TranscriptWriter(args.output_dir, uuid.uuid4().hex)
    source = AudioInput(args)
    transcriber = Transcriber(args, model, source, writer, np)
    interrupted = False

    def stop(signum, frame):
        nonlocal interrupted
        if interrupted:
            raise KeyboardInterrupt
        interrupted = True
        print('Stopping capture; finishing buffered audio...', file=sys.stderr, flush=True)
        source.close()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        source.start()
        transcriber.save_status('listening')
        print('Listening. Processing %g-second chunks with %g-second overlap. Press Ctrl+C to stop.'
              % (args.chunk_seconds, args.overlap_seconds), file=sys.stderr, flush=True)
        last_audio = time.monotonic()
        while not source.done.is_set() or not source.queue.empty():
            try:
                block = source.queue.get(timeout=.5)
            except queue.Empty:
                if source.stop.is_set():
                    break
                if time.monotonic() - last_audio > 15:
                    raise RuntimeError('No audio bytes arrived for 15 seconds; check source/device permissions')
                continue
            last_audio = time.monotonic()
            transcriber.add(block)
        if source.error:
            raise RuntimeError(source.error)
        transcriber.transcribe(final=True)
        transcriber.save_status('stopped' if interrupted else 'completed')
        print('Saved %s transcript records.' % writer.count, file=sys.stderr)
        if writer.count == 0 and not interrupted:
            raise RuntimeError('Audio source ended without recognized speech; inspect live_status.json for levels')
        return 0
    except Exception as exc:
        transcriber.save_status('error', str(exc))
        print('ERROR: %s' % exc, file=sys.stderr, flush=True)
        return 1
    finally:
        source.close()
        writer.close()


if __name__ == '__main__':
    sys.exit(main())
