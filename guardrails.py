#!/usr/bin/env python3
"""
guardrails.py — MFF-eval-safe auto-execution gate (isolated add-on, same pattern as shadow.py).

Sits BETWEEN detection and execution. It never touches the strategy; it only decides whether a
fired A/B signal is allowed to be STAGED for auto-submit. Everything defaults conservative and the
gate is FAIL-CLOSED: any internal error => block the order (miss a trade, never trade unguarded).

Purpose (Aleks, Jul-2026): auto-execute the resting-limit edge WITHOUT breaking the live MFF Pro-100k
evaluation. Current account $99,887 · $6,000 target ($106k) · $3,000 EOD-trailing max-loss · no MFF
daily-loss limit (so OUR count-based stops are the only floor) · 50% consistency rule in eval ·
min 2 trading days · own-account automation allowed.

The eval-killer is the $3k trailing drawdown. Because the agent has NO broker fill-feedback, the
hard protection is COUNT-BASED (one position, max trades/day, halt after N losses) — those bound the
worst realistic day to ~-2R regardless of what equity the model thinks it has. The $-based
DD-proximity guard runs on a MODELED equity that you keep honest with /guard/sync?equity=<real MFF
balance> (5 seconds, do it after each session). Reuses shadow.py's resolver for outcomes so this and
the shadow tab always agree.

Wire (agent.py):  import guardrails  (top, next to `import shadow`)
                  guardrails.register(app)  (bottom, next to `shadow.register(app)`)
                  gate the _exec_order call — see GUARDRAILS_PATCH.md.

Env (defaults tuned for the Pro-100k eval at $99,887):
  AUTO_SUBMIT=0            master switch (agent checks it; 1 = actually stage)
  AUTO_SESSIONS=NYAM,NYPM,PM_AH   only auto-fire these (his green sessions; London/Asia/PREM excluded)
  MAX_TRADES_DAY=3        stop over-trading a chop day
  DAY_LOSS_N=2           halt+latch after N losing SENT trades today  (primary floor guard)
  DAY_LOSS_USD=1000      halt+latch after -$ modeled loss today       (secondary)
  DAY_TARGET_USD=1500    profit-lock: stop for the day after +$ (keeps best day < 50% of $6k => consistency-safe)
  DD_FLOOR=97000         MFF trailing max-loss level (READ IT off MFF; update as it trails up)
  DD_BUFFER=800          halt+latch when modeled equity is within $ of the floor
  RAMP_TRADES=3          first N SENT trades run at qty=1 (prove routing) then normal size
  START_EQUITY=99887     modeled equity seed until first /guard/sync
  STALE_MIN=20           block if the bar feed is older than this (market hours)
  DATA_DIR=.             persist dir (shadow_log.json lives here too)

Hardening (2026-07-19 review):
  NEWS_GUARD=1           block auto sends inside ±30min of high-impact events (agent passes the flag)
  MIN_SL_PTS=5           skip degenerate tight-SL setups (absurd qty + slippage beyond -1R)
  DD_TRAIL_USD=3000      auto-trailing floor: max(DD_FLOOR, highest synced equity - this)
  DD_FLOOR_CAP=0         optional cap where the trail locks (MFF locks at start balance)
  GUARD_FLATTEN=1        loss/DD/manual latches also send exit+cancel to EXEC_WEBHOOK (stop the bleeding)
  EOD_FLATTEN_ET=16:04   daily flatten+cancel (MFF auto-liquidates 16:10 ET; holidays are manual!). '0'=off
  GUARD_LAST_ENTRY_ET=15:30  no new auto sends at/after this ET time (late entries meet the 16:10 forced flat)
  GUARD_SYNC_MAX_H=0     >0 = AUTO refuses to trade if real equity wasn't synced within N hours
  GUARD_TOKEN=           set -> /guard/sync|kill|mode require ?t=<token> (open /guard?t=... for buttons)
  SKIP_SESSIONS default is now LO,ASIA,PREM,NYL (was LO,ASIA — PREM/NYL used to auto-fire)
  state writes are atomic; a CORRUPT state file now latches HARD (was: silently reset to defaults)
  agent.py: EXEC_TIF=day default (was gtc), EXEC_MAX_QTY default 15, exec result checked before
  booking 'sent', orphan-limit sweep cancels broker orders the model wrote off as no_fill.
"""
import os, json, time, datetime as dt
try:
    import shadow                                   # reuse its resolver + ledger (same DATA_DIR)
except Exception:
    shadow = None
try:
    import requests                                 # only for the optional health-transition alert POST
except Exception:
    requests = None
try:
    from zoneinfo import ZoneInfo; _NY = ZoneInfo('America/New_York')
except Exception:
    _NY = None

DATA_DIR = os.environ.get('DATA_DIR', '.')
GLOG     = os.path.join(DATA_DIR, 'guard_log.json')    # every decision (sent/blocked) — the /guard book
GSTATE   = os.path.join(DATA_DIR, 'guard_state.json')  # kill-latch, ramp counter, synced equity
RISK_DOLLAR = 500.0                                     # 1R at 0.5%/$100k (display only)

def _env(k, d):        return os.environ.get(k, d)
def _envf(k, d):       return float(os.environ.get(k, str(d)))
def _envi(k, d):       return int(float(os.environ.get(k, str(d))))

def _now_ms():         return int(time.time() * 1000)
def _et(ms):
    d = dt.datetime.fromtimestamp(ms/1000.0, tz=dt.timezone.utc)
    return d.astimezone(_NY) if _NY else d
def _today():
    """TRADING day, not calendar day: from 18:00 ET the date rolls to the next day (Globex/MFF
    convention). Keeps the day counters/dedup/latches aligned with the real 18:00->16:10 trading
    day — before this, Sun-evening trades used Sunday's counters and midnight handed out a fresh
    allowance INSIDE the same MFF day (up to 6 trades / 4 losses per real day). Found by Aleks."""
    d = _et(_now_ms())
    if d.hour >= 18: d = d + dt.timedelta(days=1)
    return d.strftime('%Y-%m-%d')

def _sess_of(x):
    s = x.get('sess')
    if s in ('PREM','NYAM','NYL','NYPM','PM_AH','ASIA','LO'): return s
    if shadow is not None:
        try: return shadow._sess(_et(int(x.get('bos_ms') or _now_ms())))
        except Exception: pass
    return '?'

def _load(p, d):
    try: return json.load(open(p))
    except Exception: return d
def _load_failclosed(p, d):
    """Like _load, but a file that EXISTS yet won't parse is treated as corruption -> caller must
    fail CLOSED, not fall back to permissive defaults (a truncated guard_state.json used to silently
    clear the hard kill-latch and reset equity/ramp)."""
    if not os.path.exists(p): return d, False
    try: return json.load(open(p)), False
    except Exception as e:
        print('[guard] STATE CORRUPT (fail-closed):', p, e, flush=True)
        return d, True
