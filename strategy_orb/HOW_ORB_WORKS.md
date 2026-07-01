# How Strategy ORB works — plain-English guide, with examples

_Strategy ORB = "Opening-Range Breakout." A momentum strategy that catches trend-from-open days.
This explains exactly what the robot does, why, and shows it on real trades._

---

## 1. The idea in one sentence
The first 15 minutes of the US cash session (09:30–09:44 New York time) set a price **box**. On most days
price just chops inside/around it — but on ~1 day in 9 it **breaks out and trends all day**. ORB catches those
trend days, in the direction of the break, only when it agrees with the bigger trend.

**The whole edge is those ~11% trend days.** On the other ~89% it scratches roughly even. You are not trying
to win often — you are trying to be positioned when a trend day happens.

---

## 2. Why it works — and why it is NOT one of your ICT strategies
- ORB is **pure momentum**. It has **no FVG, no displacement, no liquidity sweep, no BOS** — none of the ICT
  machinery in A/B/C/F. It's a classic opening-range / "initial balance" breakout.
- Your A/B, C and F all wait for a **retracement** (price pulls back into a gap, then you enter). ORB does the
  **opposite** — it enters on the **break itself, with no pullback**.
- That opposite design is the point: it captures the clean trend-from-open days your retrace strategies
  **skip by design**, and its daily profit/loss is only **~0.10 correlated** with A/B. It diversifies you.

---

## 3. The exact steps the robot runs, every day

**Step 1 — Draw the box.** From 09:30 to 09:44 ET, record the highest high and the lowest low. That range is
the "opening range."

**Step 2 — Wait for a break.** From 09:45 on, watch each 1-minute candle:
- first candle to **CLOSE above** the box high → **LONG** signal,
- first candle to **CLOSE below** the box low → **SHORT** signal.
- It must be a **close**, not just a wick poking through — a wick is often a fakeout.

**Step 3 — Apply the two filters (only take the good breaks):**
- **Time filter:** the break must happen **by 10:30 ET.** Breaks after that lose money (tested 4 years).
- **Trend filter:** only trade **with** the 20-day trend. Price above its 20-day average = bullish regime →
  only LONGs. Below = bearish regime → only SHORTs. Counter-trend breaks are the losing slice.

**Step 4 — Place the order (one bracket):**
- **Entry:** at the edge of the box you broke (a stop-limit order).
- **Stop-loss:** the **opposite** edge of the box. That distance = **1R** (your risk unit).
- **Target:** **2.5R** (the researched best; 2R–3R all work, 2.5R is the sweet spot).
- **No break-even.** Moving the stop to break-even at +1R actually *hurts* this strategy (it scratches the
  trend days that pay). Let it run.

**Step 5 — Manage: nothing.** It either hits 2.5R, hits the stop, or closes at the 15:59 bell. Position size
is automatic: **1R is always 0.5% of the account**, so on a wild day with a wide box you trade *fewer*
contracts (see Example A).

---

## 4. Worked examples (open the PNGs in this folder)

### Example A — textbook SHORT · `AUDIT_breakout_textbook.png` · 2024-07-24
- The market gapped down hard; the 20-day trend was **bearish** → only shorts allowed.
- Opening range 09:30–09:44 was **128 points** wide (huge — it was a volatile day).
- At **09:56** a 1-minute candle **closed below** the range low → **SHORT alert**.
- Entry at the low edge; stop at the high edge (128pt = 1R); target 2.5R lower.
- Price trended down the rest of the day → **target hit. Win.**
- Because the stop was 128pt, position size dropped to **1 micro (0.26% risk)** — the strategy protects you
  from the wide stop automatically. You never override that.

### Example B — when the alert reaches you · `ORB_alert_timeline.png` · 2025-09-22 LONG
- 20-day trend **bullish** → longs allowed. Opening range 24814–24858 (44pt).
- **09:47** price pokes above the box (intrabar) — *no alert yet.*
- **09:48** the candle **closes** above the box → **this is the exact second the Telegram message is sent.**
- You **tap approve** → TradersPost fills you ~24858. Stop 24814 (44pt = 1R). Target 24969 (2.5R).
- Target hit ~12:20. **Win.**

### Example C — a normal LOSER · `AUDIT_breakout_grid.png` (2023-05-17 panel)
- The break fired, but the day **chopped and reversed**, price ran back through the box to the opposite
  edge → **−1R.** This is normal: **~46% of trades stop out.** The math still wins because the winners pay
  **2.5R** and you only take bias-aligned, early breaks. Don't be rattled by a string of −1R days.

### Example D — Friday runner
- Fridays are trend days more often than any other weekday. On those (and other high-conviction days) the
  edge keeps growing to **3R–4R**, so a bigger target is justified there.

The 6-panel `AUDIT_breakout_grid.png` shows a LONG winner, a SHORT winner, a Friday, the tariff-day reversal,
a clean loser, and a chop scratch — so you can see the full range of outcomes.

---

## 5. How you interact with it live
```
Telegram alert (tagged 🅾)  →  you TAP approve  →  TradersPost stages the stop-limit bracket
      →  it hits 2.5R  or  the stop  →  the trade is logged on your dashboard
```
- **Reactive** (you tap after the alert — the default): realistic edge **≈ +0.12R**.
- **Proactive** (one tap at 09:45 arms resting orders at both edges): **≈ +0.25R** — double, same risk.
- Everything is a **separate stream** from A/B/C/F: own Telegram tag, own TradersPost strategy, own log.

---

## 6. What to expect (backtest, 4 years, bias-aligned default)
| | |
|---|---|
| Frequency | ~**116 trades/year** (~2–3 per week) |
| Win rate to target | ~**17–20%** (low — it's a 2.5:1 payoff, judged on expectancy, like your F) |
| Expectancy | **+0.12R reactive / +0.25R proactive** (net of cost) |
| Consistency | **positive every year** 2022–2026 |
| Correlation with A/B | **~0.10** (diversifier) |

**This is in-sample** (found on the same 4 years). The live dashboard's realized number is the one that
counts. **Gate 0: prove ≥ +0.10R over 30–50 live trades before you size it up.**

---

## 7. Settings you can change (Railway env vars)
| Variable | Default | Meaning |
|---|---|---|
| `ORB_MIN` | 15 | opening-range length (minutes) |
| `ORB_LATE_CUTOFF` | 10:30 | skip breaks after this time |
| `ORB_TARGET_R` | 2.5 | take-profit in R |
| `ORB_REQUIRE_BIAS` | 1 | only trade with the 20-day trend |
| `ORB_NOBE` | 1 | no break-even (BE hurts this strategy) |
| `ORB_FRIDAY_ONLY` | 0 | (optional) only trade Fridays |
| `ORDER_TYPE` | stopLimit | stopLimit \| stop \| limit |
| `STRAT_ORB_ENABLED` | (unset) | master on/off. Unset = idle/dry-run |

Full deploy steps: **`DEPLOY_ORB.md`**. The strategy code: **`orb_live.py`**.
