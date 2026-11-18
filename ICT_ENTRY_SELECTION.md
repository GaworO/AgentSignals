# ICT entry-selection guidance v10

Scope: the existing independent ICT2022/IFVG AI prompt in
`ict_shadow_research.py`. This is an implementation of the user's interpretation
of ICT teaching, not a claim to reproduce every discretionary ICT trade.
No execution permissions, price/risk rules, schema, call budgets, session schedules,
Directional/A/B rules or pending-order lifecycle rules change in this release.
V10 retains V9's structural-stop assessment validation for both saved V9 and new
V10 packets. It adds Fibonacci context to the existing prompt only.

## Fibonacci measurements

- Range 50% is equilibrium; premium/discount is relative to explicitly named
  anchors and timeframe, not a self-sufficient entry signal.
- Impulse OTE is a 62%-79% retracement, with 70.5% an internal reference.
  Bullish price = H-r*(H-L); bearish price = L+r*(H-L). Name anchor IDs, times,
  prices and wick/body convention before using these measurements. Episode 22
  distinguishes range highs/lows from an optional body-based OTE measurement;
  this never replaces a structural wick SL with a candle-body stop.
- FVG/IFVG 50% is consequent encroachment, independent of the impulse's 50%
  and 62%. Compute separately before claiming overlap.
- OTE is optional confluence for the existing ICT2022/IFVG families. A thesis
  explicitly relying on OTE must substantiate it. A separately confirmed FVG
  thesis is not rejected solely because 62% was not reached or OTE is unknown.
- Already observed shallow retracements may support the near gap half, while
  deeper entries need their own structural justification. No hindsight labels,
  default near-edge preference or new quarter-gap candidate is introduced.
- Explanations use existing premium_discount, fvg_selection and entry_reason
  fields; no new mandatory field, new strategy or additional AI call is added.

