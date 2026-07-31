# Changelog — MNQ detector

Baseline = v10 (`det_v10.py`, time-based invalidation). Forward tests: MNQ 1m, 2022-06→2026-06,
CONFIRM mode, BE@1R / TP=2R, intrabar SL-first, costs in R vs each trade's own stop.

---

## v32.1 - TradersPost relay semantics and pending-order expiry (2026-07-31)

This patch fixes a live diagnostic defect discovered after v32 deployment. TradersPost's webhook
response `id` is a Signal ID, not a broker order ID, and TradersPost does not expose a broker-state
feedback loop to strategy code.

- Added documented `cancelAfter` to every pending ExecutionPlan leg; the default 10-minute plan
  window now reaches TradersPost itself instead of existing only in local shadow state.
- Added documented `extras` metadata with `plan_id`, `signal_key`, leg identity and absolute plan times.
- Stopped mapping TradersPost's generic response `id` to `broker_order_id`; it is stored as
  `relay_signal_id`, with `logId` stored as `relay_log_id`.
- Added `BROKER_FEEDBACK_MODE=traderspost`, which reports relay-only visibility honestly and no longer
  raises the false “broker ACK missing” critical alarm. `direct` mode retains strict callback checks.
- Added a deployable `relay_service/` with `/stage`, `/execute`, `/health`, direct provider callback
  forwarding and operator-only `/reconcile-expired`.
- Added `tools/expire_guard_order.py` for safe manual reconciliation after verifying the broker has no
  working order or open position.
- Added four tests; package total is 22 tests.

No detector, entry, SL or TP rule changed.

---

## v32.0 - canonical ExecutionPlan, broker feedback and one strategy clock (2026-07-30)

This release fixes execution-parity defects found in the four-year live-like replay. It does not
change the A/B detector edge; it makes the broker order, guard, shadow and management layers describe
the same trade.

### Execution parity

- Added `execution_plan.py`: one immutable, validated source of truth for ticker, side, final
  tick-aligned entry, stop-loss, take-profit legs, quantity, TIF, `entry_ms`, expiry and fill policy.
- Added `execution_engine.py`: one causal resolver for first legal fill, trade-through, adverse-first,
  fill-bar target handling and absolute expiry.
- `agent.py` now creates the plan once, generates every broker leg from it and rejects a partial
  multi-leg send as an invalid execution.
- `shadow.py` and `manage.py` consume the same plan. The old broker-target versus shadow-2R mismatch
  and the hidden extra-bar delay are removed.
- Guard accounting uses the plan's real target/R instead of assuming every win is +2R.

### Broker truth

- Added `broker_feedback.py` and authenticated `POST /guard/broker-event`.
- Normalized broker states: `submitted`, `accepted`, `working`, `partial`, `filled`, `closed`,
  `canceled`, `rejected`, `expired`.
- Broker callbacks can update effective activation time, actual fill price/quantity, realized P&L,
  open-position state, daily limits, profit lock and drawdown state.
- Relay HTTP 2xx is treated only as relay acceptance. Broker callbacks remain the authoritative state.

### Timebase and version

- Added `timebase.py`: UTC epoch milliseconds are canonical storage; all strategy/session labels use
  one fixed `UTC-04:00` clock. `cme_calendar.py` remains in `America/Chicago` for exchange rules.
- `/status` and `/health` report `v32.0-execplan-broker-sync`.
- Default auto session policy remains `SKIP_SESSIONS=LO,ASIA,PREM,NYL`; these sessions are still
  detected and may be logged in shadow, but the main Auto-Executor does not send them.

### Verification

- Added unit coverage for plan validation/serialization, causal execution rules, broker callbacks and
  fixed-clock session boundaries.
- Package verification: `python -m compileall -q .` and `python -m unittest discover -s tests -v`.

---

## v31.4 — late_day starts at 16:00 ET  (2026-07-30, operator instruction)

`GUARD_ENTRY_MARGIN_MIN` default 35 → **4**: the entry cutoff is the flatten deadline (16:04 ET,
earlier on early-close days) minus 4 minutes — i.e. **16:00** on a normal day. The block still
ends at the 18:00 ET reopen. `GUARD_ENTRY_MARGIN_MIN=35` restores the old 15:29 cutoff;
`GUARD_LAST_ENTRY_ET` still overrides with an explicit clock time.

Measured (4y, v31.3 rules, full guard): the reopened 15:29–16:00 band adds **27 trades,
15W/12L, +$8,548 modelled** — but read the caveat before banking it: median resolution of those
trades is **62 minutes**, and only 11 of 27 finish within 24 minutes. Live, everything still open
at the 16:10 force-flatten exits at market; the model lets them run for hours. The true value of
the band is therefore materially less certain than the +$8,548 suggests, and entries near 15:59
hand the exit decision to the liquidation engine, not the bracket.

---

## v31.3 — operator pick: zone lifetime = 120 bars  (2026-07-29)

From the v31.2 sweep the operator selected the **120-bar (2h)** lifetime: +$4,125, 6W/2L,
0 months with a longer losing streak, 2 months slightly worse on $. Shipped as the default —
`ORPHAN_WINDOW=caps` (sequence caps kept), `ORPHAN_LIFE=day`, `ORPHAN_MAX_BARS=120` — which is
byte-for-byte the configuration the sweep measured. **No environment variables are required**;
the defaults ARE the chosen variant. `ORPHAN_MAX_BARS=0` widens back to v31.0 (day end),
`ORPHAN_LIFE=session` clips at session end instead.

---

## v31.2 — the cap sweep: sequence caps stay, zone lives to END OF SESSION  (2026-07-29)

Six lifetime/cap variants, 4 years, full guard, 0.5% risk, all vs the v30.1 baseline:

| variant | orphan trades taken | W/L | orphan $ | months worse | monthly runs worse |
|---|---|---|---|---|---|
| seq caps + till day end (v31.0) | 10 | 6/4 | +$3,095 | 3 | 2 |
| **seq caps + till SESSION end** | **5** | **4/1** | **+$2,685** | **1** | **0** |
| seq caps + max 120 bars | 8 | 6/2 | +$4,125 | 2 | 0 |
| seq caps + max 240 bars | 10 | 6/4 | +$3,095 | 3 | 2 |
| no seq caps + session end | 61 | 16/45 | −$10,948 | 24 | 20 |
| no seq caps + 120 bars | 73 | 22/51 | −$8,923 | 24 | 19 |

**The finding: the poison in v31.1 was never the lifetime — it was removing the retwin/boswin
caps inside the re-armed sequence.** Both uncapped-sequence variants lose ~$9–11k regardless of
how short the zone lives. With the caps kept, every lifetime is positive.

**New defaults: `ORPHAN_WINDOW=caps` + `ORPHAN_LIFE=session`** — the zone dies with the session
it was born in. Cleanest profile of the sweep: 4W/1L, only one month worse than baseline, zero
months with a longer losing streak. `ORPHAN_MAX_BARS=120` is the higher-$ alternative
(+$4,125, 6W/2L) — at n≈5–10 per variant the dollar differences are 1–2 trades and not
statistically separable; session-end is chosen for the risk profile, not the dollars.
`ORPHAN_LIFE=day` restores v31.0; `ORPHAN_WINDOW=day` restores the (negative) v31.1 behaviour.

---

## v31.1 — the bar window runs to END OF DAY  (2026-07-29, operator adjustment)

