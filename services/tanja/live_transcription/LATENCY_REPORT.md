# Measured local transcription latency — 2026-10-11

**The current pipeline does not consistently produce text within three seconds
of speech. The recent boundary-word correction also fails its focused regression.**
Two actual local-model executions were performed; no application code, trading
code or model settings were changed. No paid APIs, orders or webhooks were used.

## Two-minute continuous-input measurement

The permitted source is our own script synthesized locally with the installed
macOS Samantha voice. It is **not Tanja, YouTube or browser audio**. The exact
120-second, mono 16 kHz recording contains 24 phrases placed at known sample
offsets, with differing phases relative to the five-second windows.
`tests/latency_fixture/reference.json` records each phrase's acoustic start/end
and the recording's SHA256; `permitted_120s.wav` is the input.

The benchmark waits for the existing local model to become ready, then sends PCM
at playback speed in 20 ms packets. A separate observer polls the transcript
JSONL every 10 ms and timestamps when each complete line becomes readable. Speech
times come from waveform placement/activity, **not Whisper's estimated word
timestamps**. Acoustic boundary uncertainty is approximately ±20 ms. Measured
delivery jitter averaged 3.16 ms, P95 4.81 ms, maximum 40.70 ms. Model loading and
YouTube streaming latency are excluded.

| Measurement | Samples | Average | Median | P95 | Maximum |
| --- | ---: | ---: | ---: | ---: | ---: |
| Phrase end → complete matching text in file | 23 | 5.401 s | 5.213 s | 8.614 s | 10.554 s |
| Phrase onset → first matching text in file | 23 | 6.235 s | 5.729 s | 8.993 s | 10.354 s |
| Transcription processing, regular windows | 24 | 2.598 s | 2.235 s | 5.365 s | 6.236 s |
| Processing including final overlap flush | 25 | 2.522 s | 2.125 s | 5.365 s | 6.236 s |

P95 uses nearest rank: sorted value at `ceil(0.95 × sample_count)`.
These are phrase-level probes from one run, not per-word measurements or a
statistical guarantee across other recordings or machine loads. Matching ignores
case and punctuation but requires every reference word in order. Reference text
is used only for measurement, never injected into recognition.

- **23/24 complete phrases matched**; 24 transcript records were saved, including
  23 updates while the source was still sending audio. All 120 seconds / 3,840,000
  PCM bytes were received and processed; both timestamped transcript files exist.
- Only **1/23 matched complete phrases (4.35%)** appeared within three seconds of
  its acoustic end; **0/23** began appearing within three seconds of onset.
- Phrase 19, “An inversion may change the setup,” was recognized as “And inversion
  may change the setup.” It remains explicitly unscored, not assigned zero latency.
- Several lines include extra unspoken words or punctuation. Exact matching of a
  reference phrase does not mean its entire output record is error-free.
- There was one within-record repeated word: record 17 contains “Wait for the
  candle to close. Close.” No adjacent repeated word across records occurred in
  this particular two-minute sample.

## Audio window and processing delay are different

The unchanged default collects **5.0 seconds of new audio** before each normal
transcription. The first actual input window is 5.0 seconds; subsequent windows
are **6.5 seconds**, including **1.5 seconds of retained context**. EOF triggers
a final 1.5-second context flush with no new audio. `windows.jsonl` records actual
durations and processing time separately for every call.

Processing above measures the existing transcribe timer: local ASR plus word
collection, excluding the prior audio accumulation and subsequent file writing.
The external file-appearance measurement includes accumulation, any queue delay,
processing and output visibility. The app also defers words whose estimated end
falls within the last 0.5 seconds of a window; those words can wait for the next
five-second update. The observed peak input queue was 6.0 seconds, and two regular
calls took longer than five seconds. Five-second batching alone prevents a
consistent three-second guarantee for speech occurring early in a window.

The existing `audio_to_output_latency_s` is based on the **last recognized word**
in a record. It can mask earlier speech waiting within that record and is not the
independent ground-truth latency reported here.

## Boundary-word regression

The second focused execution used the existing 37.918-second permitted synthetic
fixture through the existing live-input harness. Its simple overlap unit checks
passed, but the actual ASR regression **failed**:

```text
AssertionError('An accidental repeated boundary word was emitted')
```

Record 1 ends with “transcription application.” at audio end 4.14 s.
Record 2 starts with “application, the audio…” at audio start 4.36 s. “Application”
is spoken only once in the source script. Eight records were saved, seven while
audio continued. The deliberately repeated “waiting for confirmation before”
phrase is present twice in saved output, but the harness stopped at the earlier
duplicate assertion, so its later repetition assertion was not reached.

The recent correction is therefore **not verified as working**. The saved
transcripts do not preserve all raw overlapping word hypotheses; they prove the
emitted duplicate but do not establish which internal deduplication gate failed.
No further correction or third transcription run was attempted.

## Reproduce and inspect

From this module folder, using the already-installed local model and fixtures:

```sh
cd "/Users/aleksandra/Desktop/NQ Signals/AgentSignals-main/services/tanja/live_transcription"
.venv/bin/python tests/measure_latency.py --out examples/latency-retest
.venv/bin/python tests/run_focused.py live --output-dir examples/boundary-retest
```

Use fresh output directories. The second command is expected to expose the
reported regression; it must not be represented as a passing test. Recognition
and timing may vary between executions. If the two-minute fixture needs to be
regenerated, run `.venv/bin/python tests/measure_latency.py --prepare` in ordinary
macOS Terminal; it uses installed `say` and FFmpeg, with no audio upload.

Saved evidence:

- `examples/latency_120s/summary.json`: aggregate statistics and coverage.
- `examples/latency_120s/timeline.csv` and `phrase_latencies.json`: all 24 known
  speech intervals, appearance times, delays and the unmatched phrase.
- `examples/latency_120s/appearance.json`: independent observed line times,
  source clock, delivery jitter and application hash.
- `examples/latency_120s/windows.jsonl`: actual audio windows and processing times.
- `examples/latency_120s/live_transcript.jsonl`, `live_transcript.txt`,
  `live_status.json`, `process.log`: actual recognition and receipt evidence.
- `examples/boundary_regression_20261011/test_result.json`, transcript files and
  `process.log`: the failed boundary regression.

Only `tests/measure_latency.py` and measurement documentation were added/updated;
the runtime application is unchanged. Its SHA256 before and after is
`6c72af7771464db08fb083913be1bd2143d0d561432c139b807cb238ef901629`.
Postprocessing saved observations into statistics/CSV required no additional ASR
execution. Actual YouTube/browser-loopback capture remains outside this test.