def _save(p, x):
    """ATOMIC write (tmp + os.replace) — a crash/redeploy mid-dump can no longer truncate the
    state/log to invalid JSON (which then failed OPEN on the next _load)."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = p + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(x, f); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception as e:
        print('[guard] save err', p, e, flush=True)

def _skey(x):
    """same identity shadow.py uses, so a guard row can be joined to its shadow outcome."""
    try:
        if shadow is not None:
            return shadow._key('A/B', x.get('dir'), int(x.get('bos_ms') or _now_ms()),
                               round(float(x.get('entry')), _envi('GUARD_PRICE_DP', 2)))
    except Exception: pass
    return "%s|%s|%s" % (x.get('dir'), x.get('entry'), x.get('bos_ms'))

def _wd(x):
    """weekday of the signal (0=Mon) by ET clock."""
    try: return _et(int(x.get('bos_ms') or _now_ms())).weekday()
    except Exception: return -1

def is_duplicate(x):
    """True if an order for the SAME setup (dir + entry, to 0.1) was already SENT/staged today.
    Kills confluence re-emits, re-detection, and restart double-fires -> one setup = one order + one alert."""
    try:
        dp = _envi('GUARD_PRICE_DP', 1)     # price decimals for dedup identity. MNQ=1 (default);
        # FX MUST override (EURUSD=5, USDJPY=3): at dp=1 every EURUSD entry rounds to the same
        # bucket and the whole day after trade #1 would be blocked as 'duplicate'.
        d = x.get('dir'); e = round(float(x.get('entry')), dp); day = _today()
        for g in _load(GLOG, []):
            if g.get('date') == day and g.get('decision') in ('sent', 'manual'):
                if g.get('dir') == d and round(float(g.get('entry') or 0), dp) == e:
                    return True
    except Exception as ex:
        print('[guard] is_duplicate err', ex, flush=True)
    return False

# ---------- state ----------
_DEF_STATE = {'kill': False, 'kill_reason': '', 'kill_day': '', 'kill_hard': False,
              'sent_total': 0, 'equity': None, 'equity_ts': 0}
def _state():
    s, corrupt = _load_failclosed(GSTATE, dict(_DEF_STATE))
    if corrupt:                                    # corrupted state -> HARD kill until a human looks
        s = dict(_DEF_STATE); s.update(kill=True, kill_hard=True, kill_reason='state_corrupt',
                                       kill_day=_today())
    if s.get('equity') is None: s['equity'] = _envf('START_EQUITY', 99887.0)
    return s
def _set_state(s): _save(GSTATE, s)

def _kill_active(s):
    if not s.get('kill'): return False
    if s.get('kill_hard'): return True                 # DD / manual: until /guard/kill?on=0
    return s.get('kill_day') == _today()               # day-based: auto-clears next day

def _latch(reason, hard=False):
    s = _state(); s['kill'] = True; s['kill_reason'] = reason; s['kill_day'] = _today(); s['kill_hard'] = hard
    _set_state(s); print('[guard] KILL LATCH', reason, 'hard=%s' % hard, flush=True)
    # a loss/DD latch means STOP THE BLEEDING, not just "no new entries": flatten + cancel at the broker
    if reason in ('day_loss_n', 'day_loss_usd', 'dd_proximity', 'dd_breached', 'target_hit_6pct', 'state_corrupt'):
        flatten_all('latch:' + reason)

def _dd_floor():
    """Effective trailing floor = max(DD_FLOOR env, highest synced equity - DD_TRAIL_USD).
    MFF's EOD-trailing floor only RISES; tracking eq_high makes the guard follow it automatically
    instead of trusting a manually-updated env var that goes stale after every green day."""
    env_floor = _envf('DD_FLOOR', 97000.0)
    try:
        hi = float(_state().get('eq_high') or 0)
        trail = hi - _envf('DD_TRAIL_USD', 3000.0)
        cap = _envf('DD_FLOOR_CAP', 0.0)              # MFF locks the trail at start balance; cap it if set
        if cap: trail = min(trail, cap)
        return max(env_floor, trail)
    except Exception:
        return env_floor

def flatten_all(reason):
    """Best-effort: close the open position ('exit') and cancel resting orders ('cancel') at
    TradersPost for EXEC_TICKER. Fired on loss/DD latches and by the EOD flatten. Never raises.
    Off with GUARD_FLATTEN=0."""
    try:
        if _env('GUARD_FLATTEN', '1') != '1': return False
        if os.environ.get('EXEC_FX') == '1':               # FX services: MetaApi close-symbol instead of TradersPost
            try:
                import exec_fx
                return exec_fx.flatten(reason)
            except Exception as e:
                print('[guard] fx flatten err', e, flush=True); return False
        url = os.environ.get('EXEC_WEBHOOK', '')
        if not url or requests is None: return False
        tick = os.environ.get('EXEC_TICKER', os.environ.get('CONTRACT', 'MNQ1!'))
        ok = []
        for action in ('exit', 'cancel'):
            try:
                r = requests.post(url, json={'ticker': tick, 'action': action}, timeout=10)
                ok.append('%s:%s' % (action, getattr(r, 'status_code', '?')))
            except Exception as e:
                ok.append('%s:err' % action); print('[guard] flatten %s err' % action, e, flush=True)
        print('[guard] FLATTEN (%s) ->' % reason, ' '.join(ok), flush=True)
        aurl = os.environ.get('GUARD_ALERT_URL') or os.environ.get('WEBHOOK_URL')
        if aurl:
            msg = '\U0001f9f9 FLATTEN+CANCEL sent (%s) — %s' % (reason, ' '.join(ok))
            try: requests.post(aurl, json={'text': msg, 'raw': msg}, timeout=6)
            except Exception: pass
        return True
    except Exception as e:
        print('[guard] flatten_all err', e, flush=True); return False

def sweep_orphans():
    """Cancel broker-side orphan LIMIT orders: guard-book trades whose modeled outcome says the limit
    never filled (no_fill / missed / expired). Without this the resting order lives on at the broker
    after the guard has already freed the one-position slot -> a later fill silently stacks positions.
    Only sweeps when the modeled book shows NO open position ('cancel' cancels ALL open orders for the
    ticker — must not strip the bracket off a live trade). Called from agent.py after shadow.refresh."""
    try:
        d = _day_stats()
        if d['openpos']: return 0                     # never cancel while a bracket protects a position
        s = _state(); swept = s.get('swept') or {}
        sm = _shadow_by_key(); n = 0
        for g in _load(GLOG, []):
            if g.get('decision') != 'sent': continue
            k = g.get('key')
            if not k or k in swept: continue
            oc = sm.get(k, {}).get('outcome')
            if oc in ('no_fill', 'missed', 'expired'):
                if flatten_cancel_only():
                    swept[k] = _now_ms(); n += 1     # only mark on success -> a down relay is retried next bar
                elif not os.environ.get('EXEC_WEBHOOK') or _env('GUARD_FLATTEN', '1') != '1':
                    swept[k] = _now_ms()             # nothing to cancel with -> don't retry forever
        if n or len(swept) > 200:
            s['swept'] = dict(sorted(swept.items(), key=lambda kv: -kv[1])[:200]); _set_state(s)
        return n
    except Exception as e:
        print('[guard] sweep err', e, flush=True); return 0

def flatten_cancel_only():
    """Cancel resting orders only (no exit) — used by the orphan sweep."""
    try:
        if os.environ.get('EXEC_FX') == '1': return False   # MT5 entry orders self-expire (FX_ENTRY_TTL_MIN) — no sweep needed
        url = os.environ.get('EXEC_WEBHOOK', '')
        if not url or requests is None or _env('GUARD_FLATTEN', '1') != '1': return False
        tick = os.environ.get('EXEC_TICKER', os.environ.get('CONTRACT', 'MNQ1!'))
        r = requests.post(url, json={'ticker': tick, 'action': 'cancel'}, timeout=10)
        print('[guard] ORPHAN CANCEL ->', getattr(r, 'status_code', '?'), flush=True)
        return True
    except Exception as e:
        print('[guard] cancel err', e, flush=True); return False

def daily_digest_check():
    """Proof-of-life: ONE Telegram line every day at DAILY_DIGEST_ET (def 08:45 ET) with mode/health/
    equity/buffer. The point is the ABSENCE signal: no morning digest = the process is dead or Telegram
    is broken — either way, look at Railway. In-process alerts cannot report their own death; this makes
    silence itself the alarm (pair with HEALTHCHECK_URL + an uptime monitor on /guard/health).
    DAILY_DIGEST_ET=0 disables."""
    try:
        t = _env('DAILY_DIGEST_ET', '08:45').strip()
        if not t or t == '0': return False
        hh, mm = [int(v) for v in t.split(':')]
        now = _et(_now_ms())
        if (now.hour * 60 + now.minute) < (hh * 60 + mm): return False
        s = _state()
        if s.get('digest_day') == _today(): return False
        s['digest_day'] = _today(); _set_state(s)
        url = os.environ.get('GUARD_ALERT_URL') or os.environ.get('WEBHOOK_URL')
        if not url or requests is None: return False
        try:
            h = health(); ep = eval_progress(); d = _day_stats()
            msg = ('☀️ AUTO check-in %s — mode %s · health %s · eq $%s (buffer $%s, floor $%s) · '
                   'sent today %d · %s\nNo check-in tomorrow at %s ET = system is DOWN (check Railway/monitor).'
                   % (_today(), exec_mode(), h.get('status', '?'), ep['equity'], ep['buffer'], ep['floor'],
                      d['sent'], ('HALTED: ' + s.get('kill_reason', '')) if _kill_active(s) else 'armed', t))
        except Exception as e:
            msg = '☀️ AUTO check-in %s — alive, but digest build failed: %s' % (_today(), e)
        try: requests.post(url, json={'text': msg, 'raw': msg}, timeout=6)
        except Exception: pass
        return True
    except Exception as e:
        print('[guard] digest err', e, flush=True); return False

def _flatten_deadline_min(s=None):
    """Today's flatten deadline in ET minutes: the holiday early-close override (noted by the agent
    from cme_calendar) beats the normal EOD_FLATTEN_ET (16:04). None = flatten disabled."""
    try:
        s = s or _state(); ec = s.get('early_close')
        if ec and ec.get('day') == _today(): return int(ec['min_et'])
        t = _env('EOD_FLATTEN_ET', '16:04').strip()
        if not t or t == '0': return None
        hh, mm = [int(v) for v in t.split(':')]
        return hh * 60 + mm
    except Exception:
        return 16 * 60 + 4

def note_early_close(min_et):
    """Agent heartbeat calls this on holiday early-close days (from cme_calendar.EARLY_CLOSE):
    flatten + entry-cutoff move up to the early halt instead of 16:04."""
    try:
        s = _state(); s['early_close'] = {'day': _today(), 'min_et': int(min_et)}; _set_state(s)
    except Exception as e:
        print('[guard] early close note err', e, flush=True)

def eod_flatten_check(market_open=None):
    """If EOD_FLATTEN_ET='HH:MM' is set, flatten+cancel once per day at/after that ET time.
    Default UNSET (off): whether to force-flatten daily is a strategy decision — the backtest holds
    up to 48h — but if MFF force-liquidates (verify on the dashboard!) set this a few minutes before.
    Called from the agent heartbeat loop (~5 min resolution)."""
    try:
        # MFF auto-liquidates 16:10 ET on normal days but NOT on holidays — the deadline below is
        # 16:04 normally and the early-close override (set via note_early_close from cme_calendar)
        # on holiday half-days, so the flatten fires while orders can still execute.
        dl = _flatten_deadline_min()
        if dl is None: return False
        now = _et(_now_ms())
        nm = now.hour * 60 + now.minute
        # fire ONLY inside [deadline, 18:00 ET). After the 18:00 reopen a NEW trading day is live —
        # a late-firing flatten there would close a legitimate overnight position (v27.0 bug).
        if not (dl <= nm < 18 * 60): return False
        s = _state()
        if s.get('eod_flat_day') == _today(): return False
        s['eod_flat_day'] = _today(); _set_state(s)
        flatten_all('eod_%02d:%02d' % (dl // 60, dl % 60))
        return True
    except Exception as e:
        print('[guard] eod flatten err', e, flush=True); return False

# ---------- mode (auto/manual/off) + stale-data abort ----------
def stale_abort(feed_age_min):
    """Abort the send when the bar feed is older than STALE_MIN (old data => garbage setup).
    Non-latching: auto-resumes the moment fresh bars arrive. Unknown age => abort (fail-closed)."""
    try:
        if feed_age_min is None: return True                       # unknown age -> abort
        return float(feed_age_min) > _envf('STALE_MIN', 20)
    except Exception:
        return True

def exec_mode():
    """'auto' = guarded full-auto · 'manual' = your tap-to-approve semi-auto · 'off' = alerts only.
    Runtime state (flip via /guard/mode) beats EXEC_MODE env beats AUTO_SUBMIT."""
    s = _state()
    m = (s.get('mode') or os.environ.get('EXEC_MODE', '')).strip().lower()
    if m in ('auto', 'manual', 'off'): return m
    return 'auto' if os.environ.get('AUTO_SUBMIT', '0') == '1' else 'manual'

def set_mode(m):
    m = (m or '').strip().lower()
    if m not in ('auto', 'manual', 'off'): return False
    s = _state(); s['mode'] = m; _set_state(s); print('[guard] MODE ->', m, flush=True); return True

def _exec_ready():
    """True when an execution path is configured: TradersPost webhook OR the MetaApi FX adapter."""
    if os.environ.get('EXEC_WEBHOOK'): return True
    if os.environ.get('EXEC_FX') != '1': return False
    if os.environ.get('FX_BRIDGE_URL'): return True                    # cTrader bridge route (free)
    return bool(os.environ.get('METAAPI_TOKEN')) and bool(os.environ.get('METAAPI_ACCOUNT_ID'))

def is_live():
    """True only if AUTO will REALLY place orders: mode=auto AND webhook set AND not halted.
    /status uses this so it can't report 'live' while a HALT latch is blocking every order."""
    try:
        return exec_mode() == 'auto' and _exec_ready() and not _kill_active(_state())
    except Exception:
        return False

