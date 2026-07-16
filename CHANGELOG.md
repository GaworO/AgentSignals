# Changelog — MNQ detector

Baseline = v10 (`det_v10.py`, time-based invalidation). Forward tests: MNQ 1m, 2022-06→2026-06,
CONFIRM mode, BE@1R / TP=2R, intrabar SL-first, costs in R vs each trade's own stop.

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