> **Measured result of this adjustment (4y, full guard, 0.5% risk): NEGATIVE.** The day-wide
> window produces 477 orphan candidates, 75 guarded trades, **22W/53L, −$9,952**; 26 months worse
> vs 13 better; profitable months 44 → 41; several monthly losing streaks lengthen (2 → 3/5).
> The v31.0 capped variant on the same data: 10 trades, 6W/4L, **+$3,095**, no month damaged.
> Both are shipped: `ORPHAN_WINDOW=day` (default — the operator's spec) | `ORPHAN_WINDOW=caps`
> (the measured-safer variant). Recommendation on the data: run `caps`.

v31.0 re-armed only zones with ZERO tests in the 20-bar window, and re-used the 20/30-bar
retest/BOS caps inside the re-armed sequence. The operator widened the rule: **the bar window is
the trading day**. Changes:

- `find_setup_orphan` is now a single causal pass from the bar after the FVG forms to the last
  bar of the day: any CE-holding wick is a test, the origin is the deepest test so far, BOS is a
  close beyond the running structure level, checked continuously — **no retwin/boswin caps**.
- Registration widened: any setup the v10 windows failed to complete (untested OR tested without
  a BOS) stays watched, provided no candle body closed through CE inside the v10 window.
- The two kill rules are unchanged and exclusive: **body close through CE** and **end of day**.
- The immediate v10 path is untouched — fast setups emit exactly as before; +ORPH only ever adds
  what the short windows dropped. `ORPHAN_FVG=0` still restores v30.1 bit-for-bit.

## v31.0 — the orphaned-FVG re-arm  (2026-07-29)

Operator-specified: *"does the strategy still have in mind the left FVGs of the displacement in
case the market comes back there and then holds 50% of CE and starts the sequence of trades from
that step?"* It did not — `find_rejection_v10` watches an FVG for `retwin = 20` bars and then
forgets it forever. v31 adds the memory.

### The rule

A displacement FVG whose retest window expires with **zero wick-tests** (price simply ran away)
becomes a **watched zone**. The zone dies on exactly two conditions — both specified by the
operator, nothing else invented:

1. **a candle BODY closes through its CE** (50% line) — at any point, including inside the
   original window (a CE body-break in the window means the zone was never orphaned, it was killed);
2. **end of the trading day** — zones do not carry overnight.

While the zone lives, a return that wicks into the FVG with the **body still holding CE** restarts
the normal sequence from the retest step: rejection origin (same `retwin` collection), BOS below /
above the retest structure (same `boswin`), entry / SL (v29 anchor) / TP (v30 swing rule) —
all downstream code unchanged. Failed BOS windows do not kill the zone; only the two rules above do.
Emitted records carry `cat = <catalyst>+ORPH`, so the book, `/all/trades` and the Auto-Executor
column show exactly where the trade came from. One physical zone re-arms at most once per catalyst
set (deduped on the FVG itself).

### Files

`detcore/confirmation.py` (`rejection_untested`, `find_setup_orphan`) · `detcore/emit.py`
(orphan registration inside `emit()`, `emit_orphan()`) · `detcore/context.py` (`ctx.orphans`) ·
`detcore/pipeline.py` (orphan resolution pass after `run_all`) · `agent.py` (VERSION) ·
`dashboard.py` (`ORPHAN_FVG` row).

### Verified

- `ORPHAN_FVG=0` → **bit-identical** signal list to v30.1 (checked record-for-record).
- Every emitted `+ORPH` trade re-checked independently: **no body close through CE** anywhere from
  FVG birth to the BOS bar (0 violations), and the whole sequence completes inside the zone's day.
- The operator's own counter-example (2026-06-10 zone whose CE was body-broken before the late
  return) is correctly **not** emitted.
- Jun–Jul archive: 255 → 258 signals (**3** orphan re-arms). Four years: 7,533 → 7,672 signals
  (**139** orphan candidates, **10** taken by the full guard: 6W/4L, **+$3,095** at 0.5% risk).
  Month-by-month: 6 months better / 3 worse, profitable-month count unchanged (44/49).

---

## v30.1 — 1R partial OFF by default  (2026-07-29)

Measured on the last 12 months (same swing TP, same trades): partial ON $+26,197 vs OFF
$+30,548 — the partial cost **$4,351 (~14%)**, was the better month only 3/12, identical
profitable-month count and losing streaks, and softened the in-month DD in 5/12 months.
On the Jun–Jul archive replay: ON +1.19R vs OFF +2.31R with exactly one save (−1R → −0.11R).
That is a high insurance premium for the protection delivered, so the default flips to OFF.
`PARTIAL_AT_1R=1` re-enables the two-leg bracket unchanged (env only, no redeploy).

---

## v30.0 — the target: last swing level left, capped 3R; 1R partial  (2026-07-29)

Base = v29.1. Two changes, both about WHEN profit is taken. Entries, the v29 stop anchor,
the guard and the risk cap are untouched.

### 1 · TP = the last swing level to the left  (`detcore/exits.py`)

Since v10 the target was a blind `entry ± 2R`. v30 aims at structure instead:

```
target = the most recent CONFIRMED swing left of the BOS bar
         (swing low below for a SHORT, swing high above for a LONG)
rules  : must be DEEPER than SWING_TP_MIN_R x risk   (default 1R — shallower swings are skipped)
         clamped at SWING_TP_MAX_R x risk            (default 3R)
         no qualifying level in SWING_TP_LOOKBACK    (240 bars) -> fixed 2R, exactly as before
```

- A swing is a `SWING_TP_K`-bar fractal (default 5 each side) and only counts once its right
  side has printed — every bar read is `<= bos_bar`, so live and backtest see the same level.
- Emitted records carry **`tp_src`** (`swing` | `2R`) and **`tp_level`** (the raw level aimed at).
- `SWING_TP=0` restores v29's flat 2R bit for bit. Callers that never passed a BOS bar
  (forex det, strategy_f) are unchanged by construction.
- Measured on Jun–Jul 2026 (246 signals, identical trade set): **114 swing-targeted
  (1.02R…3.00R, median 2.0R), 132 fall back to 2R.** Cap and floor verified per signal.

### 2 · 1R partial — 0.2% of the account banked  (`agent.py` + `manage.py`)

`_exec_order()` now splits one signal into **two broker brackets**:

- **leg A ("banker")** — `PARTIAL_ACCT_PCT / RISK_PCT` of the contracts (0.2/0.5 = **40%**),
  TP at exactly **+1R**;
- **leg B ("runner")** — the rest, TP at the v30 target above.

Same entry limit, same stop, same TIF — they fill and stop together; only the targets differ.
This is entirely broker-side: the partial executes even if the agent is asleep mid-trade.
With 1 contract there is nothing to split — single bracket, partial skipped.
`PARTIAL_AT_1R=0` restores the single v29 bracket.

`manage.py` models the same maths in its tracking/alerts: partial banked at r1
(`+0.4R` in the pocket), runner to the real target; SL after the partial nets `2·frac−1`
(−0.2R at 40%), TP nets `frac + (1−frac)·tp_r`. The book stores `tp`, `tp_r`, `tp_src`.

### 3 · Auto-Executor wiring — tested, one bug found and fixed

The two-leg bracket was exercised against a local capture server with `agent._exec_order()`
itself (not a re-implementation), fed real v30 detector records:

- **BUG (caught by the test, fixed):** leg B was sent to a locally recomputed `entry ± 2R`
  instead of the detector's swing target — `_exec_order` still had the pre-v30 inline TP.
  The runner now uses `x['TP']` (the swing level / 2R fallback); records without a TP fall
  back to the old 2R recompute.
- Verified payloads: two brackets, same entry limit / same stop / same TIF, leg A TP at
  exactly +1R (tick-aligned), leg B TP at the detector's target; `PARTIAL_AT_1R=0` → one
  bracket at the detector's TP; `EXEC_QTY=1` → one bracket (nothing to split).
- **`/guard` (Auto-Executor page)** — new **TP type** column next to TP: SWING (green) /
  2R (blue), with "· 2 legs" when the partial split fired; hover shows the exact brackets
  (`3@30894.5 + 5@30885.25`). Book rows store `tp_src` and `legs`; the Telegram fire alert
  now reads `... / TP 30885.25 (swing) [3@30894.5 + 5@30885.25]`. Pre-v30 rows show "—".

### Arithmetic, so nobody has to re-derive it

At RISK_PCT 0.5% and the default 40% partial: reaching +1R banks **+0.2% of the account**;
a runner stopped at the original SL nets **−0.2%** account (was −0.5%); a runner reaching a
2R target nets **+0.8%** (was +1.0%); at a 3R swing target **+1.1%**.

---

## v29.0 — the stop anchor: struct, or the held FVG's far edge  (2026-07-29)

**One change, and it is the stop.** Since v10 the protective stop has been hard-coded to the
**consequent encroachment (midpoint) of the displacement FVG** — two identical lines,
`detcore/entries.py:27` (FVG-edge entry) and `:46` (OTE/fibo entry). v29 replaces both with a
structural anchor. Entries, targets, catalysts, scaffolding, the guard and the risk cap are
untouched.

### The rule

```
SL = struct                       when |entry − struct| <= SL_STRUCT_MAX_R   (default 30 points)
SL = far edge of the held FVG     otherwise
SL = CE                           only if the FVG edge is on the wrong side of entry (degenerate)
```

then `SL_ANCHOR_BUF` (default **0.25**, one MNQ tick) is added **beyond** the chosen level —
lower for a long, higher for a short.

- **`struct`** = the extreme of the whole **displacement leg**: `min(lo[s : origin_bar+1])` for a
  long, `max(hi[s : origin_bar+1])` for a short, where `s` is the displacement start bar carried in
  the setup dict. This is the swing the move came *from*, not the shallow wick that retested the gap.
- **`far edge of the held FVG`** = `fvg[0]` (the FVG low) for a long, `fvg[1]` (the FVG high) for a
  short — the edge price actually rejected from, not its midpoint. This is roughly half the FVG's
  width wider than the old CE stop.
- Both levels are **strictly causal**: every bar read is `<= su['bos_bar']`, the same window the live
  14k-bar buffer holds when the detector fires. Live and backtest see identical inputs.

### Why struct is capped at 30 points

Unbounded, the displacement-leg extreme is far too wide to trade: over the June–July bars its median
risk is **44.7 points against CE's 18.1**, the p90 is triple that, and the widest is **147.9 points**
(over the full four years, 483.6). It is the right level conceptually and the wrong level
practically on most setups. The 30-point gate keeps struct where it is genuinely tight — the cases
where the leg that produced the displacement is close behind the entry — and hands everything else
to the FVG edge, which sits at a workable **median 22.0 points**.