def manual_ok(x, feed_age_min=None, market_open=None):
    """Light gate for MANUAL mode: the non-negotiables (dup, Monday, stale, market, hard kill).
    Trade selection is yours (approve buttons) — the count/loss/DD guards don't block here."""
    try:
        beat(feed_age_min, market_open)              # stamp liveness + last feed age for /guard/health
        s = _state()
        if _kill_active(s) and s.get('kill_hard'): return (False, 'killed:' + (s.get('kill_reason') or '?'))
        ep = eval_progress()
        if ep['passed']:                           return (False, 'target_hit')
        if ep['breached']:                         return (False, 'dd_breached')
        if is_duplicate(x):                        return (False, 'duplicate')
        mm = _env('MONDAY_MODE', 'nyam').lower()
        if _wd(x) == 0:
            if mm == 'skip':                       return (False, 'monday_skip')
            if mm == 'nyam' and _sess_of(x) == 'PREM': return (False, 'monday_prem')
            if mm == 'quarter': x['_mon_quarter'] = True
        if market_open is False:                   return (False, 'market_closed')
        if stale_abort(feed_age_min):              return (False, 'stale_data')
        return (True, 'ok')
    except Exception as e:
        print('[guard] manual_ok EXC (fail-closed):', e, flush=True); return (False, 'guard_error')

# ---------- eval progress counter (where the account stands) ----------
def eval_progress():
    """Where the MFF eval stands. Uses synced real equity + today's modeled net.
    passed = hit the profit target (+6%); breached = broke the trailing drawdown floor."""
    try:
        s = _state(); d = _day_stats()
        start  = _envf('START_BALANCE', 100000.0)                 # eval starting balance
        target = _envf('TARGET_BALANCE', start + 6000.0)          # +$6,000 = +6%
        floor  = _dd_floor()                                      # auto-trails with synced equity highs
        eq     = float(s.get('equity', _envf('START_EQUITY', 99887.0))) + d['net']   # modeled; /guard/sync keeps it real
        return dict(equity=round(eq), start=round(start), target=round(target), floor=round(floor),
                    pnl=round(eq - start), to_target=round(target - eq), buffer=round(eq - floor),
                    pct=round(100.0 * (eq - start) / 6000.0, 1),      # % of the $6k target reached
                    passed=eq >= target, breached=eq <= floor)
    except Exception as e:
        print('[guard] eval_progress err', e, flush=True)
        return dict(equity=0, start=100000, target=106000, floor=97000, pnl=0, to_target=6000,
                    buffer=0, pct=0.0, passed=False, breached=False)

# ---------- today's SENT book (from guard_log) + outcomes (from shadow) ----------
def _shadow_by_key():
    if shadow is None: return {}
    try:
        log = shadow.refresh()                          # resolve against live bars
        return {t.get('key'): t for t in log if t.get('key')}
    except Exception as e:
        print('[guard] shadow.refresh err', e, flush=True); return {}

def _actualize(g, sh):
    """Join a guard row with its shadow outcome, repriced at the ACTUAL sent quantity.
    The shadow model prices outcomes at risk-model size (~$500/R); during the ramp the real
    order is 1 contract — a real -$17 loss displayed (and counted!) as -$569 skews the day-loss
    counter and the modeled equity. qty x stop x POINT_VALUE is the real risk. Found by Aleks."""
    if g.get('ext_outcome'):                      # external (C, ...) rows carry their OWN resolution —
        return {**g, 'outcome': g['ext_outcome'],  # the A/B shadow knows nothing about them
                'net': g.get('ext_net'), 'R': g.get('R')}
    oc = sh.get('outcome', 'open')
    out = {**g, 'outcome': oc, 'R': sh.get('R'), 'net': sh.get('net')}
    try:
        q = g.get('qty')
        if q and oc in ('win', 'loss', 'timeout') and g.get('entry') is not None and g.get('sl') is not None:
            pv = _envf('POINT_VALUE', 2.0)
            slp = abs(float(g['entry']) - float(g['sl']))
            risk = float(q) * slp * pv
            gross = {'win': 2.0, 'loss': -1.0, 'timeout': 0.0}[oc]
            cost = float(q) * (0.62 * 2 + 0.25 * pv * 2)
            net = gross * risk - cost
            if risk > 0:
                out['net'] = round(net); out['R'] = round(net / risk, 3)
    except Exception:
        pass
    return out

def _today_sent():
    glog = _load(GLOG, []); sm = _shadow_by_key(); day = _today(); out = []
    for g in glog:
        if g.get('decision') != 'sent' or g.get('date') != day: continue
        out.append(_actualize(g, sm.get(g.get('key'), {})))
    return out