Research basis: [Equilibrium vs Discount](https://www.youtube.com/watch?v=qC0LogyIk2I),
[Equilibrium vs Premium](https://www.youtube.com/watch?v=YuefjnUKQdM),
[2022 Episode 4](https://www.youtube.com/watch?v=L-ReMHiavPM), and
[2022 Episode 22](https://www.youtube.com/watch?v=QKc_i90chVg).
For gap CE and shallow retracements see
[2024 Lecture 03, 33:26-34:16](https://info.quagmyre.com/xwiki/bin/view/Forex/The-Inner-Circle-Trader/ICT-Youtube-Series-2024/ICT-YT-2024-08-07-ICT-2024-Mentorship-Lecture-03/).
These are conditional interpretations of the recorded teaching, not an empirical
claim that a Fibonacci ratio improves profitability or fill rate.

## Decision order

1. Establish the family-specific thesis, liquidity draw and confirmation from
   evidence available at the decision cutoff. ICT2022 needs the relevant raid,
   displacement/structure shift and its FVG. IFVG retains the strict closed
   same-timeframe inversion and supported reverse-side role.
2. Select the gap for its structural relationship to the thesis. Hold direction,
   gap/timeframe, SL anchor/buffer and liquidity boundary fixed while comparing
   LOW/MIDPOINT/HIGH. A different gap is a different candidate requiring its own
   explanation, not an invisible entry-price adjustment.
3. Compare all three levels without a universal preference. Assess structural
   suitability and fixed-2R room before considering likely retracement depth.
4. Among comparably supported entries, use attainable retracement before expiry
   as a secondary criterion. Do not sacrifice a stronger thesis for more fills.
5. An already confirmed setup may use a resting LIMIT for its first return.
   A future fill is not missing setup confirmation. WAIT identifies a concrete
   missing condition/data item; it does not require certainty of a fill.

## Conditional entry reasoning

| Situation, based only on observed evidence | Permitted reasoning | Not justified |
| --- | --- | --- |
| Confirmed directional move with shallow observed pullbacks | Consider the first encountered edge: SHORT from below meets LOW; LONG from above meets HIGH, if structure and 2R fit. | Always choose the closest edge, assume a fill, or put the LIMIT outside the gap. |
| Structure supports a deeper retracement or evidenced CE interaction | Consider MIDPOINT or the farther edge, with the same structural SL and boundary. | Choose 50% merely because it is 50%, or because it produces nicer RR. |
| Repeated tests of an ordinary gap | Assess mitigation, intervening events and invalidation. | Automatic expiry after one touch, or automatic continued validity. |
| Contradictory/overlapping price action or material opposition | WAIT with the specific failed condition even if numerical 2R fits. | Ignore opposition to obtain more trades. |
| IFVG price leaves without a retest | Accept a miss; evaluate another independently confirmed idea if supplied. | Anticipate an unclosed inversion, chase the price or treat confidence as permission. |

Gap role can be described as ordinary, continuation or a supported candidate
breakaway/measuring behavior. UNKNOWN subtype is not an additional veto on an
otherwise evidenced ordinary model. Future follow-through cannot establish the
role retrospectively at an earlier entry. An unfilled price gap and an unfilled
broker order are different observations.

Existing output fields carry the explanation: thesis in `htf_narrative`,
liquidity in `liquidity_and_sweep`/`opposing_liquidity`, formation and role in
`fvg_selection`, the three-level comparison in `entry_reason`, and concrete
missing confirmation in `watch_condition`. No new output fields or hard gates
are introduced. Changes from earlier ideas belong in `prior_idea_assessment`;
existing order geometry is never rewritten by this prompt.

## Source basis and limits

These references support the interpretation; they are not supplied as historical
market facts to the model and do not add future candles to test packets.

- [ICT 2022 Episode 6, 17:14](https://www.youtube.com/watch?v=Bkt8B3kLATQ&t=1034s):
  first-edge short example; [23:31](https://www.youtube.com/watch?v=Bkt8B3kLATQ&t=1411s):
  locate the FVG in the actual displacement leg.
- [Episode 20, 13:46](https://www.youtube.com/watch?v=Q6GFu8-Z4rY&t=826s):
  displacement before a resting entry; wait for return rather than chase.
- [Ends Series 4, 34:00](https://www.youtube.com/watch?v=SO3SJFNRim8&t=2040s):
  explicit midpoint use in a particular ES risk model. Its fixed ES stop is NOT
  imported into MNQ structural-stop sizing.
- [Advanced Gap Theory, 39:20](https://www.youtube.com/watch?v=sfRO5LrTgTA&t=2360s):
  distinguish repeated ordinary-gap tests and CE behavior;
  [06:29](https://www.youtube.com/watch?v=sfRO5LrTgTA&t=389s): difficult paths to liquidity.
- [2024 Lecture 28, 28:36](https://www.youtube.com/watch?v=HhgWGduQZQY&t=1716s):
  fill-conscious edge entry in a staged-entry example. Pyramiding and outside-gap
  offsets are NOT imported into our single-LIMIT policy.
- [2024 Lecture 29, 1:15:12](https://www.youtube.com/watch?v=fhBnnU44A6E&t=4512s):
  accept a move without an eligible entry; no mandatory daily trade count.
- [2024 Lecture 43, 39:00](https://www.youtube.com/watch?v=xUWonBnfYOk&t=2340s):
  IFVG context and discretionary early entries whose timing is not fully
  specified. Our closed-inversion requirement remains unchanged.

Fixed gross 2R, broker-safe lifecycle and the configured validity interval are
application policies, not universal rules asserted for every ICT model. The
separate authorized pending-order review can only act under its existing limits.

## Verification boundaries

Offline tests capture the assembled request through the actual cloud worker,
including London/NYAM/NYPM and pending-order review, and verify its durable prompt
version, hash and exact payload. Existing deterministic tests cover geometry,
inversion, expiry, fixed 2R, account sizing and order lifecycle. API and broker
responses in these tests are mocked.

This proves integration and preserved deterministic controls. It does not prove
that an LLM will choose a particular level, reduce WAIT/no-fill, or improve PF.
Paid replay and deployment evidence are recorded separately from these tests.
