# Five-second versus two-second chunks — 2026-10-11

**Keep the five-second configuration for now. Two-second chunks overload the
current local decoder and fail before completing the source. The three-second
P95 target was not achieved.** No model replacement, trading changes, paid APIs
or browser automation were used. Changes are confined to this module.

## Actual model executions

Both executions attempted the same existing permitted 120-second recording,
`tests/latency_fixture/permitted_120s.wav`. It is locally synthesized test speech,
not Tanja or YouTube. The reference contains 24 independently timed phrases.
The model remained `base.en`, CPU int8, four threads, beam size three.

| Configuration | Baseline | Candidate |
| --- | ---: | ---: |
| New audio per chunk | 5.0 s | 2.0 s |
| Retained overlap | 1.5 s | 1.0 s |
| Actual normal model input | 6.5 s after first chunk | 3.0 s after first chunk |
| End-of-window word holdback | 0.5 s | 0.2 s |
| Deduplication | Previous implementation | Updated span-aware implementation |
| Audio processed | 120.0 s | 78.0 s, then failure |
| Exact phrase matches | 23/24 | 14/24; 14/16 within processed audio |
| Repeated words across records | 0 | 0 in partial output |
| Adjacent repeated words within records | 1 (“Close”) | 1 (“No”) |
| Processing / processed audio duration | 0.469 | 1.434 |
| Normal chunks slower than incoming chunk interval | 0/24 | 33/39 |
| Peak input queue | 4.0 s | 20.0 s |
| Outcome | Completed | Backlog safety limit stopped capture |

Case and punctuation are ignored for phrase matching, but all reference words
must occur consecutively in order. Reference labels never enter model inference.
The candidate added “of” in “the order is waiting for a fill” and recognized
“profits” instead of “profit” in phrase 16. Eight later reference phrases were
not processed; they are not classified as eight ASR recognition errors.

## Latency and processing

| Measurement | Mean | Median | P95 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| Baseline phrase end → complete text in JSONL | 5.123 s | 4.666 s | 9.229 s | 9.329 s |
| Baseline phrase onset → first matching text | 5.889 s | 5.695 s | 9.029 s | 9.129 s |
| Baseline normal-chunk processing | 2.313 s | 2.289 s | 4.208 s | 4.472 s |
| Candidate normal-chunk processing, before failure | 2.869 s | 2.476 s | 5.642 s | 6.028 s |

Baseline latency uses external 10 ms polling of complete transcript lines against
known waveform times. P95 is nearest rank. The model loads before the source
starts. Only matched phrases are scored; these measurements exclude YouTube
streaming latency. The earlier baseline mean 5.401 s / P95 8.614 s is preserved
in `LATENCY_REPORT.md`; the fresh baseline above shows normal run-to-run timing
variation, not an improvement caused by a code change.

**There is no valid full-source candidate speech-to-file latency result.** The
candidate's source writer experienced backpressure, and capture failed before
120 seconds were processed. The original harness also discarded its in-memory
external observations on the failed exit. Reporting a comparable, successful
120-second candidate latency distribution would be incorrect.

For diagnosis only, recovery from saved app pre-write emission times relative to
the originally scheduled sample clock gives these **partial estimates**, not
externally measured file-appearance latency:

| Recovered candidate estimate, 14 matches | Mean | Median | P95 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| Scheduled phrase end → emission | 15.947 s | 13.462 s | 35.479 s | 35.479 s |
| Scheduled phrase onset → first emission | 16.065 s | 12.456 s | 34.516 s | 34.516 s |

Backpressure changed source delivery, so these estimates must not be treated as
precise actual speech-occurrence latency or compared numerically as equivalent
to the baseline. Their limited provenance is recorded in the candidate summary.
The processing timings and 20-second queue failure are direct measurements and
already establish that this configuration cannot sustain real-time input.

## Narrow code changes and duplicate checks

`live_transcriber.py` now accepts `--overlap-seconds` and `--holdback-seconds`.
The five-second defaults remain 1.5 s / 0.5 s. Selecting two-second chunks defaults
to 1.0 s / 0.2 s. Startup and status files expose the selected configuration.

