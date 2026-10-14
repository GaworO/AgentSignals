# MARKET and LIMIT language in transcription output

The transcriber preserves recognized text, audio timestamps and UTC timestamps.
It now also writes `order_type`, `order_state` and `price` as `UNKNOWN`, with
`interpretation_status: UNCLASSIFIED_TRANSCRIPT`. These fields intentionally stay
unknown until a separate interpreter evaluates explicit current speech. ASR is
not an order detector, and no execution or position tracking was added.

The vocabulary includes 13 verbatim historical expressions across all nine
requested categories. Every expression has a source path/hash, exact character
offsets, line number and the preceding transcript-block timestamp where available.
A block timestamp is not a precise word timestamp. Some exports contain no
timestamps; those remain `UNKNOWN`. Historical quotes and their prices are
excluded from Whisper's initial prompt; only spelling/terminology terms enter it.

## Source-backed interpretation examples

These examples describe what the language reports. **Every historical example is
non-actionable and provides no evidence of a current position or broker fill.**

| Original words | Reference | Language interpretation |
| --- | --- | --- |
| “I clicked market” | August 19 export, line 92; timestamp UNKNOWN | MARKET / ORDER_PLACED, self-reported submission. Does not by itself prove a fill. |
| “I have my limit in” | July 23, block 00:33:59, line 135 | LIMIT / ORDER_PLACED; still pending, not a confirmed position. |
| “See if I get filled here.” | July 23, block 00:35:29, line 139 | Awaiting fill. Preserve the earlier order state only when order continuity is established. Alone, order type/state are UNKNOWN. |
| “Okay, I just got filled.” | Same July 23 block/line | ORDER_FILLED as a self-reported language claim. LIMIT only if explicitly linked to the preceding limit order; otherwise type UNKNOWN. Not broker verification. |
| “Look like I'm not going to get filled.” | July 28, block 00:29:43, line 115 | Negated/anticipated fill; never ORDER_FILLED. |
| “I essentially took out my limit fill” | July 28, block 00:30:51, line 119 | LIMIT mentioned; wording likely describes removal, but cancellation state remains UNKNOWN because the wording is ambiguous. |
| “You see, I just canceled one.” | August 4, block 01:08:46, line 303 | Cancellation language in a SIM/OCO demonstration. Never apply it to a current live order. Type/price UNKNOWN. |
| “Let me just drag one of them.” | August 4, block 01:09:55, line 311 | Possible order-price modification in the same SIM demonstration; which order and new price are UNKNOWN. |
| “I'm going to put my stop at that high.” | May 19, block 01:25:01, line 451 | ORDER_PLANNED / stop-placement intention; stop price UNKNOWN. Not proof a stop was accepted. |
| “I took some partials.” | August 19, line 102; timestamp UNKNOWN | Self-reported PARTIAL_EXIT; quantity UNKNOWN. |
| “Okay, partial filled there.” | August 19, line 106; timestamp UNKNOWN | Exit fill language, not a new entry fill. Remaining quantity UNKNOWN. |
| “I'm going to close this before it gets lower.” | October 2, block 00:28:37, line 131 | ORDER_PLANNED / exit intention, not a completed close. |
| “I got stopped out there in  profit.” | May 19, block 01:25:21, line 455 | Self-reported stop-out. Remaining quantity/full flattening needs verification. |

The JSON vocabulary preserves original whitespace; this table lightly collapses
whitespace for readability. Full filenames, hashes and exact excerpts are in
`tanja_vocabulary.json → order_expressions`. The stop placement and full-exit
timestamps above refer to transcript blocks, not inferred chart actions.

There is no explicit numerical limit-price change in the selected order excerpts.
The drag example is clearly a SIM demonstration. It is preserved as vocabulary,
with UNKNOWN fields, rather than fabricated as a live price modification.

## Rules for the future interpreter

- `ORDER_PLANNED`: “I'm going to”, “I might”, “I'll place”, or conditional intent.
  This does not mean an order was submitted.
- `ORDER_PLACED`: an explicit current first-person placement/submission statement.
  MARKET and LIMIT submissions both remain unfilled until separately confirmed.
- `ORDER_FILLED`: an explicit current fill statement linked to the same entry
  order. Its evidence is self-reported speech unless independently verified.
  Do not use “gap filled”, “partial filled”, a touched level, a future conditional,
  somebody else's trade, a retrospective example or a SIM demonstration as a
  current entry fill.
- A pending LIMIT order is an order, not a position. An unlinked “filled” statement
  has order type UNKNOWN. Cancellation of an order is not a full position exit.
- A changed limit price is not a new entry/fill. Missing, unclear, abbreviated or
  low-confidence prices remain UNKNOWN. Never expand “700s”, fix a digit or infer
  a price from a previous transcript or known market level.
- Preserve source text/timestamps and process only evidence available at that
  timestamp. No future statement can retroactively confirm an earlier decision.

Illustrative wording below is synthetic, **not a Tanja quotation**:

| Synthetic current speech | Interpretation |
| --- | --- |
| “I will place a market buy.” | MARKET / ORDER_PLANNED; price UNKNOWN |
| “I placed a market buy order.” | MARKET / ORDER_PLACED; position unconfirmed |
| “I placed a buy limit at 123.25.” | LIMIT / ORDER_PLACED; price 123.25 only if clearly recognized; position unconfirmed |
| “My buy limit is still pending.” | LIMIT remains pending; never ORDER_FILLED |
| “Price touched 123.25.” | Observation only; order type/state UNKNOWN |
| “I changed my pending buy limit to 124.50.” | LIMIT price-change report; still no fill |
| “My buy limit just filled.” | LIMIT / ORDER_FILLED, current self-reported fill |
| “I cancelled my buy limit.” | LIMIT / ORDER_CANCELLED; no position close inferred |

These examples are documentation, not a rule engine or a trading interface.
