# ICT IFVG execution integration

Continues the deployed 10086ce release. Original ICT2022 entries, shared account
risk, budgets, and native CrossTrade reconciliation remain in place.

IFVG is a separately authorized family of the same ICT scanner, not another
service or a model-owned broker connection. The same paid response contains both
assessments. Deterministic sizing uses at most 0.5% equity including costs and
all tighter account/broker limits; structural SL and LIMIT remain original;
TP is exactly 2R gross. No partials, trailing, runners or advanced writes.

## Binding and lifecycle

- Only the nested `ifvg_review` of an authentic saved parent response is eligible.
  The original packet hash, raw AI output, validation, geometry and timestamps
  are rechecked. Dashboard companion rows are never executable inputs.
- A new IFVG execution contract must be present in both the original request
  packet and the response. Earlier shadow-only reviews cannot be activated.
- A separate operator-approved `ifvg_execution` scope on the existing ICT policy
  and its `armed_after_ms` are required. Original ICT approval is insufficient.
- Shared global execution permission, fresh broker/risk/Guard checks and native
  bracket proof remain mandatory. Shared account ownership prevents overlap.
- Original ICT2022 is processed first within the same response. IFVG may proceed
  only if shared ownership and all remaining gates permit it; no portfolio claim.
- Before submission and while pending, all original ICT invalidations apply.
  Additionally a closed candle on the formation timeframe below the lower gap
  edge (LONG), or above the upper edge (SHORT), invalidates the IFVG. Missing
  post-inversion candles block; revisions block. Wicks alone do not re-invert.
- Uses the same durable opportunity/dispatch/native broker journals for duplicate
  suppression, unknown POST outcomes, expiry cancellation, late fill protection,
  partial-fill protection, accounting and restart reconciliation.

## Operator activation

The release does not add approval to any existing policy. The manually invoked
`ict_ifvg_operator.py` provides `check`, `activate`, and `stop` commands. Only the
human operator executes `activate` after inspecting its evidence and warnings.
It changes the IFVG policy scope only, never global flags or account limits.
`stop` disables new IFVG entries; existing protection and reconciliation continue.
No Railway variable edit or redeployment is needed for these policy-file actions.
Shared account evidence must still be current and complete; policy approval does
not renew it. Existing historical broker evidence is not proof of a real IFVG
round trip. Automated tests are simulated broker/AI evidence, not live fills.

## Deployment

Keep IFVG unapproved when deploying. Preserve the exact currently active account
risk file if its path is under /app; redeploying an older copy would revert the
operator's evidence. Verify source hashes, runtime commit, all configuration and
active policy/evidence hashes. Do not redeploy a different GitHub image merely
to change a flag. This release adds no subscription and makes no paid test calls.
