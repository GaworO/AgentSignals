# Deploy Strategy ORB — runbook (GitHub → Railway)

Strategy **ORB** (Trend-Day Opening Breakout) runs **exactly like Strategy F/C**: its own process, own
Telegram, own TradersPost, reads the shared bar buffer **read-only**. It **cannot disturb A/B, C or F**
(separate service, imports only `live_emit` read-only). It is a **momentum** strategy — NOT ICT — and is
~0.10 correlated with A/B, so it diversifies.

It is also a **web service with a dashboard** (candidates · trade log · live performance), like the F app.

---

## What's in this folder
- **`orb_live.py`** — the whole strategy: signal detector + Telegram alert + TradersPost bracket + Flask dashboard.
- `DEPLOY_ORB.md` — this runbook.
- `orb_performance.html` — static backtest performance (open it locally any time).

Nothing else in the repo changes. `agent.py`, `detcore/`, `strategy_f/`, `model_c_live.py` are untouched.

---

## STEP 1 — put the folder in your GitHub repo
You already have the `AgentSignals` repo on GitHub (the one Railway builds). Add this folder to it.

**Easiest (GitHub website, no git needed):**
1. Open your repo on github.com → make sure you're on the `main` branch.
2. Click **Add file → Upload files**.
3. Drag the **`strategy_orb`** folder (with `orb_live.py` inside) into the page.
   - If the browser won't take a folder, create it: type `strategy_orb/orb_live.py` as the filename and paste the file contents.
4. Commit message: `add strategy ORB (opening-range breakout, separate service)` → **Commit changes**.

**Or with git (terminal):**
```
cd AgentSignals
mkdir -p strategy_orb && cp "/path/to/strategy_orb/orb_live.py" strategy_orb/
git add strategy_orb/orb_live.py strategy_orb/DEPLOY_ORB.md
git commit -m "add strategy ORB (opening-range breakout, separate service)"
git push origin main
```

**Check `requirements.txt`** (repo root) contains: `pandas`, `numpy`, `flask`, `gunicorn`, `requests`.
Your F service already needs flask+gunicorn, so they're likely there. If `pandas`/`numpy` are missing, add them.

---

## STEP 2 — new Railway service (same repo)
1. Railway → your project → **New → Deploy from GitHub repo** → pick the **same** `AgentSignals` repo.
2. Rename the service **`strategy-orb`** so you can tell it apart.
3. **Settings → Start command** (one worker — the dashboard runs its own poll loop):
   ```
   gunicorn --chdir strategy_orb orb_live:app --workers 1 --timeout 120 --bind 0.0.0.0:$PORT
   ```
4. **Settings → Networking → Generate Domain** → this gives you the dashboard URL (candidates/log/performance).

---

## STEP 3 — Variables (Railway → strategy-orb → Variables)
Start it **muted** to watch it for a day (alerts print to logs, nothing sent). Then flip it live.

```
# --- data + money ---
STRAT_ORB_BUF   = /data/buffer.csv          # SAME buffer the agent writes (shared /data volume)
ACCOUNT         = 100000
RISK_PCT        = 0.5
EXEC_TICKER_ORB = MNQU2026                   # your current contract
EXEC_MAX_QTY_ORB= 16

# --- alerts / execution (leave EXEC blank for paper-watching first) ---
STRAT_ORB_WEBHOOK = <your Telegram /webhook?secret=...>   # can be the same chat as A/B — alerts are tagged 🅾
# EXEC_WEBHOOK_ORB = <TradersPost relay for ORB>          # ADD ONLY when you want it to place orders
                                                          # use a SEPARATE TradersPost strategy "STRATEGY_ORB"

# --- journal / dedup on the volume so they survive restarts ---
SENT_ORB_FILE   = /data/sent_signals_ORB.json
ORB_TRADES_FILE = /data/orb_trades.json

# --- kill switch: OFF until you've watched it ---
# STRAT_ORB_ENABLED = 1     # <-- add this only when you're ready to actually send alerts
```
Attach the **same volume** at `/data` that the agent/F/C use (so it reads the live buffer and writes its journal there).

Optional detector knobs (defaults are the researched values — leave unless testing):
```
ORB_MIN=15  ORB_LATE_CUTOFF=10:30  ORB_TARGET_R=2.0  ORB_REQUIRE_BIAS=1
ORB_SLIP_TICKS=2  ORB_NOBE=0  ORDER_TYPE=stopLimit
# boosters (off by default): ORB_FRIDAY_ONLY=1  ORB_REQUIRE_GAP=1
```

---

## STEP 4 — watch it, then arm it
1. Open the **dashboard URL** (Step 2.4). You'll see: **① candidate today**, **② performance**, **③ trade log**.
   With `STRAT_ORB_ENABLED` unset it still shows candidates + reconciles a log (paper), just sends nothing.
2. Leave it a day or two. Confirm the candidate panel matches what you see on the chart (OR box, break, direction).
3. **Go live in two stages:**
   - **Alerts only:** set `STRAT_ORB_ENABLED=1` (Telegram fires; no orders yet).
   - **Auto-orders:** add `EXEC_WEBHOOK_ORB` pointing at a **separate** TradersPost "STRATEGY_ORB". Now each alert stages a **stop-limit** bracket.

## Kill switch
Delete `STRAT_ORB_ENABLED` (or set `=0`) → ORB goes idle immediately. A/B, C, F unaffected.

---

## What each ORB alert does
A **stop-limit** at the opening-range edge (buy-stop-limit above the range high / sell-stop-limit below the low),
SL at the opposite edge (=1R), TP=2R, GTC, tagged **🅾 STRATEGY ORB**. It fires **once per day** max, only for
breakouts by 10:30 ET, only **with** the 20-day regime. It is *not* a plain limit — a breakout is momentum
(a plain-limit/retest entry tested at **half** the expectancy; see `MNQ_DEEP_RESEARCH.md §8`).

## ⚠ If the Railway build FAILS ("Failed to build an image")
That is a build/dependency error, not the strategy code. Almost always Railway can't find a `requirements.txt`
at the folder it's building. Fixes:
1. **Now included:** this folder ships its own `requirements.txt` + `Procfile`, so it deploys **standalone**
   too — set the Railway service **Root Directory = `strategy_orb`** (Settings → Source) and it will build.
   With a subfolder root, use start command **`gunicorn orb_live:app --workers 1 --bind 0.0.0.0:$PORT`**
   (no `--chdir`).
2. If you build from the **repo root** instead, keep Root Directory blank and make sure the **root**
   `requirements.txt` lists `flask gunicorn pandas numpy requests`.
3. Click **View logs → Build** on the failed deploy to see the exact line; paste it to me if it still fails.
4. The service is self-contained — it no longer needs `live_emit`/`detcore` from the parent repo (it has
   built-in sizing + Telegram fallbacks), so a standalone repo of just this folder works.

## Gate 0 (same discipline as everything else)
Prove **≥ +0.10R over 30–50 live/paper trades** before sizing it up. At ~116 trades/yr (bias-aligned) that's a
few months. The dashboard's realized performance is your scoreboard.
