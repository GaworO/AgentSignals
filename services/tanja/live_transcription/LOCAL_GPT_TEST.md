# Local live GPT transcription test

Run in the existing project root while Chrome plays permitted audio through a
Multi-Output Device that includes BlackHole 2ch.

Start:

```sh
services/tanja/live_transcription/.venv/bin/python services/tanja/live_transcription/local_gpt_test.py --railway-key
```

Stop from another terminal:

```sh
pkill -INT -f '[l]ocal_gpt_test.py'
```

Ctrl+C in the capture terminal also stops it. Each invocation sends at most
110 seconds of audio and shuts down the paid connection within 120 seconds.
There is no retry or reconnect. A three-second local audio check rejects silent
routing before connecting to GPT.

The adapter reuses `live_transcriber.AudioInput`, `transcript_ingestion.gpt_session`,
`TranscriptJournal`, `Store`, and `transcript_trade_interpreter.Interpreter`.
It converts AVFoundation input to mono PCM16 at 24 kHz, as specified by the
[official OpenAI transcription documentation](https://developers.openai.com/api/docs/guides/realtime-transcription).
Incremental text and detected MARKET, LIMIT, FILL, PARTIAL, STOP and EXIT labels
print in the terminal. Interpreter results retain evidence and ambiguity fields.
Reported fills remain unverified speech observations.

Each run saves a local `tanja.sqlite3`, `transcript.jsonl`,
`shadow_observations.jsonl`, and `result.json` under
`output/local_gpt_test/<run>/`. Existing Shadow transcript observations include
the structured decisions. Every observation is non-executable; this entry point
does not load the application, workers, order sender or paper execution engine.

`--railway-key` reads the existing Tanja credential using the same read-only CLI
lookup as the existing fixture test. It never prints or saves the key and makes
no Railway changes. Set `OPENAI_API_KEY` locally and omit this flag to avoid that
lookup. No TradersPost requests, real trading or deployment are performed.

The report measures final-text delay from the estimated end of each captured
five-second interval, plus first-delta delay from its beginning. These are local
capture-to-text measurements; exact phrase timing and YouTube broadcast delay
are not available from this test.

## Observed run, 12 October 2026

Evidence: `output/local_gpt_test/20261012_160510_13b4f4/result.json`.
The connection automatically closed after 118.015 seconds, having sent 102.8
seconds of system audio. There were 284 deltas, 20 finals and 20 persisted Shadow
observations. MARKET/LIMIT/FILL/PARTIAL/STOP/EXIT counts were all zero.
Mean final-text delay after the estimated capture-window end was 7.4795 seconds;
P95 was 13.0342 seconds. The wall limit interrupted the run with
`TimeoutError: 120_SECOND_WALL_LIMIT`; the last 2.8 seconds have no final transcript.
No additional paid run was performed. The process and BlackHole capture stopped.

Validation: 17 existing ingestion/interpreter checks and three offline adapter
checks passed. The adapter checks covered out-of-order and split speech, all six
event labels, non-executable Shadow persistence, the byte ceiling and timed
shutdown without an API call.