def _day_stats():
    sent = _today_sent()
    # v27.2d: rows YOU canceled at the broker (ext_outcome='canceled' via /guard/cancel) consumed no
    # risk — they don't count toward MAX_TRADES_DAY. Dedup + one-position still bound send churn.
    n_trades = sum(1 for t in sent if t.get('outcome') != 'canceled')
    losses = sum(1 for t in sent if t.get('outcome') == 'loss')
    net = sum((t.get('net') or 0) for t in sent if t.get('outcome') in ('win', 'loss', 'timeout'))
    # openpos: resting OR running = a live commitment. External rows (C, ... via /guard/extlog) have
    # no shadow resolution — they hold the slot for EXT_OPEN_H hours (fill window), then age out
    # unless the satellite POSTs an outcome update.
    exth = _envf('EXT_OPEN_H', 4) * 3600000
    openpos = False
    for t in sent:
        if t.get('outcome') != 'open': continue
        if t.get('strat', 'A/B') != 'A/B' and not t.get('ext_outcome'):
            if (_now_ms() - int(t.get('ts') or 0)) < exth: openpos = True
        else:
            openpos = True
    return dict(sent=n_trades, losses=losses, net=net, openpos=openpos)

# ---------- the gate ----------
def guard_ok(x, feed_age_min=None, market_open=None, news_hard=None, cal_age_h=None):
    """(True,'ok') to allow staging, else (False,'<reason>'). FAIL-CLOSED on any error."""
    try:
        beat(feed_age_min, market_open)              # stamp liveness + last feed age for /guard/health
        s = _state()
        if _kill_active(s):                     return (False, 'killed:' + (s.get('kill_reason') or '?'))
        if market_open is False:                return (False, 'market_closed')
        if stale_abort(feed_age_min):           return (False, 'stale_data')   # abort on old bars (non-latching, auto-resumes)
        if _env('NEWS_GUARD', '1') == '1':
            if news_hard:
                return (False, 'news_window')   # ±30min high-impact (flags_for) — auto never trades through CPI/FOMC
            if _env('NEWS_STRICT', '1') == '1': # FAIL-CLOSED calendar: can't verify news = don't trade.
                if cal_age_h is None or float(cal_age_h) > _envf('NEWS_MAX_AGE_H', 24):
                    return (False, 'news_cal_stale')   # ForexFactory unreachable/stale -> no unattended sends
        try:                                    # MFF liquidates 16:10 ET (early-close days much earlier);
            now = _et(_now_ms())                # placing trades late risks forced-flat entries + disqualification
            let = _env('GUARD_LAST_ENTRY_ET', '').strip()
            if let and let != '0':
                lh, lm = [int(v) for v in let.split(':')]
                cutoff = lh * 60 + lm
            else:
                dl = _flatten_deadline_min(s)   # 16:04 normally; holiday early-close deadline when noted
                cutoff = (dl - _envi('GUARD_ENTRY_MARGIN_MIN', 35)) if dl else None
            # late-day applies ONLY between the cutoff and the 18:00 ET Globex reopen — after 18:00
            # a NEW MFF trading day starts and overnight entries are legitimate again.
            # (v27.0 bug: no upper bound -> the whole ASIA evening was blocked as 'late_day'.)
            nm = now.hour * 60 + now.minute
            if cutoff is not None and cutoff <= nm < 18 * 60:
                return (False, 'late_day')
        except Exception:
            return (False, 'late_day')          # unparseable cutoff -> fail closed
        try:                                    # degenerate tight stop -> absurd qty + slippage beyond -1R
            if abs(float(x.get('entry')) - float(x.get('SL'))) < _envf('MIN_SL_PTS', 5.0):
                return (False, 'sl_too_tight')
        except Exception:
            return (False, 'bad_levels')
        mah = _envf('GUARD_SYNC_MAX_H', 0)      # >0 = in AUTO require a real-equity sync fresher than N hours
        if mah > 0:
            et_ts = s.get('equity_ts', 0)
            if not et_ts or (_now_ms() - int(et_ts)) > mah * 3600000:
                return (False, 'equity_stale')
        ep = eval_progress()                                          # eval over? stop trading it
        if ep['passed']:   _latch('target_hit_6pct', hard=True);  return (False, 'target_hit')
        if ep['breached']: _latch('dd_breached', hard=True);      return (False, 'dd_breached')
        if is_duplicate(x):                     return (False, 'duplicate')   # one setup = one order (silent, no dup alert)
        if _env('GUARD_SKIP_DIB', '0') == '1' and 'DIB' in str(x.get('cat', '')):
            return (False, 'class_b_dib')   # class-B tier: excluded from the audited premium tier (T4);
                                            # went 0W/4L in the 2026 Feb-Jun forward sim. Opt-in gate.
        sess = _sess_of(x)
        # default now matches the documented intent (green sessions = NYAM/NYPM/PM_AH):
        # PREM + NYL were auto-firing on the old 'LO,ASIA' default. Env still overrides.
        skip = [w.strip() for w in _env('SKIP_SESSIONS', 'LO,ASIA,PREM,NYL').split(',') if w.strip()]
        if sess in skip:                        return (False, 'session:' + sess)   # London/Asia (+NYL) excluded by firing time
        mm = _env('MONDAY_MODE', 'nyam').lower()                              # nyam=Monday starts at NYAM (skip PREM) | skip | quarter | full
        if _wd(x) == 0:
            if mm == 'skip':                    return (False, 'monday_skip')
            if mm == 'nyam' and sess == 'PREM': return (False, 'monday_prem')  # keep Monday but ignore PREM — start at NYAM open
            if mm == 'quarter': x['_mon_quarter'] = True

        d = _day_stats()
        if d['openpos']:                        return (False, 'position_open')     # THE anti-stack rule
        pb = _peer_busy()                                                           # v27.2: one position across ALL
        if pb:                                  return (False, pb)                   # services sharing this broker acct
        if d['sent']   >= _envi('MAX_TRADES_DAY', 3):   return (False, 'max_trades_day')
        if d['losses'] >= _envi('DAY_LOSS_N', 2):
            _latch('day_loss_n');               return (False, 'day_loss_n')        # primary floor guard
        if d['net']    <= -_envf('DAY_LOSS_USD', 1000):
            _latch('day_loss_usd');             return (False, 'day_loss_usd')
        if d['net']    >=  _envf('DAY_TARGET_USD', 1500):
            _latch('profit_lock');              return (False, 'profit_lock')       # consistency-safe green stop

        eq = s.get('equity', _envf('START_EQUITY', 99887.0)) + d['net']             # modeled; keep honest via /guard/sync
        if (eq - _dd_floor()) < _envf('DD_BUFFER', 800):
            _latch('dd_proximity', hard=True);  return (False, 'dd_proximity')
        return (True, 'ok')
    except Exception as e:
        print('[guard] guard_ok EXC (fail-closed, blocking):', e, flush=True)
        return (False, 'guard_error')

_PEER_CACHE = {}

def _peer_busy():
    """v27.2 — PEER_GUARD_URL: comma-separated base URLs of OTHER guard services sharing the SAME
    broker account (e.g. forex-eur <-> forex-jpy on one FTMO login). Before any send, each peer's
    /guard/data is asked whether it holds a live commitment (resting order or open position).
    Returns a block reason or None. FAIL-CLOSED: an unreachable peer blocks the send — a peer you
    cannot see may be holding a position, and a double position on one FX account is exactly the
    over-risk this exists to prevent. Cached PEER_CACHE_S (default 20s) per process."""
    urls = [u.strip() for u in _env('PEER_GUARD_URL', '').split(',') if u.strip()]
    if not urls: return None
    if requests is None: return 'peer_unreachable'
    now = _now_ms()
    if _PEER_CACHE.get('t', 0) + _envf('PEER_CACHE_S', 20) * 1000 > now:
        return _PEER_CACHE.get('v')
    v = None
    for u in urls:
        try:
            j = requests.get(u.rstrip('/') + '/guard/data', timeout=6).json()
            if j.get('openpos'):
                v = 'peer_open'; break
        except Exception as e:
            print('[guard] peer %s unreachable: %s (fail-closed)' % (u, e), flush=True)
            v = 'peer_unreachable'; break
    _PEER_CACHE.update(t=now, v=v)
    return v

def ramp_qty(x):
    """First RAMP_TRADES sent trades run at 1 contract to prove routing; then normal size.
    Sets x['_exec_qty_override'] (honored by the _exec_order 1-line patch). Returns the override or None."""
    try:
        if _state().get('sent_total', 0) < _envi('RAMP_TRADES', 3):
            x['_exec_qty_override'] = 1; return 1
    except Exception as e:
        print('[guard] ramp err', e, flush=True)
    return None

