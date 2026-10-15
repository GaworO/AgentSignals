# Production sequences and market checks — October 14, 2026

Version 3.1 connects the two agreed entry families to the saved-context observer
and continuous paper loop. Real broker execution remains blocked.

## Entry selection

`setup_scanner.SequenceScanner` now owns the exact causal `sequence` function
used by the May development replay. The replay subclasses this shared code;
its account simulator and mechanical target/classification assumptions remain
offline. Production requires genuine AI approval, current account/news guards
and independently priced MNQ execution anchors.

The families remain manipulation → swing break → retracement → strict M1
inversion, and H1/H4 objective touch → directional M15 gap → retracement →
strict M1 inversion. Each supports long/short and confirmation/retracement
entries. No unrelated M5 confirmation is added.

The scanner's disclosed choices are strict two-left/two-right M1 pivots, a
180-minute sequence horizon, a structure-break candle-range retracement zone,
and objectives from the last eight completed H1/four completed H4 candles.
It selects the latest qualifying sequence/retest with no future bars or receipts.
These are the May research conventions, not verified discretionary Tanja choices.

The default `TANJA_ENTRY_SELECTION_MODE=context_rules` lets a validated AI review
explicitly authorize `entry_watch.sequence_selector=two_family_v1`. The watch
chooses one family, direction, stop/entry rules, fixed MNQ targets, thesis
invalidation, SMT requirement and continuation/reversal classification. A watch
does not authorize the other family automatically. Both families are discovered
and reported; the selected family must match the approved watch.

Context uncertainty, unarmed/failed/latest superseding reviews, monitoring gaps,
invalidation, reached objectives and expiry still block entries. The watch lasts
at most 15 minutes. Matching fresh inversions need no additional model call.
One entry attempt consumes its structure event or HTF gap/objective, so another
inversion cannot silently authorize a reentry. Management continues after an
entry is open; ATR exhaustion alone never forces an exit.

`TANJA_ENTRY_SELECTION_MODE=individual_trades` requires an explicitly reviewed
completed entry and disables watch reuse. The model prompt reflects this setting.
Neither choice has an established superior PF/win rate. The prior May mechanical
PF is not the performance of the new AI-filtered loop: production retains AI
targets/classification instead of the replay's nearest-target/hourly-body proxies.

Worker snapshots include a bounded sequence catalogue and its required causal
evidence. NQ snapshots retain 64 general candles per timeframe plus selected
sequence evidence; MNQ retains up to 32 priced candles per timeframe plus
matching sequence candles. Raw stored history remains available for ATR. The
500,000-byte model-request cap, six-call daily cap and review hours are unchanged.

## Three-feed readiness

The authenticated dashboard reports current ES/NQ/MNQ minute alignment, gaps in
the recent three-hour sequence history, complete HTF counts, full-session ATR
coverage and execution-contract mapping. It never interpolates missing bars.
The standard 16:15–16:30 NY halt is recognized alongside the daily pause;
special holiday hours still require sourced session overrides.

`TANJA_REQUIRE_MARKET_READINESS=true` is the production-loop default. Entries
need fresh, matching minute feeds and a dated operator attestation that chart
prices are unadjusted and correspond to the intended contracts. Fresh quotes
must match those exact ES/NQ/MNQ contracts. Existing positions continue their
protection even when new entries are blocked.

Private configuration: `TANJA_FEED_CONTRACT_MAP` is a JSON object with ES, NQ
and MNQ keys. Each object contains the exact configured `ticker`, explicit
quarterly `contract` (e.g. MNQZ2026), integer UTC `valid_from`/`valid_until`,
`back_adjusted:false` and a nonempty evidence `source`. NQ/MNQ quarters must
match and MNQ must match `TANJA_EXECUTION_CONTRACT`. Do not populate it with
guesses. It is operator-attested provenance, not exchange verification. Roll
expiry, conflicting tickers, adjusted prices or missing mappings block entries.

## Live verification before this update

Railway was running the older service with no trading-loop modules. ES/MNQ were
current. TradingView's NQ alert was active and firing, but the service rejected
incoming candles with `Symbol/ticker not configured for this service`; its last
NQ candle was October 14 at 02:58 NY. A deployment accepting all three feeds
is needed; the existing alert itself does not need replacement.

The read-only MNQ ATR probe found exactly two missing baseline minutes:
October 6 06:55 and October 12 12:35 NY. Five-session continuity was incomplete.
These are data gaps, not scheduled exchange pauses. No missing price was
invented. Fresh NQ collection also needs to rebuild its recent continuity.

## Validation and deployment boundary

291 service tests passed with one optional ASR skip; 12 shared replay tests
passed. Eight synthetic browser combinations verify NQ analysis labels, MNQ
order levels and visible broker/readiness blockers. No model call or order was
made for this update. Actual deployment and post-deployment feed results are
recorded separately in the release manifest; source tests alone do not prove
fresh feed delivery or a connected broker.