Measured on 224 emitted signals over the June–July window: **struct is used on 38, the FVG far edge
on 186.** No signal falls through to CE.

### Changed

- **`detcore/entries.py`** — new `struct_sl()`, `held_fvg_edge()` and `pick_sl()`. Both
  `find_entry_v10()` and `find_entry_fibo_v10()` now compute `entry` **first** and then pick the
  stop, because the rule is expressed in risk-from-entry and cannot be evaluated before the entry
  price exists. The two hard-coded `sl = round((su['fvg'][0] + su['fvg'][1]) / 2, 2)` lines are gone.
- **`detcore/emit.py`** — the record now carries `sl_src` (`struct` | `fvg_edge` | `ce`) plus
  `sl_ce`, `sl_struct` and `sl_fvg_edge`, so the book, `/all/trades` and any audit can show **why**
  the stop is where it is instead of inferring it.
- **`agent.py`** — `VERSION` → `v29.0`. This is what `/health` and `/guard/data` report, so a
  deployed instance can be checked at a glance.
- **`dashboard.py`** — `SL_STRUCT_MAX_R` and `SL_ANCHOR_BUF` added to the A/B settings panel.

### Unchanged — deliberately

- **The risk cap.** `cfg.max_stop_r` (`MAX_STOP_R`, default 40) and `exits.exceeds_risk_cap()` still
  **discard** a setup whose stop exceeds the cap, exactly as before. The v22 `_cap_stop` re-anchor
  (`STOP_CAP` / `STOP_CAP_TRIGGER`, both 0/off by default) still runs after the anchor is chosen.
- **The target.** `exits.take_profit()` is untouched: still `entry ± rr*risk` off the *actual* stop,
  and `agent._exec_order` still recomputes `tp = entry ± 2R` from the stop it is given.
- Entries, displacement/rejection/BOS, catalysts, sessions, `guardrails.py`, `shadow.py` and the
  fill clock: no edits.

### The cap RE-ANCHORS; it never deletes

A wider anchor would otherwise collide with `MAX_STOP_R`, which discards a setup whose stop exceeds
it. v29 does not accept that trade-off:

- **`pick_sl()`** pulls an over-wide stop back **to** `cfg.max_stop_r` and tags the source
  `+capped`. The setup is kept and the max-SL ceiling still holds exactly (no stop is ever wider
  than the cap).
- **`emit()`** gates on the **CE risk** (`risk_ce`), exactly as v28 did, so the SET of emitted
  signals is unchanged. v29 only moves the stop.

Verified: **7,533 signals in v28 and 7,533 in v29** over 2022-06→2026-06. On the June–July window the
two builds agree **signal for signal** — same date, direction, catalyst, entry and `entry_ms` on all
246 — with the stop moved on every one and a maximum stop of exactly 40.00. Anchor mix over four
years: struct 3,438, held-FVG edge 3,652, FVG edge re-anchored to the cap 443, CE 0.

`detcore/exits.py` is untouched: `exceeds_risk_cap()` behaves as before, it simply never fires
because the stop it is handed already sits inside the cap.

### Second-order effect: the anti-stack rule

The signal set is identical but the *taken* set is not quite — 370 → 388 trades after the guard over
four years (13 months take more, 3 take fewer, 33 unchanged). A wider stop changes when a position
closes, which changes which later signals `position_open` blocks. This is a real consequence of
moving the stop, not a detection change.

### v29.1 — the book shows which anchor fired  (same day)

- **`guardrails.note()`** stores `sl_src` on every book row (sent AND blocked).
- **`/guard` (Auto-Executor page)** — new **SL type** column between SL and TP:
  <span>STRUCT</span> (green) / <span>FVG edge</span> (blue) / <span>FVG edge · capped 40</span> (amber).
  Rows from before v29.1 show "—" (the field did not exist yet).
- **Telegram fire alert** now reads `SL 30952.25 (fvg_edge) / TP ...`.
- `agent.py` `VERSION` → **v29.1**.

### Live path

`det_v11.py` → `detcore/` is the live A/B detector (`agent._detect()` shells out to
`DET_FILE`, default `det_v11.py`). The emitted `SL` is what `guardrails` books and what
`agent._exec_order()` sends to TradersPost as the bracket stop:
`sl = float(x['SL'])`. No separate executor-side stop logic exists, so the Auto-Executor
picks this up with no further change — every new signal from the next detector run onward.

---

## v28.0 — the backtest was wrong; fill clock; cross-strategy audit  (2026-07-25)

> **Read this before trusting any number written in this file above.** Every forward-test figure in
> the v10–v27 entries was produced by a harness that (a) let the entry read a bar that had not closed
> when the detector fires, and (b) opened the fill window on that same bar. Over 2022-06→2026-07 that
> is worth **+0.4895 R/fill modelled vs +0.2606 R/fill real** — the backtest read **$624,270** where
> the machine makes **$245,042**, and **$12,485/month against a real $4,901**. It is not slippage and
> it is not the broker. Treat every historical R-figure above as roughly **2× optimistic** until it is
> re-run on this harness.

### Fixed — backtest/live parity
- **`detcore/entries.py`, `detcore/primitives.py` — `Config.causal` (default ON, env `DET_CAUSAL=0`
  to restore).** `find_entry_v10` scanned `fvgs(ob, bb+2)`, so the FVG defining the entry could have
  its middle bar at `bb+1`; `impulse_end_v10`'s stall walk ran to `bb+cap`. Live the buffer ends at
  the BOS bar, so it never sees either. Consequence, measured: **76% of live entries are the OTE/fibo
  level and only 24% the FVG edge — in a backtest it is 53% FVG.** Half of every historical test was
  priced at a level the machine does not quote.
- **`detcore/emit.py` — emits `entry_ms`.** `entry_bar = max(sfvg_bar|hh_bar, bos_bar)+1` has been
  stored since v11 and nothing ever read it; every consumer opened the fill window at `bos_ms+1 bar`.
  **This single line is 81% of the parity gap** (+0.4895 → +0.2169 R/fill on identical entries).
  Lag distribution over 4y: 1,227 signals at +1 bar, 377 at +2, 325 at +3, 359 at +4/+5; only 395 of
  3,035 were genuinely tradeable at `bos+1`. 159 signals over 4y flip from a modelled +2R to a real −1R.
- **`shadow.py` — fill bar is scored adverse-only** (`SHADOW_FILLBAR_TP=1` restores the old
  behaviour). The outcome loop started AT the fill bar and checked the target against that whole
  bar's range, so a favourable extreme printed BEFORE the limit was hit counted as a win. 2026-07-23
  NYAM long: book **+1.937R**, broker **−$385**. Worth **+0.049 R/fill** of edge that never existed.
- **`agent.py` / `guardrails.note()` — the book now stores the EXECUTED take-profit.** `_exec_order`
  recomputes `tp = entry ± 2R` from the POST-`ENTRY_OFFSET_PTS` entry while the book stored the
  detector's PRE-offset `x['TP']`; at offset 1 they are 3 points apart (2026-07-24 NYPM ×12: Pine
  28347.67, broker 28344.75). Worth **−0.044 R/fill**. The two errors nearly cancelled at offset 1,
  which is why neither surfaced — they stop cancelling the moment the offset changes.

### Changed — the fill clock (the money)
- **`shadow._fill_win()` → `FILL_WIN_MIN`, default 10 (was a hardcoded 240).** Expectancy by
  time-to-fill over 4y: **0–1 min +0.972R · 1–5 min +0.583R · 5–10 min +0.331R · 10–15 min −0.014R ·
  15–30 min −0.096R · 30–60 min −0.068R · 60–240 min −0.089R.** Stop distance (13–15 pt) and contract
  count are flat across every bucket, so it is not a size artefact — the setup goes stale. Result:
  fills 85.3% → 42.3%, R/fill +0.261 → +0.599, 4y net **$242,919 → $269,169**, peak DD
  **$12,128 → $3,367**, **7 losing months out of 50 → 1**, worst month **−$8,375 → −$1,792**.
  Monotone from 5 to 240 minutes and holds in all five yearly slices (2.1–2.7× each) — no fitted peak.
- **`guardrails.sweep_orphans()` → `SWEEP_LAG_MIN`, default 3.** `flatten_cancel_only()` sends a
  BLANKET ticker cancel guarded only by `if d['openpos']: return 0`. On a 10-minute clock that sweep
  runs constantly; the lag holds it to 13 minutes so a fill at 9:59 the book has not resolved yet
  cannot lose its bracket. **Do not set it to 0.**
- **`manage.check(fill_ms=None)`** now defaults to the same `FILL_WIN_MIN` (was a hardcoded 2 h).

