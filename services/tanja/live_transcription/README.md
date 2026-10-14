# Tanja local live transcription

Run this on the Mac that plays YouTube. Speech recognition runs locally with
faster-whisper `base.en` on the CPU. There is no trading integration or paid API.
The application accepts a Mac audio input, a permitted local audio/video file,
or continuously arriving raw audio on stdin.

## Start

From this folder, after the one-time audio setup below:

```sh
.venv/bin/python live_transcriber.py --offline
```

The decoder remains `--beam-size 3` with five-second chunks. The tested beam-size-one
option was faster but failed the numeric guard. See [BEAM_COMPARISON.md](BEAM_COMPARISON.md)
for measured results.

This selects **BlackHole 2ch**. Start the public video normally in your browser.
Leave the program running while the source plays; no manual session recording is
needed. It appends text approximately every five seconds as speech arrives.
Press **Ctrl+C** once to stop capture and finish buffered audio; press it again
to interrupt immediately.

## One-time macOS installation

The dependencies and free `base.en` weights are already installed locally for
this delivery. On another Mac, use an existing Homebrew installation:

```sh
cd "/Users/aleksandra/Desktop/NQ Signals/AgentSignals-main/services/tanja/live_transcription"
brew install ffmpeg
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -c 'from faster_whisper import WhisperModel; WhisperModel("base.en", device="cpu", compute_type="int8", download_root="models")'
```

Python 3.9+ is supported; this delivery used Python 3.9.6 on Apple Silicon,
faster-whisper 1.2.1, CTranslate2 4.8.2, NumPy 2.0.2 and PyAV 15.1.0.
Model weights download once from Hugging Face into `models/`. After downloading,
`--offline` prevents model-network access. No audio is uploaded.

## Route browser audio once

The device inventory on this Mac showed the MacBook and phone microphones, with
**no BlackHole device**. Chrome capture therefore remains untested and requires:

1. Install the free loopback driver: `brew install --cask blackhole-2ch`.
   Complete its administrator prompt; restart audio applications or the Mac if
   the installer requests it.
2. Open **Audio MIDI Setup → + → Create Multi-Output Device**. Enable the MacBook
   speakers/headphones and BlackHole 2ch. Use the physical output as the clock
   source and enable drift correction for BlackHole. Match their sample rates.
3. Select that Multi-Output Device in macOS Sound Output, then play YouTube in
   Chrome. Audio goes to both your speakers and BlackHole.
4. Start the transcriber in Terminal. Allow the macOS microphone permission for
   the terminal app when requested. If denied, enable it in **System Settings →
   Privacy & Security → Microphone**, then restart the terminal.