def note(x, decision, reason=''):
    """Record the gate decision into the /guard book. Call AFTER staging (decision='sent') or on block."""
    try:
        glog = _load(GLOG, []); k = _skey(x)
        if decision == 'blocked':                     # same setup re-detected each bar while fresh ->
            day = _today()                            # note each (key, reason) ONCE per day, not 15x
            for g in reversed(glog[-100:]):
                if g.get('date') != day: break
                if g.get('key') == k and g.get('decision') == 'blocked' and g.get('reason') == reason:
                    return
        glog.append(dict(key=k, strat=x.get('_strat', 'A/B'), ts=_now_ms(), bar_ms=int(x.get('bos_ms') or 0), date=_today(),
                         et=_et(_now_ms()).strftime('%Y-%m-%d %H:%M'),
                         sess=_sess_of(x), dir=x.get('dir'), entry=x.get('entry'), sl=x.get('SL'),
                         tp=x.get('TP'), qty=(x.get('_sent_qty') if x.get('_sent_qty') is not None
                                              else x.get('_exec_qty_override')), decision=decision, reason=reason))
        _save(GLOG, glog)
        if decision in ('sent', 'manual'):
            s = _state(); s['sent_total'] = int(s.get('sent_total', 0)) + 1; _set_state(s)
            _trade_alert(x, decision)                      # 🟢 push a Telegram line the moment auto places it
    except Exception as e:
        print('[guard] note err', e, flush=True)

def _trade_alert(x, decision):
    """One Telegram line when AUTO stages a trade (so you know the second it fires, not just via email).
    Off with GUARD_TRADE_ALERTS=0. Reuses GUARD_ALERT_URL, else WEBHOOK_URL (the same relay the agent uses)."""
    try:
        if _env('GUARD_TRADE_ALERTS', '1') != '1': return
        url = os.environ.get('GUARD_ALERT_URL') or os.environ.get('WEBHOOK_URL')
        if not url or requests is None: return
        q = x.get('_sent_qty') if x.get('_sent_qty') is not None else x.get('_exec_qty_override')
        qtxt = ('%s×' % q) if q else 'risk-size'
        tag = 'AUTO SENT' if decision == 'sent' else 'ARMED (manual)'
        msg = ('\U0001f7e2 %s · %s %s %s @ %s · SL %s / TP %s'
               % (tag, _sess_of(x), x.get('dir'), qtxt, x.get('entry'), x.get('SL'), x.get('TP')))
        if x.get('_alert_txt'): msg += '\n' + str(x['_alert_txt'])   # v27.1: full setup summary rides on the FIRED message
        try: requests.post(url, json={'text': msg, 'raw': msg}, timeout=6)
        except Exception: pass
    except Exception as e:
        print('[guard] trade alert err', e, flush=True)

# ---------- auto-executor HEALTH (is it live, armed, and unbroken?) ----------
def beat(feed_age_min=None, market_open=None):
    """Liveness stamp + the last feed age the pipeline actually saw. Called on every guard_ok/manual_ok,
    and (optionally, for tick-level coverage) once per detect loop via guardrails.beat() in agent.py."""
    try:
        s = _state(); s['last_seen_ms'] = _now_ms()
        if feed_age_min is not None:
            try: s['feed_age_min'] = round(float(feed_age_min), 1); s['feed_ts_ms'] = _now_ms()
            except Exception: pass
        if market_open is not None: s['market_open'] = bool(market_open)
        _set_state(s)
    except Exception as e:
        print('[guard] beat err', e, flush=True)

def _state_writable():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        p = os.path.join(DATA_DIR, '.guard_probe')
        open(p, 'w').write('1'); os.remove(p); return True
    except Exception:
        return False

def _mins_since(ms):
    try: return (_now_ms() - int(ms or 0)) / 60000.0
    except Exception: return None

def health(feed_age_min=None, market_open=None):
    """Structured verdict on the AUTO executor. status = ok | warn | critical | paused(=mode!=auto).
    Catches the SILENT failures: armed-but-no-webhook, halted, DD_FLOOR unset, feed/equity stale,
    state unwritable, gate raising, pipeline idle during RTH. HTTP maps critical->503 for a dead-man."""
    checks = []
    def add(name, level, detail): checks.append(dict(name=name, level=level, ok=(level in ('ok', 'info')), detail=detail))
    try:
        s = _state(); mode = exec_mode()
        webhook = _exec_ready()
        live = (mode == 'auto' and webhook)
        stale_min = _envf('STALE_MIN', 20)
        fa = feed_age_min; fa_src = 'live'
        if fa is None: fa = s.get('feed_age_min'); fa_src = 'last-seen'

        # 1 mode
        add('mode', 'ok' if mode == 'auto' else 'info',
            'AUTO' if mode == 'auto' else (mode.upper() + ' — auto is off by choice'))
        # 2 webhook wired (only matters in auto)
        if mode == 'auto':
            add('webhook', 'ok' if webhook else 'critical',
                'exec path configured' if webhook else 'AUTO but NO exec path (EXEC_WEBHOOK / EXEC_FX+METAAPI_*) — armed yet cannot place orders')
        # 3 halt / kill latch
        if _kill_active(s):
            r = s.get('kill_reason', '?')
            lvl = 'critical' if r == 'guard_error' else ('info' if r == 'target_hit_6pct' else 'warn')
            add('halt', lvl, 'HALTED: ' + r + ('' if s.get('kill_hard') else ' (clears next day)'))
        else:
            add('halt', 'ok', 'armed')
        # 4 feed freshness (degraded, not fatal — sends just abort until fresh)
        if fa is None:
            add('feed', 'warn', 'feed age unknown (gate not consulted yet)')
        elif float(fa) > stale_min:
            add('feed', 'warn', 'feed %.0fm old > STALE_MIN %.0fm — sends aborting (%s)' % (float(fa), stale_min, fa_src))
        else:
            add('feed', 'ok', 'fresh (%.0fm)' % float(fa))
        # 5 equity sync honesty — the DD guard rides on this number
        et = s.get('equity_ts', 0)
        if not et:
            add('equity_sync', 'warn', 'equity never synced — DD guard on MODELED equity (GET /guard/sync?equity=<real MFF bal>)')
        else:
            ah = (_mins_since(et) or 0) / 60.0
            add('equity_sync', 'warn' if ah > 36 else 'ok', 'synced %.0fh ago' % ah)
        # 6 DD_FLOOR actually set to a real level
        add('dd_floor', 'ok' if os.environ.get('DD_FLOOR') else 'critical',
            ('DD_FLOOR=' + os.environ['DD_FLOOR']) if os.environ.get('DD_FLOOR') else 'DD_FLOOR NOT set — drawdown guard is blind')
        # 7 state persistence
        add('state', 'ok' if _state_writable() else 'critical',
            'DATA_DIR writable' if _state_writable() else 'DATA_DIR NOT writable — dedup / counter / kill-latch all dead')
        # 8 shadow resolver (session classification + outcomes)
        add('shadow', 'ok' if shadow is not None else 'warn',
            'resolver ready' if shadow is not None else 'shadow.py not importable — session/outcome resolution degraded')
        # 9 read-path probe — would guard_ok throw?
        try:
            eval_progress(); _day_stats(); add('gate', 'ok', 'read-path clean')
        except Exception as e:
            add('gate', 'critical', 'gate read-path raises: %s' % e)
        # 10 pipeline liveness (only meaningful when live AND market open)
        mo = market_open if market_open is not None else s.get('market_open')
        seen = _mins_since(s.get('last_seen_ms')); idle_max = _envf('HEALTH_IDLE_MIN', 120)
        if live and mo and seen is not None and seen > idle_max:
            add('pipeline', 'warn', 'gate idle %.0fm during RTH — detector may not be feeding it' % seen)
        else:
            add('pipeline', 'ok', ('last activity %.0fm ago' % seen) if seen is not None else 'no activity yet')

        rank = {'critical': 3, 'warn': 2, 'info': 1, 'ok': 0}
        worst = max((rank.get(c['level'], 0) for c in checks), default=0)
        status = 'critical' if worst == 3 else ('warn' if worst == 2 else 'ok')
        if mode != 'auto': status = 'paused'
        bad = [c for c in checks if c['level'] in ('warn', 'critical')]
        if status == 'ok' and live:   summary = 'AUTO live & healthy'
        elif status == 'paused':      summary = 'AUTO is not the active mode (%s)' % mode
        else:                         summary = '; '.join('%s: %s' % (c['name'], c['detail']) for c in bad)[:260] or status
        out = dict(status=status, mode=mode, live=live, ts=_now_ms(), checks=checks, summary=summary)
        _health_alert(status, summary)
        return out
    except Exception as e:
        print('[guard] health EXC', e, flush=True)
        return dict(status='critical', mode='?', live=False, ts=_now_ms(),
                    checks=[dict(name='health', level='critical', ok=False, detail=str(e))],
                    summary='health check itself raised: %s' % e)