### Rejected — partials and break-even (tested, do not ship)
Against the flat 2R baseline (+0.2606 R/fill): BE@1R **+0.2063** · 50% at 1R stop-stays **+0.1952** ·
50% at 1R + BE **+0.1681** (−0.093R, −36% of the edge) · 33% at 1R + BE **+0.1811** · 50% at 1R + BE
runner 3R **+0.1809** · flat 1.5R **+0.2224** · **50% at 1.5R + BE +0.2318 (least bad)** · flat 3R
**+0.3261** but a 23-trade losing run and 32R drawdown = an eval breach, not an edge.
Partials only buy a shorter losing run (6 vs 10); the fill clock buys more drawdown reduction and
pays more. The week of 2026-07-21 had 4 of 6 losers reach ≥1.2R before reversing — the 4-year base
rate for losers reaching 1R is **30.0%**.

### Also verified / left alone
- `ENTRY_OFFSET_PTS=1` is correct and live (reproduces 2026-07-24 entries to the cent); the curve is
  flat from 1 to 3 points.
- **Waiting for the "better" FVG entry tests worse**: +1 bar +0.269R, +2 bars +0.200R, +3 bars
  +0.133R. It fills more (90.5% vs 85.3%) but the extra fills are worth **0.020R each**, and in
  dollars it only wins by risking 69% more capital (37.9% return on risk vs 57.5%).
- `EXEC_MAX_QTY=17` binds on **58% of filled trades**; the tight-stop cohort runs at **$282 of risk
  instead of $500 (56% of target)**. Lifting to 25 takes 4y net $269k → $323k at $3,947 DD — but
  net/DD barely moves (79.9× → 81.9×), so it is leverage, not edge, and peak DD is already at the
  MFF floor. Post-funding lever, not an eval lever.

### KNOWN GAP — do not read the dollar figures as your account
Entry price and fill mechanics now match live. **Which setups fire does not.** Live re-runs the
detector every minute on a rolling 14,000-bar buffer and emits a setup the first time it appears;
a single historical pass sees a different level history. Measured on 2026-07-19→24 by replaying the
rolling buffer every 5 bars (1,367 detector runs): **exact replay 79 setups, reproducing 3 of the 9
that reached the broker; one-pass backtest 16 setups, reproducing 0 of 9.** The live detection
stream is ~5× denser than any one-pass. Rankings hold on both populations (on the 79 replayed
setups: +0.331 R/fill at 240 min → +0.565 R/fill at 10 min); absolute dollars do not.

### CROSS-STRATEGY — the fill clock is NOT wired to C / F / ORB / AMD
Everything above was measured on the **A/B detector (`detcore`) only**. The other services run their
own engines, their own dedup and their own TradersPost strategies (`EXEC_WEBHOOK_C/_F/_ORB/_AMD`),
so `guardrails.sweep_orphans()` never reaches their orders. Audit:
- **Strategy F** (`strategy_f_live.py`) — `STRAT_F_FILL_MIN=30`, **touch fill** (`lo<=e<=hi`, the
  over-optimistic model A/B replaced with through-fill in v27.3), models **BE@1R** while the broker
  holds a static bracket (the phantom-BE bug A/B already paid for), and **`timeInForce: "gtc"`
  hardcoded** with `STRAT_F_AUTO_CANCEL` firing only on a body-break — **an F limit that simply never
  fills is never cancelled at the broker and can fill days later.**
- **Model C** (`model_c_live.py`) — `C_FILL_MIN=30`, touch fill, `C_NOBE=0` (BE@1R modelled), TIF day.
- **ORB** (`orb_live.py`) — `timeInForce: "gtc"` hardcoded; stop/stopLimit entries mostly fill, but
  the `limit` retest variant carries the same stale-limit risk.
- **AMD** (`amd_live.py`) — market entry, immune to all of this.
Priority order: F's GTC orphan (correctness) → F/C touch-fill and phantom BE (measurement) →
F/C 30 → 10 minutes, only after the clock is re-measured on each engine's own population.

---

## v27.0 — auto-execution hardening + MFF-rules layer  (2026-07-19)