The existing suffix/prefix deduplication now ignores standalone punctuation on
both sides and compares nearby word spans instead of requiring endpoints to
remain within a fixed 1.5-second difference. Its match must be near the previous
emission boundary and anchored to audio retained in the new window. Word spelling,
attached punctuation and numbers remain untouched. It does not globally suppress
the same words spoken later.

Five synthetic timestamp checks passed inside the candidate benchmark: punctuation
anchors, a stretched duplicate endpoint, a later genuine repetition, multiword
overlap and a new word straddling the previous boundary. These are synthetic word
hypotheses, not evidence that the historical “application” ASR regression is fixed.
That 37.9-second source was not rerun in this comparison. The 120-second source
did not emit a cross-record repeated word in either configuration.

Applying the updated deduplication deterministically to the baseline's saved real
word hypotheses preserved all lexical output and 23/24 phrase recall. This was
postprocessing, not another ASR or latency execution. The updated five-second
runtime was not benchmarked again end to end, so its latency must not be claimed
as separately measured. Within-record ASR hallucinations remain outside this
boundary correction.

Capture already uses its own reader thread. During the baseline, received bytes
advanced during 23/24 normal processing calls; the last call occurred after the
source ended. All 3,840,000 PCM bytes were received. No additional thread framework
was added. Candidate inference needed 111.872 s to process 78 s of audio: asynchronous
capture cannot compensate for processing consistently slower than playback, and
the existing bounded queue correctly reported failure.

`tests/measure_latency.py` now records configuration, word hypotheses, actual
window sizes, capture progress and queue/backlog measures. It also preserves
external observations and execution errors on future failed runs. That failure
reporting change was made after this run; no third ASR execution was performed.
An initial candidate preflight import error was repaired before any model inference.

## Recommended command and smallest next change

Keep the existing model and five-second default:

```sh
cd "/Users/aleksandra/Desktop/NQ Signals/AgentSignals-main/services/tanja/live_transcription"
.venv/bin/python live_transcriber.py --offline --device "BlackHole 2ch" --chunk-seconds 5 --overlap-seconds 1.5 --holdback-seconds 0.5
```

BlackHole/browser routing and macOS permissions remain prerequisites; this task
tested paced local PCM, not actual browser capture. For permitted local playback:

```sh
ffmpeg -nostdin -v error -re -i tests/latency_fixture/permitted_120s.wav -f s16le -ar 16000 -ac 1 pipe:1 | .venv/bin/python live_transcriber.py --offline --stdin-pcm --chunk-seconds 5 --overlap-seconds 1.5 --holdback-seconds 0.5
```

The smallest next change is to expose decoding beam size and benchmark **beam size
one instead of three**, still with `base.en`, before another two-second trial.
This is a recommendation, not a measured speed/accuracy claim; it was not applied
as an untested default. More buffering would merely increase latency.

Exact benchmark commands (choose fresh folders for reruns):

```sh
.venv/bin/python tests/measure_latency.py --chunk-seconds 5 --out examples/chunk5-retest
.venv/bin/python tests/measure_latency.py --chunk-seconds 2 --overlap-seconds 1 --holdback-seconds 0.2 --check-dedup --out examples/chunk2-retest
```

The baseline above was measured before the runtime deduplication patch. A rerun
uses the updated implementation in both configurations, as appropriate for future
comparisons. Candidate failure remains visible as a nonzero command exit.

Evidence folders:

- `examples/chunk5_baseline_20261011/`: full summary, externally observed appearance
  times, phrase timeline, transcript, receipt/status, per-window timings and raw
  word hypotheses.
- `examples/chunk2_candidate_20261011/`: failed execution/status, partial transcript,
  configuration and passed timestamp checks, per-window timings, raw hypotheses,
  summary, `estimated_timeline.csv` and `estimated_phrase_latencies.json`.
- `examples/dedup_replay_check.json`: updated deduplication preserves the baseline
  lexical output on saved real ASR hypotheses.

The requested latency and recall criteria are **not achieved** by two-second
chunks. Five-second batching remains the best observed configuration in this test.