def _health_alert(status, summary):
    """Best-effort push ONLY on a status transition (dedup in state). Set GUARD_ALERT_URL (or reuse
    WEBHOOK_URL) to get a Telegram ping the moment AUTO changes health. No-op if neither is set."""
    try:
        s = _state(); prev = s.get('last_health')
        if status == prev: return
        s['last_health'] = status; s['last_health_ms'] = _now_ms(); _set_state(s)
        if prev is None: return                              # first observation — nothing to compare to
        url = os.environ.get('GUARD_ALERT_URL') or os.environ.get('WEBHOOK_URL')
        if not url or requests is None: return
        icon = {'ok': '✅', 'warn': '⚠️', 'critical': '\U0001f534', 'paused': '⏸️'}.get(status, 'ℹ️')
        msg = '%s AUTO health %s→%s — %s' % (icon, prev, status, summary)
        try: requests.post(url, json={'text': msg, 'raw': msg}, timeout=6)
        except Exception: pass
    except Exception as e:
        print('[guard] health alert err', e, flush=True)

# ---------- Pine export (draw the AUTO trades on TradingView) ----------
def _pine_one(r):
    """One booked order -> Pine box(SL)/box(TP)/entry+SL+TP lines/label at its bar time."""
    try:
        e = float(r.get('entry')); sl = float(r.get('sl') if r.get('sl') is not None else r.get('SL'))
    except Exception:
        return []
    ts = int(r.get('bar_ms') or r.get('ts') or 0)
    if not ts or not e or not sl: return []
    try: tp = float(r.get('tp'))
    except Exception: tp = e + 2 * (e - sl)
    left = ts; right = ts + 90 * 60 * 1000
    q = r.get('qty'); qtxt = ('%s' % q) if q else 'rs'
    txt = ('AUTO %s %s x%s' % (r.get('sess', ''), (r.get('dir') or ''), qtxt)).replace('"', '')
    hi = max(e, sl, tp)
    dp = _envi('GUARD_PRICE_DP', 2)                     # Pine at instrument precision (EURUSD 5dp, JPY 3dp)
    def _f(p): return ('%.*f' % (dp, p))
    return [
        'box.new(%d, %s, %d, %s, xloc=xloc.bar_time, border_color=color.new(color.red,70), bgcolor=color.new(color.red,88))' % (left, _f(max(e, sl)), right, _f(min(e, sl))),
        'box.new(%d, %s, %d, %s, xloc=xloc.bar_time, border_color=color.new(color.green,70), bgcolor=color.new(color.green,88))' % (left, _f(max(e, tp)), right, _f(min(e, tp))),
        'line.new(%d, %s, %d, %s, xloc=xloc.bar_time, color=color.aqua, width=1, style=line.style_dashed)' % (left, _f(e), right, _f(e)),
        'line.new(%d, %s, %d, %s, xloc=xloc.bar_time, color=color.red, width=2)' % (left, _f(sl), right, _f(sl)),
        'line.new(%d, %s, %d, %s, xloc=xloc.bar_time, color=color.green, width=2)' % (left, _f(tp), right, _f(tp)),
        'label.new(%d, %.2f, "%s", xloc=xloc.bar_time, style=label.style_label_down, color=color.new(color.aqua,20), textcolor=color.white, size=size.small)' % (left, hi, txt),
    ]

def pine_book(day=''):
    """Full Pine indicator for the AUTO trades in the guard book (decision sent/manual).
    day='' = all booked trades; else only that YYYY-MM-DD. Paste into TradingView Pine Editor -> Add to chart."""
    rows = [g for g in _load(GLOG, []) if g.get('decision') in ('sent', 'manual')]
    if day: rows = [g for g in rows if g.get('date') == day]
    body = []
    for r in rows:
        body += ['    ' + ln for ln in _pine_one(r)]
    ttl = ('AUTO trades %s' % day) if day else 'AUTO trades'
    head = ['//@version=5',
            'indicator("%s", overlay=true, max_boxes_count=500, max_labels_count=500, max_lines_count=500)' % ttl,
            'if barstate.islast']
    if not body:
        body = ['    label.new(bar_index, high, "no AUTO trades", style=label.style_label_down)']
    return '\n'.join(head + body)

def book_days():
    """Distinct days that have booked AUTO trades, newest first (for the Pine day picker)."""
    ds = sorted({g.get('date') for g in _load(GLOG, [])
                 if g.get('decision') in ('sent', 'manual') and g.get('date')}, reverse=True)
    return ds

# ---------- HTTP: /guard (page), /guard/data, /guard/sync, /guard/kill, /guard/health, /guard/pine ----------
def register(app):
    try: from flask import request, jsonify, Response
    except Exception: return app

    def _data():
        s = _state(); d = _day_stats(); sm = _shadow_by_key()
        book = [_actualize(g, sm.get(g.get('key'), {})) for g in reversed(_load(GLOG, []))][:80]
        return jsonify(mode=exec_mode(), auto=os.environ.get('AUTO_SUBMIT', '0') == '1', kill=_kill_active(s),
                       kill_reason=s.get('kill_reason', ''), day=_today(), eval=eval_progress(),
                       openpos=bool(d.get('openpos')),   # v27.2: peers sharing this broker account read this
                       be=(os.environ.get('MANAGE_BE', '') == '1'), skip=_env('SKIP_SESSIONS', 'LO,ASIA'),
                       trades=d['sent'], max_trades=_envi('MAX_TRADES_DAY', 3),
                       losses=d['losses'], loss_n=_envi('DAY_LOSS_N', 2),
                       day_net=round(d['net']), day_target=_envf('DAY_TARGET_USD', 1500),
                       ramp_left=max(0, _envi('RAMP_TRADES', 3) - s.get('sent_total', 0)),
                       health=health(), pine_days=book_days(), book=book)

    def _authed():
        """GUARD_TOKEN set -> every MUTATING endpoint requires ?t=<token>. These are open GETs on a
        public URL; without this, anyone who finds the URL can clear the hard latch or arm AUTO."""
        tok = os.environ.get('GUARD_TOKEN', '')
        return (not tok) or (request.args.get('t', '') == tok)

    def _sync():
        if not _authed(): return jsonify(ok=False, err='auth'), 401
        try:
            v = float(request.args.get('equity'))
            s = _state(); s['equity'] = v; s['equity_ts'] = _now_ms()
            s['eq_high'] = max(float(s.get('eq_high') or 0), v)   # feeds the auto-trailing DD floor
            _set_state(s)
            return jsonify(ok=True, equity=v, eq_high=s['eq_high'], floor=_dd_floor())
        except Exception as e:
            return jsonify(ok=False, err=str(e)), 400

    def _kill():
        if not _authed(): return jsonify(ok=False, err='auth'), 401
        on = request.args.get('on', '1') == '1'; s = _state()
        s['kill'] = on; s['kill_hard'] = on; s['kill_reason'] = 'manual' if on else ''
        if on: s['kill_day'] = _today()
        _set_state(s)
        if on: flatten_all('manual_halt')          # HALT = stop the bleeding, not only new entries
        return jsonify(ok=True, kill=on)

    def _mode():
        if not _authed(): return jsonify(ok=False, err='auth'), 401
        m = request.args.get('set', '')
        if set_mode(m): return jsonify(ok=True, mode=m)
        return jsonify(ok=False, mode=exec_mode(), allowed=['auto', 'manual', 'off']), 400

    def _page():
        return Response(_HTML, mimetype='text/html')

    def _health():
        """Auto-executor health. ok/warn -> HTTP 200, critical -> 503 (point a free uptime monitor
        — Healthchecks.io / UptimeRobot — at this URL to get a dead-man alert if AUTO breaks).
        ?format=txt for a human-readable checklist."""
        h = health()
        code = 503 if h['status'] == 'critical' else 200
        if request.args.get('format') == 'txt':
            mark = {'ok': '✓', 'info': '·', 'warn': '!', 'critical': '✗'}
            lines = ['AUTO HEALTH: %s  —  %s' % (h['status'].upper(), h['summary'])]
            for c in h['checks']:
                lines.append('  %s %-12s %s' % (mark.get(c['level'], '?'), c['name'], c['detail']))
            return Response('\n'.join(lines) + '\n', mimetype='text/plain', status=code)
        return jsonify(**h), code

    def _pine():
        """Pine script for the AUTO trades (draw them on TradingView). ?day=YYYY-MM-DD or omit for all."""
        return Response(pine_book(request.args.get('day', '')), mimetype='text/plain')

    def _extlog():
        """POST: a satellite strategy (C, ...) sharing this account registers its SEND, or updates
        its outcome. Body: {strat, dir, entry, sl, tp, qty, bos_ms, decision:'sent'} or
        {key, ext_outcome:'win|loss|timeout', ext_net: $}. Token-protected."""
        if not _authed(): return jsonify(ok=False, err='auth'), 401
        b = request.get_json(force=True, silent=True) or {}
        glog = _load(GLOG, [])
        if b.get('key') and b.get('ext_outcome'):                     # outcome update by key
            for g0 in reversed(glog):
                if g0.get('key') == b['key']:
                    g0['ext_outcome'] = b['ext_outcome']; g0['ext_net'] = b.get('ext_net')
                    g0['outcome'] = b['ext_outcome']
                    _save(GLOG, glog); return jsonify(ok=True, updated=True)
            return jsonify(ok=False, err='key not found'), 404
        k = '%s|%s|%s|%.2f' % (b.get('strat', 'EXT'), b.get('dir'), b.get('bos_ms'), float(b.get('entry') or 0))
        for g0 in glog[-100:]:
            if g0.get('key') == k: return jsonify(ok=True, dup=True)
        glog.append(dict(key=k, strat=b.get('strat', 'EXT'), ts=_now_ms(), bar_ms=int(b.get('bos_ms') or 0),
                         date=_today(), et=_et(_now_ms()).strftime('%Y-%m-%d %H:%M'), sess=b.get('sess', '?'),
                         dir=b.get('dir'), entry=b.get('entry'), sl=b.get('sl'), tp=b.get('tp'),
                         qty=b.get('qty'), decision='sent', reason=''))
        _save(GLOG, glog)
        return jsonify(ok=True)

    def _cancel():
        """v27.2 — you canceled a resting order at the broker by hand; tell the book. Marks the row
        ext_outcome='canceled' (net 0): the table stops showing 'open', the one-position slot frees
        NOW instead of at the 4h shadow no_fill write-off, and day counters ignore it.
        GET /guard/cancel?key=<row key>&t=<token>   or   ?last=1&t=<token> (most recent open SENT row)."""
        if not _authed(): return jsonify(ok=False, err='auth'), 401
        try:
            glog = _load(GLOG, []); sm = _shadow_by_key()
            want = request.args.get('key', '')
            hit = None
            for g in reversed(glog):
                if g.get('decision') not in ('sent', 'manual') or g.get('ext_outcome'): continue
                if want:
                    if g.get('key') == want: hit = g; break
                else:
                    if sm.get(g.get('key'), {}).get('outcome', 'open') == 'open': hit = g; break
            if hit is None: return jsonify(ok=False, err='no matching open sent row'), 404
            hit['ext_outcome'] = 'canceled'; hit['ext_net'] = 0
            _save(GLOG, glog)
            print('[guard] row canceled by user:', hit.get('key'), flush=True)
            return jsonify(ok=True, key=hit.get('key'), et=hit.get('et'))
        except Exception as e:
            return jsonify(ok=False, err=str(e)), 500

    app.add_url_rule('/guard/cancel', 'guard_cancel', _cancel)
    app.add_url_rule('/guard/extlog', 'guard_extlog', _extlog, methods=['POST'])
    app.add_url_rule('/guard', 'guard_page', _page)
    app.add_url_rule('/guard/data', 'guard_data', _data)
    app.add_url_rule('/guard/sync', 'guard_sync', _sync)
    app.add_url_rule('/guard/kill', 'guard_kill', _kill)
    app.add_url_rule('/guard/mode', 'guard_mode', _mode)
    app.add_url_rule('/guard/health', 'guard_health', _health)
    app.add_url_rule('/guard/pine', 'guard_pine', _pine)
    return app