**guardrails.py** — broker-boundary safety: loss/DD/manual latches now FLATTEN (exit+cancel), not
just block; EOD flatten default 16:04 ET (MFF liquidates 16:10; holiday early-close aware via
cme_calendar -> note_early_close); late-entry cutoff auto-derived (deadline-35m); fail-closed news
calendar (news_cal_stale when ForexFactory stale >24h); MIN_SL_PTS=5; DD floor auto-trails synced
equity highs (eq_high-3000, cap DD_FLOOR_CAP); atomic state writes + corrupt-state HARD latch;
orphan-limit sweep (cancels broker orders shadow wrote off as no_fill); GUARD_TOKEN auth on
/guard/kill|mode|sync; daily proof-of-life digest 08:45 ET; GUARD_SKIP_DIB gate (opt-in);
SKIP_SESSIONS default now LO,ASIA,PREM,NYL (env overrides).
**agent.py** — EXEC_TIF default day (was gtc); exec result checked before booking sent
(failures alert, don't burn counters); EXEC_MAX_QTY default 15; SELECT_SIZE_MULT / GUARD_DYN_RISK
/ SESSION_SIZE_MULT (def ASIA:0.5,LO:0.5) sizing levers; news hard + calendar age wired into guard.
**shadow.py** — logs ALL sessions (SHADOW_EXCLUDE env restores old behaviour).
**requirements.txt** — pin pandas<3 (timestamp parsing regression breaks shadow resolution).
Validated: 4-yr machine run (2022-06->2026-06, 1.42M bars): guarded book 432 sent / 247 fills,
155W-92L, 0 floor breaches, 13/13 eval passes (median 90 calendar days). Model = fixed bracket
touch-fill (upper bound); tier ordering SELECT>A>B stable across all fill models.

---

## v11 — sweep-based invalidation  (2026-06)

### Changed (the core idea)
- **Liquidity levels die by SWEEP, not by clock.** Session H/L (AH/AL, LH/LL, NYAMH/L, NYLH/L,
  NYPMH/L), PDH/PDL, PWH/PWL and BSL/SSL now stay alive until price sweeps them (or they produce a
  confirmed setup), instead of expiring on a fixed same-day / +1-day / +2-day window.
- **Fixes the founding bug:** a Friday session level untouched Friday is still scannable Monday
  (previously it died Friday night → no Monday candidate).
- **10-trading-day backstop** so a never-swept level can't live forever.

### Added (knobs)
- `MODE` = `confirm` (default) | `sweep`.
  - **confirm** = v10 multi-break (re-arm after a BUF pullback); level dies after its **first
    confirmed setup**.
  - **sweep** = level dies on the **first trade-through** (wick or body); one shot per level.
- `CAP_DAYS` (default `10`) — the backstop in trading days.

### Unchanged (verified identical to v10)
- F.P.FVG (time, end-of-day), NDOG/NWOG + VI (gap zones), the displacement detector, rejection /
  BOS, FVG-edge / OTE entry, SL=CE, TP=2R, bias flag, dedup, `CUTOFF`, and the pickle record schema.

### Architecture
- Monolith split into the `detcore/` package: `config, data, catalysts, scaffolding, confirmation,
  entries, exits, emit, pipeline, context, primitives`. `det_v11.py` is a thin entry point.
- Output is **byte-for-byte identical** to the monolith (kept as `det_v11_monolith.py`,
  verified by `gate_check.py`).

### Forward-test result (CONFIRM, after base cost)
- 4-year pooled: **+0.135R / trade, +351R, ~2,600 trades**, win ~31%.
- Per year: +30 / +211 / **−17 (2024-25)** / +122 R — one mild losing year.
- SWEEP mode: far fewer trades (~340 / 4y), similar per-trade edge, much lower total — not used as default.
- Detail: `v11_invalidation_results.md`.

---

## v12 — end-of-day expiry + regime gate  (2026-06)

### Added
- **EOD expiry for session H/L** (`EOD_INTRADAY`, default **off**): a session level that is *tapped
  without confirming* expires at end of day; evening taps roll to the next day; **untouched levels are
  unaffected** (Friday→Monday case preserved). PDH/PWH/BSL stay multi-day.
- **Regime gate** (`regime_gate.py` + `agent.py` `REGIME_GATE=1`, opt-in): turns EOD **on in choppy
  regimes, off in trends** using `regime.py`. **Telegram notification only when the state flips.**
  Regime rechecked at most once/hour (`REGIME_TTL_SEC`).
- Backtest fill window changed **4h → 2h** (more realistic; barely moves results).

### Notes
- Blanket EOD = −43% total R over 4 years (trims winners) but removes the losing year. **Not the
  default.** Regime-gated (EOD only in chop) best-case ≈ **+401R / 4y**, no losing year.
- `agent.py` `_save_db` made tolerant of the v11 record schema; `DET_FILE` selects detector
  (`det_v10.py` default, `det_v11.py` when `REGIME_GATE=1`).
- Detail: `v12_eod_results.md`.

---

## v13 — displacement V1 + live wiring  (2026-06)

### Changed — displacement V1 is now the DEFAULT (`detcore/confirmation.py`)
- V0 took the shortest **1–3 candle** impulse. **V1 ("chain")** starts a **≥`MINIMP` (3) same-colour
  run and extends through the WHOLE unbroken run**, treating it as one displacement. DIB (class B)
  is unchanged — only the forward class-A displacement uses the chain. Reversible: `DISP_MODE=orig`.
- Knobs (`detcore/config.py`): `DISP_MODE` (`chain` default | `orig`), `MINIMP` (3), `MAXEXT` (40).
- **Forward test on the real detcore** (CONFIRM, 2h fill, base cost): pooled **+0.204R/trade, +725R/4y**
  vs V0 +0.149R / +376R — improves **every** window; the 2024-25 losing year flips **−16.8R → +93R**.
  (V0 baseline reproduced detcore vs harness to confirm the engine. See `displacement_v0_vs_v1_findings.md`.)

### Live integration (so `/candidates` + alerts reflect all the above)
- **`/candidates` now runs the live detector** (`agent.py`) — was hardcoded to det_v10; now det_v11
  (+ EOD flag) whenever `REGIME_GATE=1`, matching live alerts, with the stage trace.
- **`regime.py` classifies via det_v11** (not det_v10), EOD off to avoid circularity, and **no longer
  freezes on a stale pickle** — on detector failure `/regime` returns `ok:false` + the error. Knob `REGIME_DET`.
- **`regime_monitor.html`** shows "odświeżono HH:MM:SS" when live, "⚠ LIVE OFF — /regime: <error>"
  when not (instead of silently showing the static backtest fallback), and fetches no-cache.

### Docs
- `TradingJournal/guide.html`: catalyst freshness text rewritten from "first touch / time" to the
  v11 sweep + v12 EOD model.

### Candidate stage funnel (what `/candidates` shows)
`displacement OK` → `brak setupu (odbicie/BOS)` → `setup OK (BOS)` → `brak wejścia` /
`odcięty cap (R=…)` → **`POTWIERDZONY`** (the confirmed signal that fires an alert).

---

## v14 — displacement scan window (DISPWIN) tuning  (2026-06)

`DISPWIN` = how many bars after a level tap a displacement may **start**. Made an **env knob**
(`detcore/config.py`, default 10). It's a range [1..DISPWIN], so widening is a **superset** — it still
catches the quick 3-candle moves *and* adds later-starting ones. Only `config.py` changed.

### Forward test (chain/V1, CONFIRM, 2h fill, base cost) — net R per 12-mo window
| DISPWIN | 2022-23 | 2023-24 | 2024-25 | 2025-26 | Pooled R | exp/trade |
|---|--:|--:|--:|--:|--:|--:|
| 10 | +84.7 | +313.3 | +93.2 | +234.1 | +725 | +0.204 |
| 20 | +136.6 | +397.2 | +132.7 | +301.7 | +968 | +0.226 |
| 30 | +148.3 | +397.9 | +170.6 | +338.2 | +1055 | +0.222 |
| 50 | +170.2 | +499.7 | +251.0 | +404.3 | +1325 | +0.242 |

### Walk-forward (fit 2022-24, test UNTOUCHED 2024-26) — OOS is the decider
| DISPWIN | IS exp | OOS exp | OOS totR |
|---|--:|--:|--:|
| 10 | +0.207 | +0.201 | +327 |
| 20 | +0.235 | +0.215 | +434 |
| 30 | +0.218 | **+0.225** | +509 |
| 50 | +0.236 | +0.248 | +655 |

- **OOS expectancy rises with the window (10<20<30<50)** — the edge holds out-of-sample, not in-sample overfitting.
- **Decision: `DISPWIN=30`.** OOS-validated, catalyst still meaningful (impulse within ~30 min). 50 scores
  higher OOS but the catalyst is nearly decoupled (generic momentum-FVG) — only after live proof.
- Caveats: "OOS" is still the same 4-year MNQ era (weak walk-forward; the real test is live); +34% trades vs DW=10.

### Deploy
Upload `detcore/config.py` (adds the `DISPWIN` env read) **and** set `DISPWIN=30` on Railway.
**The env var has no effect until the updated `config.py` is deployed.**

---

## v15 — live alert hygiene (dedup · fill-gate · TP3 · re-test filter)  (2026-06)

LIVE-path fixes only (`agent.py`, `manage.py`, `live_emit.py`). Detection / backtest math unchanged.

### Fixed
- **Phantom TP/SL right after entry** (`manage.py`): the tracker measured 1R/2R/SL from BOS
  confirmation — *before* the LIMIT filled. Added a **FILL gate**: no targets until price reaches the
  entry; sends `✅ FILL` on fill; an unfilled limit **auto-cancels after 2h** (`⌛`). Verified: no phantom
  TP when price is already past 2R; FILL→1R→2R correct; unfilled→cancel.
- **Duplicate alerts** (`agent.py`): one displacement tapped by N nearby levels fired **N near-identical
  Telegram messages** and registered **N trades** in `manage` (→ N× the FILL/TP/SL follow-ups). Alerts now
  **collapse by trade identity** (date/model/dir/BOS/entry/SL): catalysts merge into the `Kat:` line plus a
  `🔗 Konfluencja N×` note, and `manage.register` runs once. A later-joining catalyst on the same trade is
  suppressed via a persisted trade-key.

### Added
- **TP3 (3R)** in alerts (`live_emit.py`) next to TP2 (2R). 2R = system target (zweryfikowany); 3R =
  optional runner, labelled *niezweryfikowany* — the 4-yr forward test exits full at 2R.
- **`MAX_RETEST`** (agent, default `0` = off): suppress a trade whose **freshest** catalyst is beyond N
  re-tests (min(brk) across the confluence). A 12th re-test of a level is a weak signal.
- **`VERSION` marker** in `/status` and `/health` (e.g. `v15`) so a deploy is verifiable at a glance.

### Deploy
Upload **`agent.py`**, **`manage.py`**, **`live_emit.py`**. Env (Railway): set **`ACCOUNT`/`RISK_PCT`** to
your real account (sizing — default $100k is dangerous), **`MAX_RETEST`** e.g. `4` for the filter (needs
the new `agent.py`), **`PRICE_OFFSET`** only during a contract roll.

---

## v16 — live tracking + regime gate + alert fallback  (2026-06)

LIVE-path additions (`agent.py`, `manage.py`). Detection unchanged.

### Added
- **Live outcome tracking** — `manage.py` records realized R (SL −1 / BE 0 / TP +2, plus `timeout`) per
  closed trade to `outcomes.json` (on the Volume). New **`/performance`** endpoint: live expectancy R,
  win%, total R (all / last 20 / last 50) shown next to the backtest reference (+0.29R trend / +0.10R chop).
- **`/lastalert?n=N`** — full alert text + entry/SL/TP straight from `journal.db`; reliable fallback when
  the Telegram relay truncates a message (the agent always builds the complete alert).
- **Regime size gate (opt-in):** `REGIME_SIZE_GATE=1` annotates each alert with the live regime + a
  suggested size factor (trend 1.0× / mixed 0.5× / chop 0.25×); `REGIME_SKIP_CHOP=1` suppresses alerts
  in choppy regime (logged, not sent). Reuses the hourly-cached regime; fail-safe to full size.
  Rationale: cross-era, cross-instrument, and fill-realism tests all show the edge is ~breakeven in chop.

### Docs / analysis (this session)
- `OVERFITTING.md` (params robust; edge generalizes to gold; regime-dependent), `FILL_REALISM.md`
  (survives realistic fills in trend, breakeven-to-negative in chop), `AUDIT.md` (15-section review).
- TradingJournal hub (`index.html`): tiles for `/performance`, `/lastalert`, `/archive`.

### Deploy
Upload `agent.py` + `manage.py`. Recommended: set `REGIME_SIZE_GATE=1` first (observe), then
`REGIME_SKIP_CHOP=1` once `/performance` confirms chop is the dead zone.

---

## v17–v19 — semi-auto execution (TradersPost → MFF eval)  (2026-06-24)

One-tap execution from Telegram. **No detection/backtest change** — execution is an additive side-channel.

### Added — agent (`agent.py`)
- **`_exec_order`**: posts a bracket order (limit + TP@2R + SL) to `EXEC_WEBHOOK` (the relay `/stage`).
  Fires in `_process_new` when `EXEC_WEBHOOK` is set — **one** Telegram message = alert + buttons.
- **Risk-based exec sizing**: `EXEC_QTY=auto` (default) → `live_emit.size_for` (the same 0.5%/`ACCOUNT`
  contract count shown in the alert's `📐 Ryzyko` line); a number = fixed. **`EXEC_MAX_QTY`** = hard cap on
  the contract count (e.g. the eval's max-position rule). MNQ = **micro**, $2/pt. Verified: a 10-pt sample
  stop → 25 micros (a clean 0.5%), capped to `EXEC_MAX_QTY`.
- **`/exectest`** now returns the relay's actual response (`relay.status` / `resp` / `sent`) for diagnosis.

### Added — relay (`NQsignals/main.py`)
- **`/stage`**: stores the pending order (file-based, shared across gunicorn workers), sends ONE Telegram
  message with inline **✅ Wejdź / ❌ Pomiń**, remembers the last ticker.
- **`/telegram` callback**: ✅ fires the order to `TRADERSPOST_WEBHOOK` (auto-submit ON → executes on MFF);
  ❌ discards. `answerCallbackQuery` first (clears the spinner).
- **Bail controls**: inline **🚫 ANULUJ/WYJDŹ** on the sent-confirmation; **persistent 🚫 FLAT reply-keyboard**
  + `/flat` `/panic` → send `cancel` (drop a working order) + `exit` (flatten a filled position) for the last ticker.
- **Diagnostics**: `/cbdebug` (last callback), `/envcheck` (what the process sees), `/setwebhook`
  (self-register; `allowed_updates` incl. `message`).

### TradersPost / MFF
- MFF **eval** account connected (`MFFUEVST…`, `MNQU2026`), Position Size = None + signal overrides,
  **auto-submit ON**. Webhook is per-strategy (MFF BOT) → same URL routes to the MFF subscription.
- **Confirmed working end-to-end**: test order `Completed` (280 ms), full bracket placed.

### Caveats (open)
- Agent has **no fill feedback** from MFF → reconcile alert vs actual fill manually.
- Running **without auto-BE** — manual BE on the `⚡1R` alert.
- TradersPost on free trial; the relay webhook URL is **hardcoded as default** (regenerate + move to a
  properly-injected env **before any real-money funded account**).

### Deploy
- `agent.py`: set `EXEC_WEBHOOK` (relay `/stage` URL incl. `?secret=`), `EXEC_QTY=auto`,
  `EXEC_MAX_QTY=16`, `EXEC_TICKER=MNQU2026`, `EXEC_TEST_SECRET`. **Env loads on restart — redeploy.**
- relay `main.py`: `TRADERSPOST_WEBHOOK`, `WEBHOOK_SECRET`; run `/setwebhook` once; send `panel` for the FLAT button.

---

## v20 — ATRMULT env · Model C dual-BOS + loose stream + candidates page  (2026-07-09)

Two live services touched. **Defaults keep current behaviour** — every change is opt-in via env or a separate tag.

### Changed — detector (`detcore/config.py`)
- **`ATRMULT` is now env-readable** in `Config.from_env` (was hardcoded `1.5`; the live A/B path builds via `from_env`, verified in `detcore/pipeline.py`). Default **unchanged at 1.5** — identical behaviour unless you set it.
- **`ATRMULT=1.0` = the validated A/B loosening.** `atrmult` is the displacement-strength PRE-gate (sum-of-bodies ≥ `atrmult`×ATR5m), *not* the edge (edge = rejection→BOS, untouched). 1.5→1.0: **+56% trades (1489→2325/yr), same per-trade exp (+0.209→+0.204), +52% total R/yr.** Walk-forward (train 22-24 / test 25-26) **HELD**: OOS total **R +649 vs +495 base = +31% more profit** at +65% more trades, positive every year incl 2026; every session stays +EV (extra trades are not junk). Set `ATRMULT=1.0` on the A/B service to run; delete to revert. Rig `Trading/ab_loosen.py`.

### Added — Model C live (`model_c_live.py`)
- **`C_BOS_MODE`** = `both` (default) | `strict` | `local`. Adds a second *local-swing* BOS path (struct = max-high of the last 5 bars before the hold) beside the strict whole-leg BOS. ⚠ **Backtest verdict: the local BOS is junk** — staircase + local = **−0.20R, 0/5 yrs** (whipsaw) vs strict **+0.213R, 5/5**. Wired for **inspection only**: local candidates carry a distinct `[LOCAL-BOS]` alert tag and route to TradersPost strategy **`STRATEGY_C_LOCAL`**, so they never mix into the strict `STRATEGY_C` book. Set `C_BOS_MODE=strict` to suppress them entirely.
- **`C_HOLD`** (`0.25`) — deep-FVG body-hold frac for the local variant; **`C_BOS_LOCAL_SESSIONS`** (empty = all) — optionally restrict the local path to given sessions.
- **`C_DISP_LOOSE`** (`0` = off) — additive *loose-staircase* extra-entry stream (relaxed retrace / min-leg) layered on the tight staircase; loose fills tagged `·LOOSE` / strategy **`STRATEGY_C_LOOSE`**. Knobs **`C_RETR_LOOSE`** (`0.75`), **`C_MINLEN_LOOSE`** (`6`). Default OFF — tight staircase unchanged.
- **`/candidates` now renders an HTML step-funnel** (displacement → rejection → BOS, showing which gate each candidate died at), matching the A/B candidates view — previously raw JSON.

### Deploy
- **A/B service**: replace `detcore/config.py`, set **`ATRMULT=1.0`** (or leave unset for current 1.5), redeploy — env loads on restart.
- **Model C service**: deploy `model_c_live.py`. Defaults preserve current behaviour except `/candidates` is now HTML and `C_BOS_MODE=both` surfaces (separately-tagged) local candidates; set `C_BOS_MODE=strict` for the old strict-only stream.
- ⚠ More alerts only help if executed — live coverage (~5% of book, sleeping through NYAM/PM_AH) is a bigger lever than any loosening. Gate 0 still reconciles vs the **broker export**, not `/pnl`.

### Companion artifact
- `Trading/AB_trades_lastmonth.pine` — 194 A/B (atrmult=1.0) would-be trades on the 2026-06-08→07-09 archive (72 win / 58 loss / 64 BE), each with entry-limit line + risk/reward boxes + E/SL/TP label, for TradingView.

---

## v22 — stop-cap re-anchor (keep wide-stop trades instead of discarding them)  (2026-07-15)

**A/B + Model C.** Default **OFF** (`STOP_CAP=0`) — deploying changes nothing until the env is set. Touches only the two paths that build entries via `detcore.entries.get_entry_v10` (A/B, Model C); F / ORB / AMD / forex are untouched (own stop logic).

### The idea
- SL is the **CE (midpoint) of the displacement FVG**. On long-displacement OTE/continuation entries that stop balloons (90–205pt), so `max_stop_r=40` **discards** the setup (`odciety cap`) — **~22% of all setups are thrown away**. The setups aren't bad; the stop is mis-anchored to a far structure.
- v22: instead of discarding, **re-anchor the SL to a fixed distance from entry** and keep the trade (TP stays 2R off the new stop).

### Changed — detector
- **`detcore/entries.py`** — new `_cap_stop()` called in `find_entry_v10` + `find_entry_fibo_v10`: if the structural (FVG-mid) risk exceeds the cap, move SL to `stop_cap` points from entry and recompute `risk`; `exits.take_profit` then gives the 2R target automatically.
- **`detcore/config.py`** — `stop_cap` (`STOP_CAP`) + `stop_cap_trigger` (`STOP_CAP_TRIGGER`) fields + `from_env`.

### Added — knobs
- **`STOP_CAP`** (`0` = off). Recommended **`28`** (25–30 is a flat optimum; 28 ≡ 30 in tests).
- **`STOP_CAP_TRIGGER`** (`0` = cap **every** trade, i.e. `SL = min(structural, STOP_CAP)`). Set `40` to only rescue the >40pt trades and leave the 28–40 cohort on their structural stop (the "stage-1" variant).
- **`MAX_STOP_R` is unchanged (`40`) but goes dormant** once `STOP_CAP>0` — every stop is already ≤ cap, so the discard never fires. It's the rollback backstop.

### Deploy
- Replace `detcore/entries.py` + `detcore/config.py`, and bump `agent.py` `VERSION` `v21`→`v22` (shows in `/status` + `/health` to confirm the deploy); set **`STOP_CAP=28`** on the A/B service (and the Model C service if separate). Roll back any time with `STOP_CAP=0`.

### Forward-test result  (real `detect()`, live cfg DISPWIN=30 / ATRMULT=1.0, BE@1R / TP=2R, no costs, 1R=$500)
- **4-year (2022-06→2026-06):** discard **+793R / $396.5k** → cap-all-28 **+929R / $464.5k (+$68k)**; win 31.3→31.7%, exp +0.278→+0.287. **Beats discard every out-of-sample year** (cap chosen on the 2026 seed ⇒ 2022-25 are OOS): Δ **+$1.5k / +$5k / +$10.5k / +$27.5k / +$23.5k**.
- **Last 12 months:** +$107.5k → **+$136k (+$28.5k / +27% per account)**; 762→912 trades.
- **`min(structural,28)` beats fixed-28-for-all** (+$110.5k): widening the ~78% of already-tight stops loses money — only the wide ~22% are re-anchored.
- **TP = 2R confirmed** — 3R tested last-year = much worse (+$17k, win 20%).
- Rigs: `Trading/dispwin30_run.py` (real pipeline at live cfg), `Trading/v22_deploy_summary.html`, `Trading/v22_forwardtest_live.html`.

---

## v23 — FVG-magnet size-up (bet bigger on reversals that target an unfilled FVG)  (2026-07-16)

**A/B only, size-only.** Isolated add-on `magnet.py` (same pattern as `select_tag.py`) — **READ-ONLY**: it only **tags** a setup and **multiplies position size**. It never changes entry / SL / **TP (2R kept)** / direction / whether a trade fires. Wrapped in `try/except` → fails closed, alerts + candidates keep running if it ever errors.

### The idea
- A **Reversal** whose 2R target reaches **into an unfilled opposing FVG** (a bearish gap above a long / bullish below a short) is a validated higher-quality cohort — the FVG is a **draw/magnet**, not a wall. Found while testing the opposite (a veto), which back-fired.
- Tag those and **size up**; everything else unchanged.

### Changed — new file + 4 hooks in `agent.py`
- **`magnet.py`** (new) — `load_buffer` + `check` + `tagline`: finds the nearest unfilled opposing FVG ahead (3-bar gap ≥ `MINFVG`, unfilled, within `LOOKBACK=120` bars) + counter-trend context (last-5 candidates opposite). Excludes `F.P.FVG` (its magnet is dead, +0.06R).
- **`_process_new`** — after `select_tag`: prepend a `🧲`/`🧲🧲` line to the alert and set `repx['_size_mult']`.
- **`_exec_order`** — `qty = round(qty * _size_mult)` inserted **before** the `EXEC_MAX_QTY` cap (auto-size **honours the MFF contract cap**; absorbed when risk-based size already ≥ cap).
- **`/candidates`** — a `🧲`/`🧲🧲` badge on confirmed (`POTWIERDZONY`) rows + `'magnet'` added to `_PREF`.

### Tiers (edit in `magnet.py`)
- **MAGNET** (reversal + unfilled FVG ahead, ~62/yr) → **×1.25** (`SIZE_MAGNET`).
- **PREMIUM** (+ last-5 opposite / counter-trend, ~35/yr) → **×1.5** (`SIZE_PREMIUM`). Set both to 1.5 to size the whole 62/yr uniformly.

### Deploy
- Upload **`magnet.py`** + **`agent.py`**, bump `VERSION` `v22`→`v23` (shows in `/status` + `/health`). **No env vars required** — defaults (1.25 / 1.5) are baked into `magnet.py`. *Optional:* tune from Railway without re-uploading via **`MAGNET_SIZE`** / **`MAGNET_SIZE_PREMIUM`**. Roll back by deleting `magnet.py` or setting both to `1.0`. `EXEC_MAX_QTY` still caps size.

### Forward-test result  (4yr MNQ, live cfg DISPWIN=30 / ATRMULT=1.0 / STOP_CAP=28, BE@1R / TP=2R, no costs, 1R=$500)
- **Holds out of sample** (train 2022-24 → hold-out 2025-26): reversal+FVG **+0.44R → +0.38R**, premium **+0.55R → +0.64R**, vs baseline **+0.29R → +0.25R**. Positive **8/8** six-month periods; bootstrap 90% CI (premium, OOS) **[+0.41, +0.90]**; cohort max-DD only **−6R**.
- **Earnings lift (last 12 months, $100k @0.5%):** broad 62/yr **×1.5 ≈ +$8k (+5.4%)**, **×2 ≈ +$16k** — drawdown-neutral. Rare (~1–1.7 / week), so modest but essentially free.
- **Rejected variants:** the opposing-FVG **veto** back-fires (those trades win *more*); the FVG-**rejection trigger** is weak (+0.20R OOS, ~87% duplicate existing catalysts); it **does NOT transfer to Continuation** (collapses OOS) or to Model C (too few trades). **2R kept** (1.5R wins more often but earns slightly less; FVG-edge target ties 2R at a higher win-rate — parked).
- Doc / rigs: `Trading/FVG_MAGNET_STRATEGY.html`, `Trading/FVG_MAGNET_SESSIONS_EXAMPLES.html`, `magnet.py`, `stress_params.py`.

---

## v26 — AUTO executor: MFF-eval-safe order gate + live counter  (2026-07-17)

### The idea
- The resting-limit edge (**+0.19R**) only pays if the limits are actually *placed*. v26 closes the last manual
  gap: a **fail-closed gate** sits between detection and execution and fires the order itself — but only when
  every eval-safety rule passes. It never widens what trades; it can only **subtract** (skip / shrink / halt).
- **Nothing is added to the detector.** `guardrails.py` reuses the existing `shadow.py` price resolver and reads
  the same signal stream; it just decides *place / hold / abort* and remembers state in `DATA_DIR`
  (`guard_state.json`, `guard_log.json`).

### Added — `guardrails.py` (new, next to `shadow.py`)
- **3 modes, runtime-flippable** (`EXEC_MODE`, or the buttons on `/guard`): **AUTO** places orders · **MANUAL**
  arms them for one-tap · **OFF** = alerts only. Flip live at `POST /guard/mode` — no redeploy.
- **Stale-data abort (fail-closed):** if the feed is older than `STALE_MIN` (20) the gate **aborts** rather than
  trading on old prices. Unknown age → abort (the safe default), not trade.
- **Eval counter** — `eval_progress()`: live equity, P&L, **% to the $6k target**, `passed` (≥ `TARGET_BALANCE`)
  and `breached` (hit the trailing-drawdown floor). The agent always knows where it stands in the eval.
- **Drawdown guard:** halts if equity comes within `DD_BUFFER` ($800) of your real MFF trailing floor
  (`DD_FLOOR`, read off the MFF dashboard). **■ HALT** latches — it won't silently un-halt.
- **Session gate by FIRING time** (`SKIP_SESSIONS=LO,ASIA,NYL`) — excludes London/Asia (+ NY-Lunch in eval).
  Uses the detector's real firing session, **not** the catalyst label.
- **Monday-from-NYAM** (`MONDAY_MODE=nyam`): keep Monday but **skip its PREM** — start at the NYAM open, since the
  week has barely begun. (`skip` = no Monday · `quarter` = quarter-size · `full` = normal.)
- **One order per setup** (`is_duplicate`): collapses the ~4.3× confluence/model inflation so a single setup can't
  fire four overlapping limits (and no duplicate Telegram alert).
- **Ramp** (`RAMP_TRADES=3`): the first 3 auto orders are forced to **1 contract** to prove broker routing before
  risk-based sizing (`RISK_PCT`, capped at `EXEC_MAX_QTY`) kicks in.
- **Daily caps:** `MAX_TRADES_DAY` (3), stop after `DAY_LOSS_N` (2) losers or `DAY_LOSS_USD` ($1000) lost, and
  bank a green day at `DAY_TARGET_USD` ($1500).
- **Routes:** `/guard` (dashboard + panic buttons), `/guard/data` (JSON the dashboard card polls), `/guard/mode`
  (flip mode), `/guard/sync?equity=` (feed the real MFF balance to the counter), `/guard/kill` (latch HALT).
- **Health check** `/guard/health` — a structured self-diagnosis so AUTO can't break silently. Catches
  armed-but-`EXEC_WEBHOOK`-unset, halt latched, `DD_FLOOR` unset (guard blind), feed/equity stale, `DATA_DIR`
  unwritable, the gate raising, and pipeline idle during RTH. Returns **HTTP 200** (ok/warn) or **503** (critical)
  so a free uptime monitor (Healthchecks.io / UptimeRobot) pointed at it becomes a **dead-man alarm**; `?format=txt`
  for a human checklist. On any health **transition** it pushes one Telegram line (set `GUARD_ALERT_URL`, or it
  reuses `WEBHOOK_URL`). A per-cycle `guardrails.beat()` in the agent heartbeat loop keeps the liveness signal
  honest even on quiet days. The `/guard` page shows a HEALTH pill; `/guard/data` carries the verdict.

### Changed — `agent.py` (v25 → **v26**), `dashboard.py`, `allview.py`
- `agent.py`: `import guardrails` + `guardrails.register(app)`; the `EXEC_WEBHOOK` block is replaced by the
  3-mode gate; two `qty` lines in `_exec_order` honour the ramp / Monday-quarter overrides; one `guardrails.beat()`
  line in the heartbeat loop feeds the health check. Quiet log for the expected skips (`duplicate`, `monday_skip`,
  `monday_prem`) so they don't spam.
- `agent.py` **`/status`**: new **`auto_live`** flag — 🟢 true only when mode is `auto` **and** `EXEC_WEBHOOK` is
  set (i.e. it will really place orders), plus `auto_mode`. One glance tells you if it's armed.
- `dashboard.py`: a live **Auto-Executor card** (mode · eval progress bar · P&L · today's trades, polling
  `/guard/data` every 30s) and the `/guard` page in the nav. **Removed** the retired *Shadow executor* and
  *Compare* nav entries.
- `allview.py`: **row-dedup** in the All-trades table — one row per setup (fullest catalyst kept, chart link
  never dropped), with a `×N` badge showing how many confluences merged. Toggle with `ALLVIEW_DEDUP`.
- `MANAGE_BE=0` stays the default: fixed stop beats break-even (**+0.298R vs +0.146R** over 4 years).

### Why AUTO, not just alerts (the honest result)
- **12-mo forward test:** taking *every* signal (MANUAL) **breaches the eval drawdown in month one**. The same
  year run through the AUTO gate **passes**. The rules don't add profit — AUTO nets ~5% *less* gross — they
  **survive the drawdown** that otherwise ends the eval. Capital preservation, not alpha.
- **Monte Carlo (400 sims, realistic ~57% resting-limit fills):** live pass rate ≈ **54%**, not 100%. Median
  pass ≈ **2.3 months**, not the 8 the all-signal curve implies. Sizing up on a tight buffer *lowers* the pass
  rate — hence `RISK_PCT=0.4` and `EXEC_MAX_QTY` during the eval.
- **Overfitting check (all 4 years):** Monday is **mixed by year** (−0.04 / +0.02 / +0.06 / −0.14 / +0.21), *not*
  robustly negative — so `MONDAY_MODE=nyam` is **variance reduction**, not a mined edge. Same logic for the
  session skips: they cut drawdown, they aren't cherry-picked winners.

### Deploy
- Add `guardrails.py`; replace `agent.py` / `dashboard.py` / `allview.py` (**diff first** — patch the copies only
  if your deployed repo matches). Set the env vars below. **TradersPost auto-submit ON** is the actual hands-off
  switch — the code stages the order, TradersPost fires it. First 3 orders run at 1 contract; confirm each lands
  on MFF before trusting size. Roll back anytime: `EXEC_MODE=manual` (or `off`) — zero redeploy.

---

## Env reference

| Var | Where | Default | Meaning |
|---|---|---|---|
| `MODE` | detector | `confirm` | `confirm` (multi-break) / `sweep` (first breach) |
| `CAP_DAYS` | detector | `10` | backstop life in trading days |
| `EOD_INTRADAY` | detector | off | session H/L tapped-unconfirmed expire EOD |
| `DISP_MODE` | detector | `chain` | displacement: `chain` (V1) / `orig` (V0) |
| `MINIMP` | detector | `3` | V1: min candles to start the chain |
| `MAXEXT` | detector | `40` | V1: max chain length (cap) |
| `DISPWIN` | detector | `10` | bars after a tap a displacement may start; **30 recommended** (walk-forward) |
| `REGIME_GATE` | agent | off | `1` = det_v11 for live AND /candidates + regime-gated EOD + Telegram flips |
| `DET_FILE` | agent | det_v10 / det_v11 | which detector the agent runs |
| `REGIME_TTL_SEC` | agent | `3600` | how often regime is rechecked |
| `REGIME_DET` | regime | `det_v11.py` | which detector `regime.py` classifies with |
| `MAX_RETEST` | agent | `0` | skip a trade whose freshest catalyst is beyond N re-tests (0=off; needs v15 `agent.py`) |
| `ACCOUNT` | agent | `100000` | account $ for position sizing — **set to your real account** |
| `RISK_PCT` | agent | `0.5` | % of account risked per trade |
| `PRICE_OFFSET` | live_emit | `0` | feed→broker contract price gap; set only during a contract roll |
| `FRESH_MIN` | agent | `15` | only alert if BOS is within N minutes |
| `REGIME_SIZE_GATE` | agent | off | `1` = annotate alerts with regime + size factor (trend 1.0× / mixed 0.5× / chop 0.25×) |
| `REGIME_SKIP_CHOP` | agent | off | `1` = suppress alerts in choppy (red) regime |
| `MAX_STOP_R` | detector | `40` | risk cap (points) — discard setups needing a wider stop; **dormant when `STOP_CAP>0`** |
| `STOP_CAP` | detector | `0` | **v22** — `>0` re-anchors any stop wider than this to this many pts & keeps the trade (TP=2R); **`28` recommended** |
| `STOP_CAP_TRIGGER` | detector | `0` | **v22** — `0` = cap every trade (`min(structural,STOP_CAP)`); `40` = only rescue >40pt trades |
| `CUTOFF` | detector | `2026-05-17` | emit filter; empty = all (backtest) |
| `EXEC_WEBHOOK` | agent | (empty) | relay `/stage` URL incl. `?secret=` — enables one-tap execution |
| `EXEC_QTY` | agent | `auto` | exec size: `auto`=risk-based (`size_for`) / a number=fixed |
| `EXEC_MAX_QTY` | agent | (none) | hard cap on exec contract count (eval max-position rule); MNQ micros |
| `EXEC_TICKER` | agent | `CONTRACT`/`MNQ1!` | broker symbol for exec orders (e.g. `MNQU2026`) |
| `EXEC_TEST_SECRET` | agent | (empty) | gate for `/exectest` |
| `EXEC_MODE` | **v26** guardrails | `auto` | `auto` (place) / `manual` (arm, you tap) / `off` (alerts only) — flip live at `/guard/mode` |
| `SKIP_SESSIONS` | **v26** guardrails | `LO,ASIA` | firing-time sessions the executor won't trade (eval: `LO,ASIA,NYL`) |
| `MONDAY_MODE` | **v26** guardrails | `nyam` | `nyam` (Mon starts NYAM, skip PREM) / `skip` / `quarter` / `full` |
| `MAX_TRADES_DAY` | **v26** guardrails | `3` | max auto orders per day |
| `DAY_LOSS_N` | **v26** guardrails | `2` | stop for the day after N losers |
| `DAY_LOSS_USD` | **v26** guardrails | `1000` | stop for the day after $ lost |
| `DAY_TARGET_USD` | **v26** guardrails | `1500` | bank a green day — stop after $ gained |
| `DD_FLOOR` | **v26** guardrails | (none) | your **real** MFF trailing-drawdown $ level (read off the MFF dashboard) |
| `DD_BUFFER` | **v26** guardrails | `800` | halt if equity comes within $ of `DD_FLOOR` |
| `RAMP_TRADES` | **v26** guardrails | `3` | first N auto orders forced to 1 contract (prove routing) |
| `START_EQUITY` | **v26** guardrails | `100000` | eval start equity for the counter |
| `START_BALANCE` | **v26** guardrails | `100000` | eval start balance for the counter |
| `TARGET_BALANCE` | **v26** guardrails | `106000` | pass line ($6k target) |
| `STALE_MIN` | **v26** agent/guardrails | `20` | abort if feed older than N min (fail-closed) |
| `GUARD_ALERT_URL` | **v26** guardrails | (uses `WEBHOOK_URL`) | where to push the one-line Telegram alert on an AUTO health transition |
| `HEALTH_IDLE_MIN` | **v26** guardrails | `120` | flag pipeline idle if the gate sees no activity for N min during market hours |
| `MANAGE_BE` | agent | `0` | `1`=move to BE at 1R; `0`=fixed stop (fixed wins +0.298R vs +0.146R / 4yr) |
| `RISK_PCT` | agent | `0.5` | % risked per exec order (**eval: `0.4`** on a tight buffer) |
| `ALLVIEW_DEDUP` | allview | `1` | `1`=one row per setup (collapse confluence dupes) / `0`=raw |
| `TRADERSPOST_WEBHOOK` | relay | (hardcoded MFF BOT) | TradersPost strategy webhook — fires orders |
| `WEBHOOK_SECRET` | relay | `nqscout2024` | guards `/stage`, `/cbdebug`, `/envcheck`, `/setwebhook` |
| `TG_WEBHOOK_SECRET` | relay | (empty) | Telegram `setWebhook` secret_token header |
| `FLAT_TICKER` | relay | (empty) | fallback ticker for 🚫 FLAT if none stored yet |
| `ATRMULT` | detector | `1.5` | displacement-strength gate (sum-bodies ≥ ×ATR5m); **`1.0` = +65% A/B setups, WF-validated** |
| `REJ_FRAC` | detector | `0.5` | rejection body-hold as frac of FVG (0.5=CE / 0.25=stricter) |
| `C_BOS_MODE` | model C | `both` | `both`/`strict`/`local` — add local-swing BOS (tagged `STRATEGY_C_LOCAL`; backtest junk, inspection only) |
| `C_HOLD` | model C | `0.25` | body-hold frac for the local-BOS variant |
| `C_BOS_LOCAL_SESSIONS` | model C | (all) | restrict the local-BOS path to given sessions |
| `C_DISP_LOOSE` | model C | off | `1` = additive loose-staircase extra-entry stream (`STRATEGY_C_LOOSE`) |
| `C_RETR_LOOSE` | model C | `0.75` | loose-stream retrace frac |
| `C_MINLEN_LOOSE` | model C | `6` | loose-stream min leg length |

(Unchanged v10 vars — `DATA_CSV`, `OUT_PKL`, `ENTRY_PRIMARY`, `DEBUG_TRACE`, `TRACE_OUT`,
`WEBHOOK_URL` — behave as before.)
