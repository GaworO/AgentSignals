# Directional LIVE V1 quote boundary — 23 October 2026

This release changes the existing quote adapter and final validation only. It does not arm execution, create orders, request AI, change the scanner or modify SL/TP. A/B remains shadow. It does not attest the provider's collective/drafted feeds.

## Implemented controls

- Both consensus sources are denied in the adapter and again in the final gate, even if normalized eligibility flags are incorrectly true. Never round off-tick consensus prices to manufacture an executable quote.
- The supported alternative is the existing CrossTrade NT8 adapter, using the same `/v1/api/market/quote` endpoint. Account-scoped quotes are not an independent provider; the tested account route also returned drafted. `/market/info` and `/accounts` returned client disconnected. No connected authoritative alternative was found. A new vendor subscription cannot be fabricated from a TradingView candle entitlement.
- An NT8-shaped response alone is insufficient. `quote_verification` must identify source, exact contract, provider reference, venue BBO, `bid_ask_event_time`, `verified_at_ms` and `expires_at_ms`. The current quote receipt must fall in that interval; final validation checks expiry again. Neither last-trade time nor receipt time substitutes for bid/ask event time. Consensus `ts`/`eventTs` remain explicitly unverified UNIX-seconds observations.
- Source switches are compared to the previous actual response for the same contract, across failed calls/restarts. No source fallback may inherit a prior source's authorization.
- Exact Directional order identity and reviewed geometry, dated MNQ contract and last-trade cutoff, current bid/ask, spread, quote age and HTTP latency, original LIMIT, expiry, fresh structural revalidation and account risk must pass after AI. LONG uses ask; SHORT uses bid. Native transport independently requires LIMIT. The latest native gate still runs after account refresh and before POST.
- Original limit cannot be worsened. A resting nonmarketable limit is blocked by this existing conservative entry rule; this release does not change that rule to chase a fill. A favorable observation must also be within the approved entry zone. SL/TP never move to force acceptance.
- Gross minimum is 2R at the worst allowed original limit and at the observed executable side. Net R is separately reported as (gross reward minus RT fees)/(gross risk plus RT fees plus stop slippage stress). Stress is not a guaranteed stop fill bound.
- Persisted price diagnostics explicitly distinguish observed side price from verified executable price. Contract expiry and maximum API latency are validated finite policy fields. No fresh candle close is used as a quote.

## Configuration and units

All numerical trading limits reside in the existing `DIRECTIONAL_EXECUTION_POLICY_FILE`; no default policy is installed. `approved=true` and a genuine validation reference remain mandatory. Missing/unapproved policy blocks entries. `DIRECTIONAL_QUOTE_SOURCE=crosstrade_nt8` alone does not authorize a feed.

| Field | Unit and validation | Current proposal/evidence |
|---|---|---|
| max_quote_age_ms | positive finite milliseconds since oldest required provider event | 1000 provisional engineering ceiling; NOT statistically validated |
| max_quote_latency_ms | positive finite measured HTTP milliseconds; omitted inherits max_quote_age_ms | 1000 provisional; final gate requires actual latency |
| max_spread_ticks | positive finite ticks; MNQ tick=0.25 points | 2 provisional; historical session medians near 2, recent NYAM sample median 3 |
| entry_zone_points | positive finite index points, absolute distance from original limit | 0.25 provisional; no adequate actual AI/quote timing pairs, NOT calibrated |
| adverse LIMIT deviation | hard zero, cannot be loosened by max_slippage_ticks | 0, original order protection |
| max_signal_age_ms | positive finite milliseconds; additionally original setup and AI expiry | existing setup cap 120000, not a new research result |
| max_slippage_ticks | positive finite stress/check ceiling; cannot override original LIMIT | unresolved; no fill-based calibration |
| max_volatility_points | positive finite points using closed_m1_max_true_range and 1..1440 window bars | unresolved, no invented threshold |
| contract_last_trade_ms | positive integer UTC epoch milliseconds, exact contract | must be supplied with verified contract_reference; no generic third-Friday inference near holidays |
| quote_verification.*_at_ms | positive integer UTC epoch milliseconds | no provider attestation yet |
| costs.round_trip_usd | finite >=0 USD/contract with contract and validation_reference | historical 1.90/contract is evidence, not guaranteed future costs |
| costs.stop_slippage_ticks | finite >=0 ticks, documented stress | unknown; no fake zero |
| min_rr | finite >=2, GROSS | 2 |
| max_account_age_ms | <=30000 milliseconds | existing account freshness protection retained |
| max_thesis_age_ms | positive finite milliseconds | requires approved context policy |

The 1000ms/2-tick/0.25-point suggestions are an unapproved candidate, not fitted to make the current consensus feed pass. All 24 fresh NYAM observations fail the combined age/spread candidate or are invalid. Increasing age to accommodate 3–5-second consensus observations would not establish CME provenance. Do not activate this proposal.

## Evidence collection

`tools/quote_observation_report.py` consumes exact recorded QuoteClient rows and reports per NY date/session, spread, reported ts/event age, ts-event gap, request latency, off-tick values, failures and source switches. Sessions: weekdays NYAM [09:30,11:00), NYPM [13:30,16:00); Guard's earlier entry cutoff is unchanged. Exact request/response records are matched only to preceding quotes within the explicit alignment bound; missing pairs remain UNKNOWN. No interpolation or assumed AI latency is evidence.

Source clarification was submitted through the authorized CrossTrade contact form. Confirmation: “Got it — thanks. We'll be in touch shortly.” Ask for actual source modes, executable BBO status, component timestamp semantics/caching and the supported connected alternative. Human reply pending. Private credentials are absent from the message.

## Validation and deploy

Offline tests inject provider responses and forbid external sockets. They test LONG/SHORT, stale/future quote, excessive spread, wrong/expired contract, adverse original-limit price, expiry, structure change, sub-2R, collective/drafted switching, timeout/missing quote, expired source proof, mismatched review IDs and invalid/nonfinite parameters. Passing mocks do not certify broker data.

Deploy to the existing Agent50k only after confirming saved/effective permissions unchanged and SQLite backups valid. Only release SHA metadata changes. No approved execution policy, new source permission or trading activation is installed by this release.
