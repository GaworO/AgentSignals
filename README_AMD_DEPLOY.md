# strategy-amd — deploy guide (standalone service, A/B untouched)

A new Railway service in the **exact mold of strategy-f / strategy-c**: its own
process, own volume, own buffer, own Telegram webhook, own TradersPost strategy.
It runs the **validated** research engine unchanged (`ict/` + `config/amd.yaml`),
so the live forward test is a faithful test of the backtest
(+0.44R, PF 2.39, t=3.21, ~21 trades/yr, 5/5 positive years, maxDD −5.2R).

**Dry-run verified:** replayed against real bars it reproduced the 2026-05-04
backtest trade to the tick — SHORT entry 27758.75 / SL 27793.25 / flatten **+0.44R**
(backtest: entry 27758.8 / SL 27793.2 / +0.44R).

---

## What's in this drop

| file | purpose |
|---|---|
| `amd_live.py` | the service (detector + alert + Gate-0 journal + Flask app) |
| `ict/` | the bundled research engine (data, liquidity, fvg, ifvg, cisd, strategy…) |
| `config/amd.yaml` | the frozen validated config (NY-PM + short + non-equal + accumulation gate 1.2) |
| `requirements.txt` | adds `pyyaml`, `tzdata` (needed by the engine) |
| `agent.py` | **one added block** (~line 373): forwards each bar to the AMD service — identical to the F/C blocks, fire-and-forget, outside the lock, env-gated. Does nothing until `STRAT_AMD_FORWARD_URL` is set. |
| `dashboard.py` | AMD strategy card (How / Dashboard / Gate 0) + a new **Compare** view (the cross-strategy scorecard). Mirrors the F/ORB pattern. |
| `allview.py` | AMD folded into **/all/trades** and **/all/candidates** so it appears in the joined tables alongside A/B/C/F/ORB. |

Everything else in the repo is unchanged. A/B, F, C, ORB are not imported or touched.

---

## 1. New Railway service

- New service **from the same repo**, name `strategy-amd`.
- **Start command:** `gunicorn amd_live:app`
- Attach a **volume** mounted at `/home/claude` (so the buffer + journal persist).

## 2. Env vars (on the strategy-amd service)

| var | value | note |
|---|---|---|
| `AMD_BUF` | `/home/claude/buffer_AMD.csv` | own bar buffer on the volume |
| `AMD_BUFFER_BARS` | `45000` | ~30 trading days — the accumulation gate needs 20d lookback |
| `SENT_AMD_FILE` | `/home/claude/sent_signals_AMD.json` | dedup |
| `AMD_TRADES_FILE` | `/home/claude/amd_trades.json` | Gate-0 journal |
| `STRAT_AMD_ENABLED` | *(leave unset at first)* | unset = detect + journal only, **sends nothing** |
| `STRAT_AMD_WEBHOOK` | your Telegram `/webhook` | set only when going live |
| `EXEC_WEBHOOK_AMD` | TradersPost relay (**separate** strategy `STRATEGY_AMD`) | set only when auto-staging |
| `EXEC_TICKER_AMD` | `MNQ1!` | |
| `ACCOUNT` / `RISK_PCT` | `100000` / `0.5` | for the $ counter |

## 3. Point the bar feed at it (on the AGENT service)

Add **one env var to the agent** (the forward block is already in `agent.py`):

```
STRAT_AMD_FORWARD_URL = https://strategy-amd-production.up.railway.app/bars   # fan bars to AMD
STRAT_AMD_URL         = https://strategy-amd-production.up.railway.app        # so the dashboard + /all views can read AMD
```

Now every closed 1-min bar the agent receives is fan-forwarded to strategy-amd,
exactly like F and C. A/B detection is unaffected (fire-and-forget, outside the lock).
`STRAT_AMD_URL` lets the unified dashboard show the AMD card and fold AMD into
**All trades** / **All candidates**.

## 4. Warm-up (important)

The accumulation gate compares each morning's range to its **trailing 20-day
average**, so the service can't fire until its buffer holds ~20 trading days.
Two options:
- **Seed it** (recommended): upload ~30 trading days of recent MNQ 1-min bars to
  `AMD_BUF` on first boot → it can detect immediately. (Same `ts_event,open,high,low,close,volume` format.)
- **Or let it warm up**: no signals for the first ~4 weeks while the buffer fills.

---

## Rollout (safe, staged)

1. **Silent** (`STRAT_AMD_ENABLED` unset): it detects, journals, and shows
   everything on the service's home page + `/stats`, but sends no Telegram/TradersPost.
   Watch it for a few real setups; confirm alerts match what you'd expect.
2. **Alerts on**: set `STRAT_AMD_ENABLED=1` + `STRAT_AMD_WEBHOOK`. Telegram only.
3. **Auto-stage** (optional): add `EXEC_WEBHOOK_AMD` (its own TradersPost strategy).

## Endpoints
`/` Gate-0 page · `/stats` JSON · `/bars` POST intake · `/poll` manual poll · `/health`

## Gate 0 — the honest caveat
Gate 0 = prove ≥ +0.15R live. **AMD is ~21 trades/year**, so 30–50 live trades is
**~1.5–2.5 years** of data. This is the price of a low-frequency edge: live
confirmation is slow. Options: (a) accept the backtest + warm-started walk-forward
as your evidence and run it small, or (b) run it silent/paper and let the journal
accumulate while you trade your higher-frequency edges. Do **not** loosen the config
to force more trades — that reintroduces the dead 4-confirm model (see AMD_STRATEGY.md).
