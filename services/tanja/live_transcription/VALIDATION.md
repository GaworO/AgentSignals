# Actual validation results — 11 October 2026

Two focused transcription runs were executed, using the real local faster-whisper
model. No additional transcription run was made after the limit was reached.
The application runs and receives/transcribes continuous PCM audio. Full acceptance
is **not yet met**: the second run found a repeated boundary word, and the final
narrow deduplication correction is unverified.

## Permitted source

`tests/synthetic_speech.wav` is a 37.918-second, 16 kHz mono recording generated
locally by the installed macOS Samantha voice from our original test script.
It is not Tanja, a downloaded video, or YouTube Live. The script deliberately
repeats one complete sentence to distinguish spoken repetition from duplicate
overlap output. `tests/source.json` records provenance and the audio hash.

The first fixture-generation attempt inside the restricted sandbox created an
empty WAV. This was caught by checking its audio metadata before transcription.
It was regenerated successfully using the installed Mac speech service outside
that restriction, and a zero-audio guard was added to the fixture generator.
Dependency/model installation and fixture preparation did not invoke transcription.

## Run 1: local file

```sh
.venv/bin/python tests/run_focused.py local
```

Result: **passed the original focused checks**. The app received 1,213,382 PCM
bytes and wrote eight chronological, timestamped transcript records. All six
checked terms were recognized: entry, displacement, liquidity, profit, stop and
position. Mean inference processing was 2.0397 seconds per window, maximum 2.6852
seconds; total test duration including initialization was 19.175 seconds.

The original check detected no duplicated adjacent complete records. Inspection
then found the boundary word “application” repeated across the first two lines.
The overlap timestamp tolerance was increased and a stricter boundary-word check
was added before the second test. Therefore the first pass does not establish
complete duplicate suppression.

Evidence: `examples/local/process.log`, `test_result.json`,
`live_transcript.jsonl`, `live_transcript.txt` and `live_status.json`.

## Run 2: paced continuous input

```sh
.venv/bin/python tests/run_focused.py live
```

The harness started the transcriber, waited for the model to become ready, then
used FFmpeg `-re` to feed the permitted recording as a continuing PCM source at
playback speed. This exercised `--stdin-pcm`, the same chunk/transcription/writer
path used by the device adapter. It did not exercise macOS hardware capture.

The application completed normally and wrote eight records. The harness observed
seven separate file updates while the audio producer was still running, beginning
7.706 seconds after test launch. Received audio: 37.918 seconds; maximum queued
audio: 2 seconds. Mean processing: **1.6815 seconds per window**, maximum 2.151.
Mean measured audio-end-to-output latency: **2.0736 seconds**, maximum 2.4444.
These latency values describe the local paced test, including buffering and
inference; they do not measure YouTube or broadcast latency.

The **overall harness result failed** because the same boundary word was still
repeated. Timestamp, audio receipt, speech recognition, file consistency and
complete-record duplicate checks had passed before that assertion. The saved
output visibly retains the deliberately repeated sentence twice; the later
assertion for it and the live-update assertions were not reached after failure.
The independently saved polling log nevertheless records all seven live updates.

Evidence: `examples/live/process.log`, `test_result.json`,
`live_transcript.jsonl`, `live_transcript.txt` and `live_status.json`.

## Final correction and remaining work

After the second run, the bounded overlap matcher was changed to ignore empty
punctuation anchors and allow word endpoints to shift within the retained
1.5-second overlap. This is a general overlap rule, not a hardcoded word or a
transcript rewrite. **No test was run on that final change**, because the two-run
budget was exhausted. Its effectiveness and treatment of immediate intentional
repetitions remain unverified. The prior failed result was preserved unchanged.

`validation_summary.json` explicitly marks `live_test_passed: false`,
`final_dedup_patch_tested: false` and `definition_of_done_fully_met: false`.
The initial files in `output/` are unmodified copies of the live-style example,
clearly identified by `output/EXAMPLE_SOURCE.md`; the capture-time originals and
the update polling evidence remain in `examples/live/`.

## Browser audio blocker

An unsandboxed, read-only AVFoundation inventory found the MacBook and phone
microphones but no BlackHole input. No microphone or browser audio was recorded.
Actual YouTube Live status was not checked and no YouTube Live transcription was
claimed. Browser capture requires BlackHole installation, a Multi-Output Device
and the one-time macOS microphone permission for the terminal, as documented in
README.md. Once routed, the simple start command is:

```sh
.venv/bin/python live_transcriber.py --offline
```

All application code, vocabulary, environment, model cache, fixture and output
files are inside this Tanja subproject. Only three referenced NoteGPT transcripts
were used for terminology. No production service files, trading execution,
TradersPost requests, broker orders, paid APIs, subagents or strategy research
were involved.
