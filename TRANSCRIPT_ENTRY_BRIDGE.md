# Local transcript entry bridge — test delivery only

The existing live transcriber now passes finalized interpreter events to
`transcript_entry_bridge.EntryBridge`. Candidate payloads and rejection reasons
are attached to the existing Shadow transcript observations. Incremental GPT
deltas never trigger an entry.

To include the configured contract in a future bounded capture run, use the
existing command with `--entry-contract MNQZ2026`. Live capture produces local
previews only. The prior two-minute test has stopped; no additional paid audio
run was performed to validate this bridge.

The bridge requires a current reported MARKET or LIMIT placement, explicit
MNQ, direction, quantity, entry/reference price and numeric stop in the current
quote. It does not infer missing prices, use later statements to backfill an
earlier entry, convert ES/NQ to MNQ, or interpret the speaker's fill as our fill.
It rejects intentions, conditional/negated statements, stale/unknown/future
capture times, contradictory transitions, invalid quarter-point prices and
incorrect stop/target directions.

This test phase accepts exactly one explicitly quoted contract. That restriction
is a test boundary, not an account-sizing policy. Live orders and account sizing
remain disabled. Each payload has `test: true`; the network boundary revalidates
that flag and the complete payload before sending. Signals expire after 15 seconds.
The timestamp is local capture time; YouTube's upstream delay is not measured.

An optional local transport check uses an original synthetic text fixture:

```sh
services/tanja/live_transcription/.venv/bin/python services/tanja/tests/run_transcript_entry_bridge.py --railway-config --send-test
```

This reads the existing dedicated Tanja test webhook and contract without printing
or saving credentials. It makes no Railway changes and uses no transcription API.
The fixed fixture is clearly identified as synthetic, not real Tanja speech.
An existing delivery ID returns its saved outcome without another POST; unknown
outcomes are never retried. The local store limits attempts to five per rolling
day, at least 60 seconds apart. The old fixed connection test is unchanged.

TradersPost documents that `test: true` does not send orders to a broker:
[official webhook reference](https://docs.traderspost.io/docs/developer-resources/webhook-reference).
Receiving a webhook does not verify account routing, actual broker fills, bracket
support or signal overrides. Real entry recognition and your account sizing still
require validation before any live execution could be added.

## Observed validation, 12 October 2026

- 36 focused checks passed: 11 bridge, 8 existing connection, 9 interpreter and
  8 ingestion/storage checks. All automated transports were mocked.
- The actual historical replay produced nine observations: its incomplete limit
  entry was blocked; the other eight events were observations only. No payload
  was sent from historical text.
- One real POST of the synthetic fixture was accepted by the configured dedicated
  Tanja test webhook for MNQZ2026. Payload: buy one, limit 20000, stop 19990,
  target 20020, `test: true`. These numbers are test data.
- Receipt ID: `cb0b183a-f7b2-4393-a36d-0a29d8d2c1b9`; `success: true`.
- Ten Shadow observations store the historical decisions, synthetic preview and
  sanitized test receipt. Broker orders sent: zero. Additional paid API calls,
  Railway mutations and deployments: zero.

Evidence lives under `live_transcription/output/entry_bridge_demo/`:
`synthetic_test_payload.json`, `result.json`, `shadow_observations.jsonl`, and
`journal/tanja.sqlite3`. The separate `entry_bridge_preview/` folder records the
reviewable local preview created before the network test.
