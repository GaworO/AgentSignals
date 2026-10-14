# Beam comparison and MARKET/LIMIT vocabulary — 2026-10-11

**Keep beam size 3 as the default, with five-second chunks.** Beam size 1 was faster,
but neither configuration passed the numeric guard. No faster default was selected.

Two focused commands were executed. Each used the same existing permitted
120-second paced recording and then the same 18.924-second synthetic numeric
recording. The latter supplements the existing source, which has no numeric
prices. Both are original locally generated Samantha speech, not Tanja or YouTube.
There were no other models, beams or chunk sizes tested in this task.

Settings were identical apart from beam size: `base.en`, CPU int8, four threads,
5 s new audio, 1.5 s retained context, 0.5 s word holdback. Both application and
vocabulary hashes matched across runs. Historical expressions and numbers never
enter the ASR prompt; only the shared terminology list does.

| Measurement | Beam 3 | Beam 1 |
| --- | ---: | ---: |
| Speech end → complete text: mean | 4.956 s | 4.123 s |
| Speech end → complete text: median | 4.564 s | 4.022 s |
| Speech end → complete text: P95 | 8.237 s | 6.854 s |
| Speech end → complete text: maximum | 8.396 s | 6.946 s |
| Speech onset → first text: mean | 5.732 s | 5.108 s |
| Speech onset → first text: median | 5.676 s | 4.972 s |
| Speech onset → first text: P95 | 8.069 s | 6.654 s |
| Speech onset → first text: maximum | 8.196 s | 6.746 s |
| Regular-chunk processing: mean | 2.131 s | 1.575 s |
| Regular-chunk processing: median | 2.069 s | 1.544 s |
| Regular-chunk processing: P95 | 3.308 s | 1.860 s |
| Regular-chunk processing: maximum | 3.496 s | 2.646 s |
| Exact phrase recall | 22/24 | 23/24 |
| Repeated words across records | 0 | 0 |
| Within-record adjacent repeats | 1 (“Close”) | 2 (“price”, “Close”) |
| Peak input queue | 3.0 s | 2.5 s |
| Processing time / audio duration, including EOF flush | 0.432 | 0.320 |
| Regular calls slower than the 5 s arrival interval | 0/24 | 0/24 |
| Numeric guard | Failed | Failed |

Both pipelines completed all 120 s / 3,840,000 bytes and saved 24 records, with
23 updates while audio continued. Timestamp-aware deduplication checks passed in
both focused runs. Zero repeats across these records does not prove the historical
37.9-second “application” ASR regression is fixed; that recording was not rerun.

Latency comes from independent waveform timestamps and external 10 ms JSONL polling;
model initialization and YouTube latency are excluded. P95 uses nearest rank.
Only matched phrases are scored, so the latency distributions have different
subsets: beam 3 missed “Move the stop to break even” and “An inversion may change
the setup”; beam 1 missed only the inversion phrase. Earlier beam-3 measurements
used the earlier vocabulary and must not be mixed with this paired comparison.

## Why beam size 1 was not promoted

The shared numeric source tests 123.25, 120.50, 130.75, 124.50, five contracts and
“My limit order is not filled.” Both beams preserved quantity and the negation,
but both split the target across records as `130` and `.75`, producing raw combined
text `130 .75`. That expression remains **UNKNOWN**; it was not repaired to 130.75.
Three of four complete numeric surface forms were found in each output. Beam 3
also flagged `124` as low confidence, so its limit-change price needs UNKNOWN
handling. Beam 1 emitted `$124.50` without a flagged numeric token, but the target
failure still fails its gate. Extra unspoken “field” text follows the negated
fill statement in both guard outputs.

Beam 1 reduced mean processing by approximately 26% and mean speech-end delay by
approximately 17%. It is not the default because the numeric acceptance criterion
failed, and it added a within-record repeated word. Beam 3 is retained as the
existing default, **not declared numerically reliable**. Neither reaches a 3 s
P95 speech-end target. This small synthetic test is not a guarantee on Tanja's voice.

The scorer was corrected to accept sentence-final punctuation after a complete
decimal. Beam-3 saved output was rescored without new ASR; the same criterion was
used for both. Separated decimal fragments and uncertain digits still fail.
Expected values are test ground truth and never rewrite transcription.

## Changes and order interpretation

`live_transcriber.py` accepts `--beam-size 1|3`, default 3, and records the selected
beam. Raw text and timestamps remain intact. Added `order_type`, `order_state` and
`price` fields default to UNKNOWN, with `interpretation_status: UNCLASSIFIED_TRANSCRIPT`.
No automatic order classifier, position tracker or execution code was added.

`tanja_vocabulary.json` contains 13 verbatim expressions in the nine requested
categories. Sources include exact hashes, lines, character offsets and available
block timestamps. Missing timestamps remain UNKNOWN. All source offsets/hashes
were checked. Misleading generic “fill”/“filled” examples were replaced with actual
order-language excerpts. Historical quotes are non-actionable and excluded from
ASR prompting. Ambiguous cancellations and SIM demonstrations are labeled.

`ORDER_LANGUAGE.md` supplies MARKET/LIMIT examples and separates ORDER_PLANNED,
ORDER_PLACED and ORDER_FILLED. Pending limits do not establish a position; touched
prices, gap fills, historical examples and SIM demonstrations never confirm a
current fill. An unlinked “filled” statement leaves order type UNKNOWN. There was
no explicit numerical limit-price modification in the selected excerpts; the
drag example is a SIM demonstration with unknown order and price.

## Start and reproduce

From this module folder, keep the current configuration:

```sh
.venv/bin/python live_transcriber.py --offline --chunk-seconds 5 --beam-size 3
```

This uses the existing BlackHole default; driver/routing/permission setup remains
required. Browser capture was not tested here. Local continuous playback:

```sh
ffmpeg -nostdin -v error -re -i tests/latency_fixture/permitted_120s.wav -f s16le -ar 16000 -ac 1 pipe:1 | .venv/bin/python live_transcriber.py --offline --stdin-pcm --chunk-seconds 5 --beam-size 3
```

Exact focused commands, using fresh output folders for reproduction:

```sh
.venv/bin/python tests/measure_latency.py --chunk-seconds 5 --overlap-seconds 1.5 --holdback-seconds 0.5 --beam-size 3 --check-dedup --check-numeric --out examples/beam3-retest
.venv/bin/python tests/measure_latency.py --chunk-seconds 5 --overlap-seconds 1.5 --holdback-seconds 0.5 --beam-size 1 --check-dedup --check-numeric --out examples/beam1-retest
```

Completed benchmark commands print quality gates in JSON; process exit success
does not mean the numeric gate passed. The saved fixtures are ready to use.
`.venv/bin/python tests/beam_checks.py` regenerates only the numeric source in
ordinary Terminal, using installed `say` and FFmpeg; it performs no ASR itself.

Outputs:

- `examples/beam_comparison.json`: metrics, selected default and matched hashes.
- `examples/beam3_20261011/` and `examples/beam1_20261011/`: summary, timeline,
  externally observed appearance times, transcripts/status, windows and hypotheses.
- Each folder's `numeric_check.json`, `numeric_process.log`, `numeric_guard/`:
  numeric output, uncertainty flags, negation check and pass/fail result.
- `tests/beam_numeric/`: original spoken script, permitted WAV and source hash.

Changes are confined to Tanja's live_transcription module. No paid APIs, subagents,
production trading changes, broker orders or TradersPost requests were used.
