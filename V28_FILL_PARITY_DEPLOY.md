# v28 — fill parity + the 10-minute cancel

Two independent changes, one patch. Change A makes the backtest describe the machine you actually
run. Change B is the money: cancel the resting entry limit at 10 minutes.

Patch: `v28_fill_parity.patch` (222 lines, 8 files). Verified: applies clean with `patch -p1`,
`py_compile` passes on every touched module, and both smoke tests below reproduce live to the cent.

```
cd AgentSignals-main
patch -p1 < v28_fill_parity.patch
python3 -m py_compile agent.py guardrails.py shadow.py manage.py detcore/*.py
```

---

## A · Why the backtest read +0.489R and live reads +0.261R

Two separate leaks. Measured over 4 years, 3,021 deduped signals, live config
(ATRMULT=1.0, STOP_CAP=30/40, MAX_STOP_R=40, ENTRY_OFFSET_PTS=1, rr=2).

| what the model was doing | 4y net R / fill |
|---|---|
| backtest as written today — fill window opens at `bos_bar+1` | **+0.4895** |
| same entries, fill window opens at `entry_bar` (the field `emit()` already stores) | **+0.2169** |
| causal entries (entry may only read bars ≤ BOS bar), fill at `entry_bar` | **+0.2606** ← what live gets |

**Leak 1 — the fill happened on the bar that created the level. Worth 0.27R, i.e. 81% of the gap.**
`find_entry_v10` scans `fvgs(ctx, ob, bb + 2, bull)`, so the FVG that defines the entry can have its
middle bar at `bb+1`. Every consumer then opened the fill window at `bos_ms + 1 bar` — the *same*
bar. The order was being filled by the price action that created its own level.

The fix needed no new logic. `detcore/emit.py` has stored the right bar all along:
`entry_bar = max(sfvg_bar | hh_bar, bos_bar) + 1`. Nothing used it. Lag distribution over 4 years:
1,227 signals at +1 bar, 377 at +2, 325 at +3, 359 at +4/+5 — only 395 were genuinely tradeable on
`bos+1`. The patch adds `entry_ms` next to `entry_bar` in `emit()` and makes `shadow.score()` anchor
on it (`score(..., entry_ms=...)`). Old shadow rows have no `entry_ms` and silently fall back to
`bos_ms`, so nothing in the existing log breaks.

**Leak 2 — the backtest entry is not the entry live quotes. Worth the remaining ~0.04R, but it is
what makes trade-by-trade comparison possible at all.** Live, the buffer ends at the BOS bar, so
`fvgs(ob, bb+2)` finds nothing and the code falls through to the OTE/fibo entry; `impulse_end_v10`'s
stall rule likewise walks to `bb+cap` in a backtest and stops at `bb` live. Result: **76% of live
entries are OTE, only 24% FVG — in a backtest it is 53% FVG.** Half your trades were being measured
at a price the machine never quotes.

`Config.causal` (default `True`, env `DET_CAUSAL=0` to restore) clamps both scans to the BOS bar.
Live behaviour does not change — live was already causal. Only the backtest moves, onto the truth.

**Do not try to close this gap by waiting for the better entry.** I tested it: letting the entry calc
see one extra closed bar gives +0.269R against +0.261R (a wash, with a longer losing run), two bars
+0.200R, three +0.133R. The +0.489R was never reachable.

### Two smaller book-vs-broker mismatches, fixed in the same patch

**Fill-bar phantom win — +0.049R/fill of modelled edge that was never real.** `score()` already
delays the *fill* search by a bar but the outcome loop still scored the whole fill bar, so a
favourable extreme printed *before* the limit was hit counted as a target. 23.07 NYAM long is the
case: book `+1.937R`, broker `−$385`. Patched, the same call returns `loss −1.063R`.
`SHADOW_FILLBAR_TP=1` restores the old behaviour if you want to A/B it.

**The book stored a take-profit the broker never received — −0.044R/fill.** `_exec_order()` computes
`tp = entry ± 2R` from the *post*-`ENTRY_OFFSET_PTS` entry; `guardrails.note()` and `shadow.record()`
stored the detector's *pre*-offset `x['TP']`. At offset 1 they are 3 points apart — visible on
Friday: the Pine drew NYPM ×12's target at 28347.67, the broker held 28344.75. The patch stashes
`x['_exec_tp']` at send time and books that. `/guard`, the Pine export and the shadow gate now all
describe the order that exists. The two errors nearly cancelled at offset 1, which is why neither
surfaced — they stop cancelling the moment you touch the offset.

### Expect the /guard numbers to fall after deploy

That is the bug leaving, not a regression. Honest baseline: **+0.26R/fill at the old 240-minute
window, +0.60R at the 10-minute one.** Re-baseline the Gate-0 threshold against those, not against
the old backtest — +0.15R was set against a number that read +0.49R.

---

## B · The 10-minute cancel

