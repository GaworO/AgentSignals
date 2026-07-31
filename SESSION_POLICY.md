# Session policy in the A/B Auto-Executor

## Why PREM and ASIA appeared in the report but were rejected

The detector and the Auto-Executor are separate layers.

The detector can find A/B setups in every labeled session:

```text
ASIA, LO, PREM, NYAM, NYL, NYPM, PM_AH
```

The guard then decides whether the setup may place real money. Its current default is:

```text
SKIP_SESSIONS=LO,ASIA,PREM,NYL
```

Therefore:

- a PREM or ASIA setup can appear in detector/shadow data;
- the guard records `blocked` with reason `session:PREM` or `session:ASIA`;
- no broker order is sent;
- the four-year Auto-Executor P&L represents the current selective auto book, not every detector signal.

## PREM session versus PREM catalyst

These are not the same thing.

- **PREM session**: the BOS/confirmation itself occurs during the PREM time window.
- **PREMH/PREML catalyst**: a premarket high or low is used as liquidity, but the actual confirmation may occur later in NYAM or NYPM.

A NYAM trade may therefore contain `PREMH` or `PREML` in its catalyst and still be allowed by the guard.

## Why the defaults are conservative

The repository's existing policy describes NYAM/NYPM/PM_AH as the selected “green” auto sessions. ASIA and LO were kept out of real-money auto and logged in shadow; PREM and NYL were later added to the skip list to stop them auto-firing without the same audited confidence.

The four-year report did not independently prove that every PREM or ASIA setup is bad. It tested the deployed Auto-Executor policy, which excludes them before execution.

Do not remove PREM or ASIA from `SKIP_SESSIONS` merely because individual examples won. Promote a session only after a separate causal run reports:

- fills and no-fills;
- net R after costs;
- monthly P&L and drawdown;
- long/short balance;
- stability by year and regime;
- interaction with one-position and daily-loss guards.

## Current v32 recommendation

Keep:

```text
SKIP_SESSIONS=LO,ASIA,PREM,NYL
```

Continue logging all sessions in shadow (`SHADOW_EXCLUDE=` empty). This collects forward evidence without changing the live book.
