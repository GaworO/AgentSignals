# Provider reference grounding — 2026-11-20

The first two real NY AM reviews were retained as rejected. The first completed response returned an unknown snapshot identity and only 626 input tokens; its input discrepancy is unresolved. Four separately ledgered protocol echoes confirmed both documented content representations and large synthetic inputs work, so no transport-format cause is asserted.

The next real review sent a measured 288,281-byte JSON body and reported 103,530 input tokens. It cited ES, NQ and MNQ as change-evidence IDs. These are symbols present in the packet, not observation IDs. Strict validation correctly rejected it; no trade or candidate was accepted from the answer.

Version four gives Structured Outputs a packet-specific domain for observation, hypothesis and source-rule references, and exact snapshot identities. Deterministic short wire aliases replace only exact observation identifiers throughout the wire packet. Every observation, numerical value, timeframe, source and caveat remains present; round-trip tests establish losslessness. The original canonical packet and raw provider answer remain durable. Only typed reference fields are decoded before the unchanged canonical and numerical validators. Unknown IDs, mismatched prices, cross-contract proposals, missing confirmation and independent risk gates still fail closed. Reference domains above the supported schema limit fail before spending rather than dropping context.

The external chronological runner must skip already attempted snapshots, retain both failed reviews and their costs, and use the same eight-dollar aggregate ledger. Existing frozen experiment plans remain immutable; changed requests require a new versioned plan. This is advisory shadow analysis without order transport.

Schema limits and shared definitions: https://developers.openai.com/api/docs/guides/structured-outputs