4-year expectancy by how long the limit waited: **0–1 min +0.972R · 1–5 min +0.583R · 5–10 min
+0.331R · 10–15 min −0.014R · 15–30 min −0.096R · 30–60 min −0.068R · 60–240 min −0.089R.**
Stop distance (13–15 pt) and contract count are flat across every bucket, so it is not a size
artefact — the setup goes stale. Fills after 10 minutes are 51% of your trade count and lose money.

| cancel after | fills | fill rate | R/fill | 4y net | worst run | peak DD |
|---|---|---|---|---|---|---|
| 5 min | 912 | 30.2% | +0.707 | $223,344 | 6 | $3,447 |
| **10 min** | **1,277** | **42.3%** | **+0.599** | **$269,169** | **8** | **$3,367** |
| 15 min | 1,525 | 50.5% | +0.499 | $266,975 | 8 | $4,056 |
| 30 min | 1,900 | 62.9% | +0.382 | $259,056 | 9 | $5,542 |
| 240 min (today) | 2,578 | 85.3% | +0.261 | $242,919 | 10 | $12,128 |

Every year separately: 2022 +0.178→+0.474 · 2023 +0.254→+0.546 · 2024 +0.303→+0.730 ·
2025 +0.245→+0.518 · 2026 +0.312→+0.738, with drawdown down 60–70% in each. The curve is monotone
from 5 to 240 minutes, so there is no peak to have overfitted to. 5 minutes has the best per-trade
number but gives up $46k of total profit; 10 is the knee.

### Where it lives

| file | what changed |
|---|---|
| `shadow.py` | new `_fill_win()` → `FILL_WIN_MIN` (default **10**). `score(fill_bars=None)` reads it; `refresh()` passes `entry_ms`. This is the clock — everything downstream keys off the `no_fill` verdict. |
| `guardrails.py` | `sweep_orphans()` gains `SWEEP_LAG_MIN` (default **3**) — see the race note below. |
| `manage.py` | `check(fill_ms=None)` now defaults to the same `FILL_WIN_MIN` instead of a hardcoded 2 h. |

No new cancel path is needed. `sweep_orphans()` already cancels at the broker as soon as the model
writes a row off as `no_fill`; it was simply never firing before the 4-hour mark.

### The one sharp edge — read this before shipping

`flatten_cancel_only()` sends a **blanket `{'action':'cancel'}` for the whole ticker**. It is guarded
by `if d['openpos']: return 0` in `sweep_orphans()`. On the old 240-minute clock that guard almost
never mattered; on a 10-minute clock the sweep runs constantly, and a fill at 9:59 that the book has
not resolved yet would have its bracket stripped. `SWEEP_LAG_MIN=3` holds the sweep until 13 minutes
after the send, past the point where the next `shadow.refresh()` has seen the fill. Do not set it
to 0. If you ever run a second strategy on the same ticker, this blanket cancel is the thing to
re-engineer first.

### Second-order effect worth watching

An unfilled limit used to hold the one-position slot for `EXT_OPEN_H` (4 h), silently blocking every
later setup that day. On the 10-minute clock the slot frees almost immediately, so **daily send count
will go up**. `MAX_TRADES_DAY` only counts rows that consumed risk (`no_fill`/`missed`/`canceled` are
excluded), so the cap still binds on real trades — but watch the first week. The $269k figure assumes
every deduped signal is sent, which means today's live book is if anything *worse* than the +0.261R
baseline, not better.

---

## Env

```
FILL_WIN_MIN=10          # new — the whole change B
SWEEP_LAG_MIN=3          # new — race guard on the blanket ticker cancel
DET_CAUSAL=1             # new — default; 0 = old look-ahead, backtest only
```

Rollback without redeploying: `FILL_WIN_MIN=240`, `SWEEP_LAG_MIN=0`, `SHADOW_FILLBAR_TP=1`,
`DET_CAUSAL=0` restores every old behaviour exactly.

## Smoke tests that were run

1. **Causal detector reproduces Friday's live orders to the cent** — buffer truncated at the BOS bar,
   `ENTRY_OFFSET_PTS=1`: NYPM ×12 → 28377.39 / SL 28393.75 (live: 28377.39 / 28393.75);
   NYPM ×10 → 28243.08 / SL 28262.60 (live: 28243.08 / 28262.60).
2. **`shadow.score()` against broker truth** — 23.07 NYAM long: was `win +1.937R`, now `no_fill`
   (11.5 min wait, cancelled by the new clock); at `FILL_WIN_MIN=240` it returns `loss −1.063R`,
   matching the broker's −$385. 24.07 NYPM ×10: `loss` both before and after (4.8 min, inside the
   window). 24.07 NYPM ×12: was `open` for 4 hours, now `no_fill`.
3. `patch -p1 --dry-run` clean, `py_compile` clean on all 8 files.

## What is NOT in this patch

No partials, no breakeven, no change to the 2R target — all of them test worse (50% at 1R + BE costs
0.093R/trade, −36% of the edge). No change to `ENTRY_OFFSET_PTS`; 1 pt is already at the plateau.