These are the [official BlackHole routing instructions](https://github.com/ExistentialAudio/BlackHole/wiki/Multi-Output-Device).
Only use audio you are permitted to process. No YouTube extraction, downloading,
login bypass or browser automation is used. Loopback captures other system audio
too; mute notifications and other applications during a session.

## Choose an input

```sh
.venv/bin/python live_transcriber.py --list-devices
.venv/bin/python live_transcriber.py --offline --device "BlackHole 2ch"
```

Use the exact name from the **AVFoundation audio devices** list, or its audio
index. Camera/video indices are a separate list. FFmpeg normally prints an
input error after listing devices; that inventory command does not record.
If the list is empty inside a restricted coding sandbox, run it in ordinary
Terminal. Selecting a built-in microphone records room sound, not direct Chrome
audio. FFmpeg converts the selected device's audio to 16 kHz mono automatically.

## Local recording and live-style input

Local file:

```sh
.venv/bin/python live_transcriber.py --offline --file tests/synthetic_speech.wav
```

Continuous source arriving at playback speed:

```sh
ffmpeg -nostdin -v error -re -i tests/synthetic_speech.wav -f s16le -ar 16000 -ac 1 pipe:1 | .venv/bin/python live_transcriber.py --offline --stdin-pcm
```

Stdin must be signed 16-bit little-endian mono PCM at 16,000 Hz. The app loads
the model before listening. The test harness waits for readiness before starting
the source. This piped path is a permitted **live-style test**, not an actual
YouTube Live or hardware-loopback test.

## Output

- `output/live_transcript.jsonl`: one JSON record per new recognized segment.
- `output/live_transcript.txt`: readable timestamped text.
- `output/live_status.json`: current audio levels, received bytes, processed
  duration, record count, queue size and timing; explicit errors when capture fails.

Both transcript files are flushed after each update, while input continues. Files
append across launches; `session_id` separates sessions and audio timestamps start
at zero for each session. Wall-clock timestamps are UTC. `--output-dir PATH` selects
another folder; two processes cannot write to the same folder simultaneously.

Each record contains `audio_start_s`, `audio_end_s`, `wall_clock_utc`, original
recognized `text`, `processing_latency_s`, model and session identifiers. Live
inputs additionally record `audio_to_output_latency_s`: elapsed time from the
estimated end of the captured speech to emission. This includes windowing, queue
and inference delay, but cannot measure YouTube's broadcast/player delay. Local
file processing reports that field as null. Timestamps describe audio received
by this process, not the recording's video clock or a trading fill time.

Records also include `beam_size` and unclassified `order_type`, `order_state`,
`price` fields, all UNKNOWN until a separate interpreter evaluates explicit current
speech. `interpretation_status` is UNCLASSIFIED_TRANSCRIPT. The transcriber never
assumes MARKET or converts a pending LIMIT into a confirmed position.
See [ORDER_LANGUAGE.md](ORDER_LANGUAGE.md) for source-backed order expressions,
MARKET/LIMIT examples and ORDER_PLANNED / ORDER_PLACED / ORDER_FILLED distinctions.

## Vocabulary and transcription behavior

`tanja_vocabulary.json` was built from three existing NoteGPT sources referenced
by this Tanja project: May 19, May 22 and October 2, 2026. It records source hashes
and small matching excerpts. Forty terms were found verbatim; eight requested
terms are explicitly marked as not found verbatim. Only the small terminology
list enters Whisper's prompt, never the historical quotes, prices or strategy.

The order-language update adds 13 verbatim expressions across nine categories,
including limit placement, pending/fill/cancellation, price changes, stops and
exits. Exact source offsets/hashes and available block timestamps are preserved;
missing timestamps, ambiguous prices and uncertain order types remain UNKNOWN.
Historical examples and SIM demonstrations are explicitly non-actionable.

Recognition is English transcription, not translation. Five-second windows retain
1.5 seconds of overlap and defer the final half-second until the next update.
Word timestamps and overlap-text comparison suppress repeated output while allowing
genuine repetitions later in the audio. Silence filtering and a low-level gate
reduce blank-audio hallucinations. Raw recognition spelling, punctuation and
numbers are retained; uncertain words are flagged without numeric correction.
ASR can still miss or mishear words, especially abbreviations and prices.
The original two-run validation found one repeated boundary word. A final narrow change
extends timestamp matching to the retained 1.5-second overlap and ignores empty
punctuation anchors. The subsequent 2026-10-11 focused regression **still failed**:
“application” was emitted twice across a record boundary. Close, intentional
word repetitions remain an accuracy edge case. Do not treat deduplication as proven.
The subsequent [chunk comparison](CHUNK_COMPARISON.md) added a span-aware correction
and passed five synthetic timestamp checks; the historical 37.9-second ASR
regression was not rerun. Two-second chunks overloaded the decoder, so the
five-second default remains in place.

If processing falls more than 20 seconds behind a live input, capture fails
explicitly instead of silently dropping audio. `--model tiny.en` reduces CPU cost
after downloading that free model once. This MVP does not reconnect a disconnected
device or start the browser for you.

## Reproduce the focused checks

The supplied WAV is our own script rendered by macOS Samantha. It is **not Tanja's
voice** and contains no real trading instruction. Provenance is in
`tests/source.json`; recreate it with `.venv/bin/python tests/create_fixture.py`
from ordinary Terminal if needed. No microphone or paid API is involved.

Use fresh test output folders so prior evidence is preserved:

```sh
.venv/bin/python tests/run_focused.py local --output-dir output/retest-local
.venv/bin/python tests/run_focused.py live --output-dir output/retest-live
```

The checks run the actual local model. They validate received audio, recognized
speech, timestamps, latency, chronological append, overlapping-word deduplication
and preservation of an intentionally repeated spoken sentence. The live check
polls output files while FFmpeg is still sending audio and requires at least three
separate visible updates before EOF. Test results and process logs accompany the
example transcripts. No service, broker, TradersPost or strategy tests are invoked.

See `VALIDATION.md` for the two actual test results and the precise browser-capture
limitation. Model implementation: [faster-whisper](https://github.com/SYSTRAN/faster-whisper).

## Independently measured latency

See [LATENCY_REPORT.md](LATENCY_REPORT.md) for the two-minute continuous-input
measurement and duplicate regression. Average phrase-end-to-complete-text delay
was 5.401 seconds; P95 was 8.614 seconds. Only one of 23 exactly matched phrases
appeared within three seconds. This excludes YouTube streaming latency and uses
known waveform times, rather than the app's estimated last-word timestamp.

Reproduce with the supplied permitted fixture and a fresh output folder:

```sh
.venv/bin/python tests/measure_latency.py --out examples/latency-retest
```

See [CHUNK_COMPARISON.md](CHUNK_COMPARISON.md) for the fresh five-second baseline,
the failed two-second trial, exact configuration commands and remaining limits.
`--overlap-seconds` and `--holdback-seconds` allow the existing chunking behavior
to be configured without replacing the model or audio capture path.

## Existing Railway service: transcript ingestion

`../transcript_ingestion.py` adds transcript session, attempt, event and Shadow
observation tables to the existing `Store` database, `DATA_DIR/tanja.sqlite3`.
It runs as another thread of the existing single-worker application. No new
service or production database is required. Ingestion defaults to disabled.

The inspected Railway Tanja variables contain an OpenAI key but no audio source.
Live YouTube access remains blocked: a watch URL is not an authorized server
audio feed. The Mac BlackHole route cannot supply audio while the laptop is off.

Once an authorized publisher feed is available, configure the same service:

- `TANJA_TRANSCRIPT_ENABLED=true`
- `TANJA_TRANSCRIPT_PAID_APPROVED=true` only after approving ongoing API usage
- `TANJA_TRANSCRIPT_SOURCE_MANIFEST_URL=https://publisher.example/session.json`
- Reuse `OPENAI_API_KEY` and the existing persistent `DATA_DIR` volume.

The publisher manifest is JSON with `id` (stable broadcast ID), `live` (boolean),
`audio_url` (direct permitted HTTPS audio), `started_at` (Unix UTC seconds) and
`authorization` (permission reference). `live=false` means wait; the worker polls
every 30 seconds. YouTube watch/googlevideo URLs are rejected. No scraping,
caption fetching, or stream URL extraction is implemented.

Initial operation has a persisted ten-minute audio ceiling per source session,
including reconnects (~$0.17 transcription at documented pricing). Reconnects
continue with a new attempt and an explicit gap; the application does not request
replay. Provider-side buffering or repeated audio must be tested with the actual
authorized live source; live disconnect recovery has not been proven.
This ceiling is a bounded MVP limit, not full-session production coverage.
File fixtures resume after their saved sent-audio checkpoint; a completed fixture
is never retranscribed. Interrupted unfinalized output remains unknown.

Authenticated `GET /api/transcripts?after=SEQUENCE` returns incremental and final
events with a stable database cursor. `GET /api/plans` includes
`transcript_observations`, exposing final transcripts to the existing Shadow
observer. These are unverified transcript observations, not confirmed trades.
`GET /api/transcripts/shadow?after=SEQUENCE` provides paginated final observations;
consumers must advance `next_sequence` rather than repeatedly reading page one.
Raw provider text is retained; delta events require provider event IDs for safe
deduplication. Repeated words with distinct event IDs are preserved. Conflicting
duplicates are rejected. Capture intervals are not model word timestamps;
reconnect gaps are recorded separately. No order sender is imported or activated.

Focused test (run from `services/tanja`):

```sh
live_transcription/.venv/bin/python tests/test_transcript_ingestion.py
```

This streams the original permitted local speech fixture at playback speed,
recognizes actual PCM using cached local `base.en`, and emulates the GPT transport
events to test the same database handler. It is not an OpenAI or Railway audio
test. Evidence: `output/ingestion_fixture_test/result.json`; the isolated test
volume uses the existing Store/schema, leaving production data untouched.

Approved GPT test with an API key already in the terminal environment:

```sh
live_transcription/.venv/bin/python tests/run_gpt_ingestion_fixture.py --paid-approved
```

The test is capped at 38 seconds (~$0.011 estimated transcription), saves evidence
under `output/gpt_ingestion_fixture_test/result.json`, and sends no trading requests.
`--railway-key` optionally reads the key from the existing linked Tanja project
without printing or saving it; it requires Railway CLI login. This is local
fixture capture, not proof that Railway receives live YouTube audio.

The approved GPT fixture run saved 126 deltas, eight finals and eight Shadow
observations from 37.918 seconds of audio while playback continued. Estimated
transcription cost: $0.01077 (actual invoice not checked). Initial validation
mistakenly counted only the first 100-row page; it was corrected and the stored
database was rechecked without another paid call. Completed-source recovery
returned without connecting or adding duplicate events. Nine focused local
storage/protocol/audio tests also passed. No real Tanja audio was used in these
fixture tests and no trading accuracy is asserted.

Recheck saved GPT evidence without audio playback or any API call:

```sh
live_transcription/.venv/bin/python tests/run_gpt_ingestion_fixture.py --verify-existing
```

Docker adds FFmpeg and `websocket-client`. Preserve the existing Gunicorn startup,
one worker/replica, and volume. No deployment was performed for this change and
the outdated supplied ZIP must not replace current sources.
