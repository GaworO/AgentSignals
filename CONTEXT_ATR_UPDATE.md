# Saved context and daily ATR — October 14, 2026

This implements requested items 1 and 2. It does not deploy or enable broker
execution. The user's confirmed ATR choice is **five completed full futures
sessions**, with the New York session running from 18:00 to 17:00.

## New inversions under saved context

`context_watch.py` checks each new MNQ inversion at the latest closed cutoff.
A valid saved review supplies direction, setup family, POI/sequence rules,
invalidation, objectives, and explicit stop/entry rules. Fresh candles provide
the manipulation/structure break, M15 displacement, zone retracement, inversion
close, and optional paired SMT measurements. Every observed candidate receives
an explanation, including opposite-direction, wrong-timeframe and incomplete
sequence candidates. No additional AI request is made for these checks.

The structured AI response now includes nullable `entry_watch`. It can be armed
while waiting for a future trigger, but only with explicitly accepted directional
context and intact structure. A wait caused by uncertain context cannot arm it.
A newer unarmed, failed or abstaining review supersedes earlier approval.
Expiry, objective touch, invalidation or missing monitoring bars block the watch.
The maximum context lifetime remains 15 minutes; this change does not extend
morning reviews into all-day permission or change the existing six-call budget.

Older complete `entry_setup` reviews can narrowly reuse their existing family,
fixed levels, stop anchor and target for new inversions while still valid. They
cannot manufacture new discretionary POIs or buffers. Reviews containing only a
direction remain insufficient for a complete entry plan. New structured reviews
can select explicit future rules, including the first qualifying M15 gap after
an HTF objective, rather than needing a future evidence ID.

Mechanical acceptance records `origin=saved_context_rule`, the source AI run,
original packet, response availability, expiry and policy fingerprint. It is not
stored as another AI response or a new model quality assessment. The normal
selection observer sizes and applies current account/news guards; the paper
lifecycle recompiles the fresh plan before accepting it. Selected limits wait
within the remaining context lifetime and retain cancellation behavior.

## Daily ATR

`daily_atr.py` calculates each completed session's true range:

`TR = max(high - low, abs(high - previous close), abs(low - previous close))`

ATR is the arithmetic average of the last five completed TR values. Today's
range is the highest high minus lowest low from its session open through the
latest closed minute. Today's partial TR does not enter the baseline.
This is explicitly a five-session **SMA**, not an assumed Wilder smoothing rule.

The full-session boundaries follow the
[CME Micro E-mini hours](https://www.cmegroup.com/articles/faqs/micro-e-mini-equity-index-futures-frequently-asked-questions.html).
The published 16:15–16:30 New York halt is excluded from required minutes; the
17:00–18:00 maintenance interval and weekend closure are also excluded. UTC
timestamps and New York calendar construction handle DST. Valid supplied bars
inside the session still contribute to its measured range.

Each of the six historical sessions needed for five TRs and their preceding close
must contain all required minutes, as must the current session through cutoff.
Late receipts and future bars are excluded. Missing bars are never filled, and
incomplete days are never silently replaced with older convenient days. Until
sufficient history arrives, the dashboard names the missing-history restriction.

Special holiday hours can be supplied through sourced calendar overrides. Their
definition must follow the product schedule rather than the AI spending calendar;
the [CME holiday schedule](https://www.cmegroup.com/trading-hours.html) can change.
Without a supplied override, a shortened/missing session fails the completeness
check. An override example is shown below; the example dates are synthetic.

```json
{"2099-01-02":{"close":"13:00","source":"verified product schedule"},
 "2099-01-03":{"closed":true,"source":"verified product schedule"}}
```

Configuration defaults:

```text
TANJA_DAILY_ATR_PERIOD=5
TANJA_DAILY_ATR_SESSION=futures
TANJA_ATR_SESSION_OVERRIDES={}
```

`regular` is an explicit alternative session (09:30–16:00 NY), not the selected
default. The ATR policy is part of the saved selection revision. Worker snapshots,
AI input packets and new-candidate evaluation use the same calculation.

When current range reaches/exceeds ATR, **new continuation entries are blocked**.
Reversal entries retain their normal requirements. Existing positions continue
their selected management; ATR alone does not force a partial or close. Pending
continuation entries require a fresh ATR measurement before their paper fill.

## Remaining work

Deploy/verify the uploaded service, move AI analysis from ES/MNQ to ES/NQ, connect
continuous broker orders/fills/account-wide history, and finish/validate the
discretionary management and additions. These changes verify mechanics; they do
not establish strategy performance or exact reproduction of Tanja's choices.