_HTML = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Auto-Executor · Guard</title><style>
*{box-sizing:border-box;margin:0;font-family:system-ui,-apple-system,sans-serif}
body{background:#0d0d0d;color:#eee;padding:14px;font-size:13px}
h1{font-size:15px;margin-bottom:2px}.sub{color:#888;font-size:11px;margin-bottom:12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:14px}
.c{background:#1a1a19;border:1px solid #ffffff1a;border-radius:8px;padding:9px 11px}
.c .l{color:#888;font-size:10px}.c .v{font-size:19px;font-weight:600;margin-top:2px}
.pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700}
.on{background:#0ca30c22;color:#3ecb3e}.off{background:#89878122;color:#aaa}.kill{background:#d03b3b22;color:#e66}.manual{background:#3987e522;color:#7ab8f5}.warnp{background:#e0a93b22;color:#e0a93b}
.modebar{display:flex;gap:6px;align-items:center;margin:10px 0 14px}.modebar .lb{color:#888;font-size:11px;margin-right:2px}
.btn{cursor:pointer;border:1px solid #ffffff26;background:#1a1a19;color:#bbb;padding:5px 12px;border-radius:6px;font-size:12px;font-weight:600;text-decoration:none}
.btn.act{border-color:currentColor}.btn.mauto.act{color:#3ecb3e}.btn.mmanual.act{color:#7ab8f5}.btn.moff.act{color:#e0a93b}
.btn.kills{color:#e66;border-color:#d03b3b55}.btn.arms{color:#3ecb3e;border-color:#3ecb3e55;margin-left:auto}
.pinep{margin-top:16px;border-top:1px solid #ffffff14;padding-top:12px}
.pinep .ph{display:flex;gap:8px;align-items:center;font-size:12px;color:#bbb;flex-wrap:wrap;margin-bottom:6px}
.pinep select{background:#1a1a19;color:#ddd;border:1px solid #333;border-radius:5px;padding:4px 8px;font-size:12px}
.cpy{cursor:pointer;border:1px solid #22d3ee55;background:#22d3ee18;color:#22d3ee;padding:4px 12px;border-radius:6px;font-size:12px;font-weight:600}
#pinebox{width:100%;height:26vh;background:#0d0d0d;color:#cfefff;border:1px solid #222;border-radius:8px;padding:9px;font:11px/1.4 monospace;box-sizing:border-box;margin-top:2px}
table{border-collapse:collapse;width:100%;font-size:12px}th{color:#888;text-align:left;font-weight:500;padding:5px;border-bottom:1px solid #333;font-size:10px;text-transform:uppercase}
td{padding:5px;border-bottom:1px solid #232322;font-variant-numeric:tabular-nums}
.sent{color:#3ecb3e;font-weight:600}.blk{color:#e88}.win{color:#3ecb3e}.loss{color:#e66}.open{color:#e0a93b}
.g{color:#888}
.evalbar{background:#1a1a19;border:1px solid #ffffff1a;border-radius:10px;padding:12px 14px;margin-bottom:12px}
.evalbar .top{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;font-size:12px}
.evalbar .ttl{color:#888}.evalbar .st{font-weight:700}
.trackp{position:relative;height:20px;background:#282826;border-radius:5px;overflow:hidden}
.fillp{height:100%;border-radius:5px;min-width:3px}
.bn{padding:2px 9px;border-radius:10px;font-size:11px;font-weight:700}</style></head><body>
<h1>Auto-Executor — Guard <span id=mode class=pill></span> <span id=kill class=pill></span> <span id=health class=pill></span></h1>
<div class=sub>MFF Pro-100k eval · resting-limit A/B · fail-closed · stale-data aborts the send · <span id=day></span> · <span class=g>sync real equity: /guard/sync?equity=NNNNN · health JSON: /guard/health</span></div>
<div id=healthbar class=sub style="margin:-6px 0 12px;font-weight:600"></div>
<div class=modebar>
 <span class=lb>MODE</span>
 <a class="btn mauto" href="/guard/mode?set=auto" onclick="return flip('auto')">AUTO</a>
 <a class="btn mmanual" href="/guard/mode?set=manual" onclick="return flip('manual')">MANUAL (tap-to-approve)</a>
 <a class="btn moff" href="/guard/mode?set=off" onclick="return flip('off')">OFF</a>
 <a class="btn arms" href="#" onclick="return doarm()" title="Clear a HALT latch — re-arm the executor">▶ ARM</a>
 <a class="btn kills" href="#" onclick="return dokill()" title="Stop &amp; latch — places no orders until you ARM">■ HALT</a>
</div>
<div class="evalbar"><div class="top"><span class="ttl" id=ev_head></span><span class="st" id=ev_state></span></div>
<div class="trackp"><div class="fillp" id=ev_fill></div></div></div>
<div class=cards id=cards></div>
<div class=modebar style="margin-top:4px">
 <span class=lb>SHOW</span>
 <a class="btn fall act" href="#" onclick="return setf('all')">All decisions</a>
 <a class="btn fsent" href="#" onclick="return setf('sent')">Fired only</a>
 <a class="btn fblk" href="#" onclick="return setf('blocked')">Blocked only</a>
 <span class=g style="font-size:11px">blocked rows never traded — their R/Net$ are model-priced (shown gray)</span>
</div>
<table><thead><tr><th>Strat</th><th>Time ET</th><th>Sess</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP</th><th>Qty</th><th>Decision</th><th>Outcome</th><th>R</th><th>Net$</th></tr></thead><tbody id=tb></tbody></table>
<div class="pinep">
 <div class="ph">📈 <b>Pine for TradingView</b> — see the AUTO trades on your chart
  <select id="pineday" onchange="loadPine()"></select>
  <button class="cpy" onclick="copyPine(this)">Copy script</button>
  <span class="g">paste into TradingView → Pine Editor → Add to chart</span>
 </div>
 <textarea id="pinebox" readonly placeholder="pick a day (or 'all trades') → the Pine appears here"></textarea>
</div>
<script>
async function load(){
 let d=await (await fetch('/guard/data',{cache:'no-store'})).json();
 let mp=document.getElementById('mode');mp.textContent=d.mode.toUpperCase();mp.className='pill '+(d.mode=='auto'?'on':d.mode=='manual'?'manual':'off');
 document.querySelectorAll('.btn.mauto,.btn.mmanual,.btn.moff').forEach(b=>b.classList.remove('act'));
 let ab=document.querySelector('.btn.m'+d.mode);if(ab)ab.classList.add('act');
 let k=document.getElementById('kill');k.textContent=d.kill?('HALTED: '+d.kill_reason):'armed';k.className='pill '+(d.kill?'kill':'on');
 let h=d.health||{};let hp=document.getElementById('health');
 hp.textContent='HEALTH '+(h.status||'?').toUpperCase();
 hp.className='pill '+(h.status=='ok'?'on':h.status=='critical'?'kill':h.status=='paused'?'off':'warnp');
 let hb=document.getElementById('healthbar');
 hb.textContent=(h.status&&h.status!='ok'&&h.status!='paused')?('⚠ '+(h.summary||'')):'';
 hb.style.color=h.status=='critical'?'#e66':'#e0a93b';
 document.getElementById('day').textContent=d.day;
 let e=d.eval||{};
 let ef=document.getElementById('ev_fill');ef.style.width=Math.max(3,Math.min(100,e.pct||0))+'%';
 ef.style.background=e.breached?'#d03b3b':e.passed?'#0ca30c':((e.pnl||0)<0?'#e0a93b':'#3987e5');
 document.getElementById('ev_head').textContent='Eval progress — $'+(e.start||0).toLocaleString()+' → $'+(e.target||0).toLocaleString()+' (+6%)';
 document.getElementById('ev_state').innerHTML=e.passed?'<span class=bn style="background:#0ca30c22;color:#3ecb3e">✓ TARGET HIT — PASS</span>':e.breached?'<span class=bn style="background:#d03b3b22;color:#e66">✕ DRAWDOWN BREACHED — halted</span>':('$'+(e.to_target||0).toLocaleString()+' to target · '+(e.pct||0)+'%');
 let bufc=(e.buffer||0)<1200?'#e66':'#3ecb3e';
 document.getElementById('cards').innerHTML=[
  ['Equity (modeled)','$'+(e.equity||0).toLocaleString()],
  ['P&L vs start','<span style=color:'+((e.pnl||0)>=0?'#3ecb3e':'#e0a93b')+'>'+((e.pnl||0)>=0?'+':'')+'$'+(e.pnl||0).toLocaleString()+'</span>'],
  ['% to +6% target',(e.pct||0)+'%'],['DD buffer','<span style=color:'+bufc+'>$'+(e.buffer||0).toLocaleString()+'</span>'],
  ['Trades today',d.trades+' / '+d.max_trades],['Losses today',d.losses+' / '+d.loss_n],
  ['Day P&L','$'+d.day_net+' / '+d.day_target],
  ['Ramp · BE',(d.ramp_left>0?(d.ramp_left+'@1'):'sized')+' · BE '+(d.be?'ON@1R':'off')]
 ].map(c=>'<div class=c><div class=l>'+c[0]+'</div><div class=v>'+c[1]+'</div></div>').join('');
 let dec=x=>x.decision=='sent'?('<span class=sent>SENT'+(x.qty?(' ×'+x.qty):'')+'</span>'):x.decision=='manual'?('<span class=sent>ARMED'+(x.qty?(' ×'+x.qty):'')+'</span>'):('<span class=blk>BLOCK: '+(x.reason||'')+'</span>');
 let oc=x=>{let o=x.outcome||'';let c=o=='win'?'win':o=='loss'?'loss':o=='open'?'open':'g';
  let h='<span class='+c+'>'+o+'</span>';
  if(o=='open'&&(x.decision=='sent'||x.decision=='manual'))
   h+=' <a href="#" data-k="'+encodeURIComponent(x.key||'')+'" title="I canceled this order at the broker — mark it canceled and free the slot" onclick="return cancelRow(this.dataset.k)" style="color:#e0a93b;text-decoration:none">✕</a>';
  return h;};
 let fired=x=>x.decision=='sent'||x.decision=='manual';
 window._dec=dec;window._oc=oc;window._fired=fired;window._book=d.book||[];
 renderBook();
 let ps=document.getElementById('pineday');
 let opts='<option value="">all trades</option>'+((d.pine_days||[]).map(dd=>'<option value="'+dd+'">'+dd+'</option>').join(''));
 if(ps.dataset.sig!==opts){let cur=ps.value;ps.innerHTML=opts;ps.dataset.sig=opts;if(cur)ps.value=cur;loadPine();}
}
let _filter='all';
function setf(f){_filter=f;document.querySelectorAll('.btn.fall,.btn.fsent,.btn.fblk').forEach(b=>b.classList.remove('act'));
 document.querySelector('.btn.f'+(f=='all'?'all':f=='sent'?'sent':'blk')).classList.add('act');renderBook();return false;}
function renderBook(){
 let dec=window._dec,oc=window._oc,fired=window._fired;if(!dec)return;
 let rows=(window._book||[]).filter(x=>_filter=='all'||(_filter=='sent'?fired(x):!fired(x)));
 document.getElementById('tb').innerHTML=rows.map(x=>{
  let isB=!fired(x);                         // blocked rows never traded -> model-priced R/Net$, render gray
  let rc=isB?' style="color:#6b7688" title="model-priced — trade was NOT executed"':'';
  let rv=(x.R!=null?x.R:''),nv=(x.net!=null?x.net:'');
  if(isB&&(rv!==''||nv!=='')){rv=rv!==''?('('+rv+')'):'';nv=nv!==''?('('+nv+')'):'';}
  return '<tr><td><b>'+(x.strat||'A/B')+'</b></td><td>'+(x.et||'')+'</td><td>'+(x.sess||'')+'</td><td>'+(x.dir||'')+
  '</td><td>'+(x.entry||'')+'</td><td>'+(x.sl||'')+'</td><td>'+(x.tp||'')+'</td><td>'+(x.qty||'')+'</td><td>'+dec(x)+'</td><td'+(isB?rc:'')+'>'+oc(x)+
  '</td><td'+rc+'>'+rv+'</td><td'+rc+'>'+nv+'</td></tr>';}).join('');
}
let _tok=new URLSearchParams(location.search).get('t');
try{ if(_tok) localStorage.setItem('guard_t',_tok); else _tok=localStorage.getItem('guard_t'); }catch(e){}
const _t=_tok?('&t='+encodeURIComponent(_tok)):'';
async function flip(m){await fetch('/guard/mode?set='+m+_t,{cache:'no-store'});load();return false;}
async function dokill(){await fetch('/guard/kill?on=1'+_t,{cache:'no-store'});load();return false;}
async function cancelRow(k){let r=await (await fetch('/guard/cancel?key='+k+(_t||''),{cache:'no-store'})).json();
 if(!r.ok){document.getElementById('healthbar').textContent='⚠ cancel: '+(r.err||'failed');}load();return false;}
async function doarm(){await fetch('/guard/kill?on=0'+_t,{cache:'no-store'});load();return false;}
async function loadPine(){let day=document.getElementById('pineday').value;
 let t=await (await fetch('/guard/pine?day='+encodeURIComponent(day),{cache:'no-store'})).text();
 document.getElementById('pinebox').value=t;}
function copyPine(btn){let b=document.getElementById('pinebox');b.select();navigator.clipboard.writeText(b.value);
 btn.textContent='Copied ✓';setTimeout(()=>{btn.textContent='Copy script';},1200);}
load();setInterval(load,30000);
</script></body></html>"""
