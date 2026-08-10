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

The eval-killer is the $3k trailing drawdown. In this v32 challenge build, authenticated broker
snapshots and order lifecycle callbacks are mandatory for AUTO. Count-based brakes remain a second
independent floor, but position, working-order, equity, drawdown and realized-P&L truth come from
``broker_feedback.py``. Shadow outcomes are diagnostic only and can never free a live commitment.

Wire (agent.py):  import guardrails  (top, next to `import shadow`)
                  guardrails.register(app)  (bottom, next to `shadow.register(app)`)
                  gate the _exec_order call — see GUARDRAILS_PATCH.md.

Env (defaults tuned for the Pro-100k eval at $99,887):
  AUTO_SUBMIT=0            master switch (agent checks it; 1 = actually stage)
  AUTO_SESSIONS=NYAM,NYPM,PM_AH   only auto-fire these (his green sessions; London/Asia/PREM excluded)
  MAX_TRADES_DAY=3        stop over-trading a chop day
  DAY_LOSS_N=2             daily stop after N losing A/B setup groups today
  DAY_LOSS_COUNT_MODE=group  A/B + shallow are one setup and one loss toward DAY_LOSS_N
  DAY_LOSS_USD=1000      halt+latch after -$ modeled loss today       (secondary)
  DAY_TARGET_USD=1500    profit-lock: stop for the day after +$ (keeps best day < 50% of $6k => consistency-safe)
  DD_FLOOR=97000         MFF trailing max-loss level (READ IT off MFF; update as it trails up)
  DD_BUFFER=800          proximity threshold; behavior set by DD_PROXIMITY_MODE
  RAMP_TRADES=3          first N SENT trades run at qty=1 (prove routing) then normal size
  START_EQUITY=99887     modeled equity seed until first /guard/sync
  STALE_MIN=20           block if the bar feed is older than this (market hours)
  DATA_DIR=.             persist dir (shadow_log.json lives here too)

Hardening (2026-07-19 review):
  NEWS_GUARD=1           block auto sends inside ±30min of high-impact events (agent passes the flag)
  MIN_SL_PTS=5           skip degenerate tight-SL setups (absurd qty + slippage beyond -1R)
  DD_PROXIMITY_MODE=soft  off | soft (block one setup) | hard (legacy latch+flatten)
  DD_TRAIL_USD=3000      auto-trailing floor: max(DD_FLOOR, highest synced equity - this)
  DD_FLOOR_CAP=100100    100K eval trail lock (start balance + $100)
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
    import broker_feedback                          # authenticated broker/order/account truth
except Exception:
    broker_feedback = None
try:
    from zoneinfo import ZoneInfo; _NY = ZoneInfo('America/New_York')
except Exception:
    _NY = None

DATA_DIR = os.environ.get('DATA_DIR', '.')
GLOG     = os.path.join(DATA_DIR, 'guard_log.json')    # every decision (sent/blocked) — the /guard book
GSTATE   = os.path.join(DATA_DIR, 'guard_state.json')  # kill-latch, ramp counter, synced equity
RISK_DOLLAR = 500.0                                     # 1R at 0.5%/$100k (display only)


class GuardPersistenceError(RuntimeError):
    """A critical guard ledger could not be read or durably written."""

def _env(k, d):        return os.environ.get(k, d)
def _envf(k, d):       return float(os.environ.get(k, str(d)))
def _envi(k, d):       return int(float(os.environ.get(k, str(d))))

def _dd_proximity_mode():
    """off | soft | hard.

    off  = do not pre-emptively block on modeled proximity; hard DD breach still latches.
    soft = block only the current setup and auto-resume; never flatten or persist a halt.
    hard = legacy behavior: persistent hard halt + flatten/cancel.
    """
    m = _env('DD_PROXIMITY_MODE', 'soft').strip().lower()
    return m if m in ('off', 'soft', 'hard') else 'soft'

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
    if not os.path.exists(p):
        return d
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        if p in (GLOG, GSTATE):
            raise GuardPersistenceError('critical ledger corrupt: ' + os.path.basename(p)) from e
        return d
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
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(x, f); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, p)
        return True
    except Exception as e:
        print('[guard] save err', p, e, flush=True)
        raise GuardPersistenceError('critical ledger write failed: ' + os.path.basename(p)) from e

def _skey(x):
    """same identity shadow.py uses, so a guard row can be joined to its shadow outcome."""
    try:
        if shadow is not None:
            return shadow._key(x.get('_strat', 'A/B'), x.get('dir'), int(x.get('bos_ms') or _now_ms()),
                               round(float(x.get('entry')), _envi('GUARD_PRICE_DP', 2)))
    except Exception: pass
    return "%s|%s|%s|%s" % (x.get('_strat', 'A/B'), x.get('dir'), x.get('entry'), x.get('bos_ms'))

def _wd(x):
    """weekday of the signal (0=Mon) by ET clock."""
    try: return _et(int(x.get('bos_ms') or _now_ms())).weekday()
    except Exception: return -1

def is_duplicate(x):
    """True when this exact setup group/strategy was already staged today.

    Group identity (when present) is authoritative.  A/B and A/B-shallow are
    siblings inside one batch and are not guarded separately, so the one-trade
    rule blocks a re-fire of the whole group, never the second sibling.
    """
    try:
        dp = _envi('GUARD_PRICE_DP', 1)
        day = _today(); gid = x.get('_setup_group_id')
        strat = x.get('_strat', 'A/B'); d = x.get('dir')
        e = round(float(x.get('entry')), dp)
        bos = int(x.get('bos_ms') or 0)
        for g in _load(GLOG, []):
            if g.get('date') != day or g.get('decision') not in ('sent', 'manual'):
                continue
            if gid and g.get('setup_group_id') == gid:
                return True
            # Legacy rows without a group id: include strategy and BOS so two
            # different setups at the same price are not collapsed all day.
            if not gid and not g.get('setup_group_id'):
                if g.get('strat', 'A/B') == strat and g.get('dir') == d                         and round(float(g.get('entry') or 0), dp) == e                         and int(g.get('bar_ms') or 0) == bos:
                    return True
    except Exception as ex:
        print('[guard] is_duplicate err', ex, flush=True)
    return False

# ---------- state ----------
_DEF_STATE = {'kill': False, 'kill_reason': '', 'kill_day': '', 'kill_hard': False,
              'sent_total': 0, 'equity': None, 'equity_ts': 0,
              'equity_sync_day': '', 'equity_day_net_at_sync': 0.0}
def _state():
    s, corrupt = _load_failclosed(GSTATE, dict(_DEF_STATE))
    if corrupt:                                    # corrupted state -> HARD kill until a human looks
        s = dict(_DEF_STATE); s.update(kill=True, kill_hard=True, kill_reason='state_corrupt',
                                       kill_day=_today())
    # v31.9 migration: legacy dd_proximity was a persistent hard halt.  In soft/off mode
    # clear ONLY that legacy latch automatically.  A real dd_breached, manual kill, state
    # corruption or uncertain sibling batch remains hard and requires operator review.
    if (not corrupt and s.get('kill') and s.get('kill_reason') == 'dd_proximity'
            and _dd_proximity_mode() != 'hard'):
        s['kill'] = False; s['kill_hard'] = False; s['kill_reason'] = ''; s['kill_day'] = ''
        s['last_auto_clear_reason'] = 'legacy_dd_proximity'
        s['last_auto_clear_ms'] = _now_ms()
        _save(GSTATE, s)
        print('[guard] auto-cleared legacy dd_proximity latch (%s mode)' % _dd_proximity_mode(), flush=True)
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
    if reason in ('day_loss_n', 'day_loss_usd', 'dd_proximity', 'dd_breached', 'target_hit_6pct', 'state_corrupt', 'sibling_batch_uncertain'):
        flatten_all('latch:' + reason)

def _dd_floor():
    """Effective trailing floor = max(DD_FLOOR env, highest synced equity - DD_TRAIL_USD).
    MFF's EOD-trailing floor only RISES; tracking eq_high makes the guard follow it automatically
    instead of trusting a manually-updated env var that goes stale after every green day."""
    env_floor = _envf('DD_FLOOR', 97000.0)
    try:
        if broker_feedback is not None and broker_feedback.feedback_required():
            snap = broker_feedback.truth()
            if snap.get('drawdown_floor') is not None:
                return float(snap['drawdown_floor'])
        hi = float(_state().get('eq_high') or 0)
        trail = hi - _envf('DD_TRAIL_USD', 3000.0)
        cap = _envf('DD_FLOOR_CAP', 100100.0)         # 100K eval locks at start + $100
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
        if _env('CHALLENGE_MODE', '1') != '1' and os.environ.get('EXEC_FX') == '1':
            try:
                import exec_fx
                return exec_fx.flatten(reason)
            except Exception as e:
                print('[guard] fx flatten err', e, flush=True); return False
        url = os.environ.get('EXEC_WEBHOOK', '')
        if not url or requests is None: return False
        tick = os.environ.get('EXEC_TICKER', os.environ.get('CONTRACT', 'MNQ1!'))
        ok = []
        accepted = True
        for action in ('exit', 'cancel'):
            res = _relay_action(action)
            accepted = accepted and bool(res.get('ok'))
            ok.append('%s:%s' % (action, res.get('status') if res.get('status') is not None else 'err'))
        print('[guard] FLATTEN (%s) ->' % reason, ' '.join(ok), flush=True)
        if accepted and broker_feedback is not None:
            try:
                broker_feedback.mark_cleanup_requested('exit', reason)
            except Exception as e:
                print('[guard] cleanup ledger err', e, flush=True)
                accepted = False
        aurl = os.environ.get('GUARD_ALERT_URL') or os.environ.get('WEBHOOK_URL')
        if aurl:
            msg = '\U0001f9f9 FLATTEN+CANCEL sent (%s) — %s' % (reason, ' '.join(ok))
            try: requests.post(aurl, json={'text': msg, 'raw': msg}, timeout=6)
            except Exception: pass
        return accepted
    except Exception as e:
        print('[guard] flatten_all err', e, flush=True); return False

def _active_pending_group(s=None):
    """Return a live batch reservation, clearing an expired one safely."""
    st = _state() if s is None else s
    pg = st.get('pending_group') or None
    if not pg:
        return None
    try:
        if int(pg.get('expires_ms') or 0) <= _now_ms():
            st['last_batch'] = {**pg, 'status': 'reservation_expired', 'ended_ms': _now_ms()}
            st['pending_group'] = None
            _set_state(st)
            return None
    except Exception:
        return pg
    return pg


def begin_sibling_batch(group_id, planned_risk_usd, strats=None):
    """Persistently reserve the one active setup before the first relay call."""
    try:
        s = _state()
        if _active_pending_group(s):
            return False
        try:
            sec = int(float(_env('EXEC_CANCEL_AFTER_SEC', '') or 0))
        except Exception:
            sec = 0
        if sec <= 0:
            sec = int(round(_envf('FILL_WIN_MIN', 10) * 60))
        grace = _envi('BATCH_PENDING_GRACE_SEC', 120)
        now = _now_ms()
        s['pending_group'] = dict(group_id=str(group_id), started_ms=now,
                                  expires_ms=now + max(60, sec + grace) * 1000,
                                  planned_risk_usd=round(float(planned_risk_usd or 0), 2),
                                  strats=list(strats or []), accepted=[])
        _set_state(s)
        return True
    except Exception as e:
        print('[guard] begin_sibling_batch err', e, flush=True)
        return False


def touch_sibling_batch(group_id, strat, relay_status=None):
    try:
        s = _state(); pg = _active_pending_group(s)
        if not pg or pg.get('group_id') != str(group_id): return False
        acc = list(pg.get('accepted') or [])
        acc.append(dict(strat=strat, relay_status=relay_status, at_ms=_now_ms()))
        pg['accepted'] = acc; s['pending_group'] = pg; _set_state(s); return True
    except Exception as e:
        print('[guard] touch_sibling_batch err', e, flush=True); return False


def finish_sibling_batch(group_id, status='sent'):
    try:
        s = _state(); pg = s.get('pending_group') or {}
        if pg and pg.get('group_id') != str(group_id): return False
        s['last_batch'] = {**pg, 'status': status, 'ended_ms': _now_ms()}
        s['pending_group'] = None; _set_state(s); return True
    except Exception as e:
        print('[guard] finish_sibling_batch err', e, flush=True); return False


def _relay_action(action):
    url = os.environ.get('EXEC_WEBHOOK', '')
    if not url or requests is None:
        return dict(action=action, ok=False, status=None, error='exec webhook unavailable')
    tick = os.environ.get('EXEC_TICKER', os.environ.get('CONTRACT', 'MNQ1!'))
    attempts = max(1, _envi('GUARD_ACTION_RETRIES', 3))
    last = dict(action=action, ok=False, status=None)
    for attempt in range(1, attempts + 1):
        try:
            r = requests.post(url, json={'ticker': tick, 'action': action}, timeout=10)
            st = getattr(r, 'status_code', None)
            last = dict(action=action, ok=(st is not None and 200 <= int(st) < 300),
                        status=st, attempt=attempt)
            if last['ok']:
                return last
        except Exception as e:
            last = dict(action=action, ok=False, status=None, attempt=attempt, error=str(e))
        if attempt < attempts:
            time.sleep(min(1.0, 0.2 * attempt))
    return last


def rollback_sibling_batch(group_id, reason='partial_send'):
    """Best available atomic rollback: CANCEL first, then EXIT any possible fill.

    A 2xx response confirms relay acceptance, not final broker state.  If either
    command is not accepted, AUTO hard-kills because the account state is unknown.
    """
    cancel = _relay_action('cancel')
    exit_ = _relay_action('exit')
    ok = bool(cancel.get('ok') and exit_.get('ok'))
    result = dict(ok=ok, group_id=str(group_id), reason=reason,
                  cancel=cancel, exit=exit_, at_ms=_now_ms())
    if ok:
        finish_sibling_batch(group_id, 'rolled_back')
    else:
        try:
            s = _state(); pg = s.get('pending_group') or {}
            pg.update(status='rollback_uncertain', rollback=result)
            s['pending_group'] = pg; _set_state(s)
        except Exception: pass
        _latch('sibling_batch_uncertain', hard=True)
    print('[guard] SIBLING ROLLBACK', result, flush=True)
    return result


def sweep_orphans():
    """Cancel broker-side orphan LIMIT orders: guard-book trades whose modeled outcome says the limit
    never filled (no_fill / missed / expired). Without this the resting order lives on at the broker
    after the guard has already freed the one-position slot -> a later fill silently stacks positions.
    Only sweeps when the modeled book shows NO open position ('cancel' cancels ALL open orders for the
    ticker — must not strip the bracket off a live trade). Called from agent.py after shadow.refresh."""
    try:
        if broker_feedback is not None and broker_feedback.feedback_required():
            expired = broker_feedback.expired_execution_ids()
            if not expired:
                return 0
            # Broker truth, not minute-bar shadow, decides whether there is a
            # position.  A cancel accepted by the relay remains a commitment
            # until a later callback/snapshot confirms zero working orders.
            if abs(float((broker_feedback.truth() or {}).get('position_qty') or 0)) > 0:
                return 0
            s = _state(); last = int(s.get('orphan_cancel_attempt_ms') or 0)
            if _now_ms() - last < _envi('GUARD_CLEANUP_RETRY_SEC', 30) * 1000:
                return 0
            s['orphan_cancel_attempt_ms'] = _now_ms(); _set_state(s)
            return len(expired) if flatten_cancel_only() else 0
        d = _day_stats()
        if d['openpos']: return 0                     # never cancel while a bracket protects a position
        s = _state(); swept = s.get('swept') or {}
        sm = _shadow_by_key(); n = 0
        for g in _load(GLOG, []):
            if g.get('decision') != 'sent': continue
            k = g.get('key')
            if not k or k in swept: continue
            oc = sm.get(k, {}).get('outcome')
            # v28: with FILL_WIN_MIN=10 the write-off happens minutes after the send, so the blanket
            # ticker cancel now runs often. Wait SWEEP_LAG_MIN past the window before touching the
            # broker, so a fill at 9:59 that the book has not resolved yet cannot lose its bracket.
            _lag = _envf('SWEEP_LAG_MIN', 3) * 60000
            if (_now_ms() - int(g.get('ts') or 0)) < _lag: continue
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
        if _env('CHALLENGE_MODE', '1') != '1' and os.environ.get('EXEC_FX') == '1': return False
        url = os.environ.get('EXEC_WEBHOOK', '')
        if not url or requests is None or _env('GUARD_FLATTEN', '1') != '1': return False
        res = _relay_action('cancel')
        print('[guard] ORPHAN CANCEL ->', res.get('status'), flush=True)
        if not res.get('ok'):
            return False
        if broker_feedback is not None:
            broker_feedback.mark_cleanup_requested('cancel', 'orphan_expiry')
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
        s = _state(); pending = s.get('eod_cleanup') or {}
        # Confirmation may arrive after the 18:00 reopen.  Clear the pending
        # cleanup on any heartbeat; never keep AUTO blocked until tomorrow's
        # EOD window merely because the callback was slightly delayed.
        if pending and broker_feedback is not None:
            if broker_feedback.flat_confirmed_since(int(pending.get('requested_ms') or 0)):
                s['eod_flat_day'] = pending.get('day') or _today()
                s['eod_cleanup'] = None; _set_state(s)
                return True
        # fire ONLY inside [deadline, 18:00 ET). After the 18:00 reopen a NEW trading day is live —
        # a late-firing flatten there would close a legitimate overnight position (v27.0 bug).
        if not (dl <= nm < 18 * 60): return False
        s = _state()
        if s.get('eod_flat_day') == _today(): return False
        pending = s.get('eod_cleanup') or {}
        if pending.get('day') == _today() and broker_feedback is not None:
            if _now_ms() - int(pending.get('attempt_ms') or 0) < _envi('GUARD_CLEANUP_RETRY_SEC', 30) * 1000:
                return False
        accepted = flatten_all('eod_%02d:%02d' % (dl // 60, dl % 60))
        if not accepted:
            s = _state(); s['eod_cleanup'] = {'day': _today(), 'requested_ms': _now_ms(),
                                              'attempt_ms': _now_ms(), 'relay_accepted': False}
            _set_state(s); return False
        if broker_feedback is not None and broker_feedback.feedback_required():
            now_ms = _now_ms(); s = _state()
            prior_request = int((pending or {}).get('requested_ms') or now_ms)
            s['eod_cleanup'] = {'day': _today(), 'requested_ms': prior_request,
                                'attempt_ms': now_ms, 'relay_accepted': True}
            _set_state(s)
            return False                         # only a later flat snapshot completes EOD
        s = _state(); s['eod_flat_day'] = _today(); _set_state(s)
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
    """'auto' = guarded full-auto · 'manual' = review-only/no broker send · 'off' = alerts only.
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
    if _env('CHALLENGE_MODE', '1') == '1': return False
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
    """Light gate for MANUAL review mode. MANUAL never calls the execution webhook in v31.11.
    This function only decides whether a candidate may be shown/logged for operator review."""
    try:
        beat(feed_age_min, market_open)              # stamp liveness + last feed age for /guard/health
        s = _state()
        if _kill_active(s) and s.get('kill_hard'): return (False, 'killed:' + (s.get('kill_reason') or '?'))
        ep = eval_progress()
        if ep['passed']:                           return (False, 'target_hit')
        if ep['breached']:                         return (False, 'dd_breached')
        if _active_pending_group(s):                return (False, 'group_pending')
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

# ---------- modeled equity + eval progress counter ----------
def _modeled_equity(s=None, d=None):
    """Return current modeled equity without double-counting today's P&L.

    `state.equity` is the absolute broker equity supplied to /guard/sync. During the same
    trading day we add only the change in guard day-net since that snapshot. On a later
    trading day the full current-day net is added to the last absolute broker snapshot.

    Legacy state files without sync metadata retain the old `equity + day_net` behavior
    until the operator performs one fresh /guard/sync after deploying this version.
    """
    if broker_feedback is not None and broker_feedback.feedback_required():
        snap = broker_feedback.truth()
        if snap.get('equity') is None:
            raise RuntimeError('broker equity unavailable')
        return float(snap['equity'])
    s = s or _state()
    d = d or _day_stats()
    base = float(s.get('equity', _envf('START_EQUITY', 99887.0)))
    sync_day = str(s.get('equity_sync_day') or '')
    if sync_day and sync_day == _today():
        at_sync = float(s.get('equity_day_net_at_sync') or 0.0)
        return base + float(d.get('net') or 0.0) - at_sync
    return base + float(d.get('net') or 0.0)

def eval_progress():
    """Where the MFF eval stands. Uses absolute synced equity plus only post-sync P&L.
    passed = hit the profit target (+6%); breached = broke the trailing drawdown floor."""
    try:
        s = _state(); d = _day_stats()
        start  = _envf('START_BALANCE', 100000.0)                 # eval starting balance
        target = _envf('TARGET_BALANCE', start + 6000.0)          # +$6,000 = +6%
        floor  = _dd_floor()                                      # auto-trails with synced equity highs
        eq     = _modeled_equity(s, d)
        snap = broker_feedback.truth() if broker_feedback is not None and broker_feedback.feedback_required() else {}
        profit = eq - start
        target_reached = eq >= target
        trading_days = int(snap.get('trading_days') or s.get('trading_days') or 0)
        best_day = float(snap.get('best_day_profit') or 0.0)
        if not best_day:
            daily = snap.get('daily_pnl') or {}
            best_day = max([float(v or 0) for v in daily.values()] + [0.0])
        consistency_ratio = (best_day / profit) if profit > 0 else None
        consistency_limit = _envf('CONSISTENCY_LIMIT', 0.50)
        consistency_met = bool(consistency_ratio is not None and consistency_ratio <= consistency_limit + 1e-9)
        min_days_met = trading_days >= _envi('MIN_TRADING_DAYS', 2)
        pass_ready = bool(target_reached and consistency_met and min_days_met)
        broker_status = str(snap.get('evaluation_status') or 'active').lower()
        require_status = _env('PASS_REQUIRE_BROKER_STATUS', '1') == '1'
        passed = broker_status == 'passed' if require_status else pass_ready
        breached = bool(eq <= floor or broker_status in ('failed', 'breached'))
        return dict(equity=round(eq), start=round(start), target=round(target), floor=round(floor),
                    pnl=round(eq - start), to_target=round(target - eq), buffer=round(eq - floor),
                    pct=round(100.0 * (eq - start) / 6000.0, 1),      # % of the $6k target reached
                    target_reached=target_reached, trading_days=trading_days,
                    min_trading_days=_envi('MIN_TRADING_DAYS', 2), min_days_met=min_days_met,
                    best_day_profit=round(best_day, 2),
                    consistency_ratio=(round(consistency_ratio, 4) if consistency_ratio is not None else None),
                    consistency_limit=consistency_limit, consistency_met=consistency_met,
                    pass_ready=pass_ready, broker_status=broker_status,
                    awaiting_pass_confirmation=bool(pass_ready and not passed),
                    passed=passed, breached=breached)
    except Exception as e:
        print('[guard] eval_progress err', e, flush=True)
        return dict(equity=0, start=100000, target=106000, floor=97000, pnl=0, to_target=6000,
                    buffer=0, pct=0.0, target_reached=False, trading_days=0,
                    min_trading_days=2, min_days_met=False, best_day_profit=0,
                    consistency_ratio=None, consistency_limit=0.5, consistency_met=False,
                    pass_ready=False, broker_status='unknown', awaiting_pass_confirmation=False,
                    passed=False, breached=False, error=str(e))

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
    if broker_feedback is not None and g.get('execution_id'):
        bt = broker_feedback.trade_truth(g.get('execution_id'))
        if bt is not None:
            return {**g, 'outcome': bt.get('outcome'), 'net': bt.get('net'),
                    'R': None, 'truth_source': bt.get('source', 'broker')}
    if g.get('ext_outcome'):                      # legacy manual reconciliation only
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
    # A/B and A/B-shallow are sibling rows of one signal group.  The setup counts once for
    # MAX_TRADES_DAY and, by default, once for DAY_LOSS_N.  DAY_LOSS_USD still sums the
    # realized/modelled P&L of every leg, so both independent budgets remain represented.
    grouped = {}
    for t in sent:
        grouped.setdefault(t.get('setup_group_id') or t.get('key'), []).append(t)
    n_trades = 0; losses = 0; net = 0.0
    # The challenge rule is invariant: one A/B signal group is one loss even if
    # both independently-sized sibling legs stop out.  Force it here so a stale
    # Railway value (DAY_LOSS_COUNT_MODE=leg) cannot silently change behavior.
    if _env('CHALLENGE_MODE', '0') == '1':
        loss_mode = 'group'
    else:
        loss_mode = _env('DAY_LOSS_COUNT_MODE', 'group').strip().lower()
        if loss_mode not in ('group', 'leg'):
            loss_mode = 'group'
    exth = _envf('EXT_OPEN_H', 4) * 3600000
    openpos = False
    for grows in grouped.values():
        consumed_risk = False
        group_open = False
        group_net = 0.0
        group_leg_losses = 0
        for t in grows:
            oc = t.get('outcome')
            if oc not in ('canceled', 'no_fill', 'missed', 'open', None, ''):
                consumed_risk = True
            if oc == 'loss':
                group_leg_losses += 1
            if oc in ('win', 'loss', 'timeout'):
                leg_net = float(t.get('net') or 0)
                group_net += leg_net
                net += leg_net
            if oc == 'open':
                group_open = True
                if t.get('strat', 'A/B') not in ('A/B', 'A/B-shallow') and not t.get('ext_outcome'):
                    if (_now_ms() - int(t.get('ts') or 0)) < exth:
                        openpos = True
                else:
                    openpos = True
        if consumed_risk:
            n_trades += 1
            if loss_mode == 'leg':
                losses += group_leg_losses
            elif not group_open and group_net < 0:
                # One setup is one losing trade even when both independently-sized siblings lose.
                # Mixed win/loss siblings are classified by their combined P&L.
                losses += 1
    return dict(sent=n_trades, losses=losses, net=net, openpos=openpos, loss_mode=loss_mode)

# ---------- the gate ----------
def guard_ok(x, feed_age_min=None, market_open=None, news_hard=None, cal_age_h=None):
    """(True,'ok') to allow staging, else (False,'<reason>'). FAIL-CLOSED on any error."""
    try:
        beat(feed_age_min, market_open)              # stamp liveness + last feed age for /guard/health
        s = _state()
        if str(x.get('_strat') or 'A/B') not in ('A/B', 'A/B-shallow'):
            return (False, 'strategy_not_allowed')
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
                # v31.4 (operator): late_day starts at 16:00 ET — margin default 35 -> 4
                # (16:04 deadline - 4 = 16:00 on normal days; early-close days still scale down
                # because the margin rides on the calendar deadline, not a fixed clock time).
                # NOTE: an entry at 15:59 has 11 minutes before the 16:10 MFF force-flatten; the
                # 4y band 15:29-16:00 modelled +$8,548 on 27 trades, but median resolution is
                # 62 min and only 11/27 finish inside 24 min — live, the flatten decides the rest.
                cutoff = (dl - _envi('GUARD_ENTRY_MARGIN_MIN', 4)) if dl else None
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
        if broker_feedback is not None and broker_feedback.feedback_required():
            if not os.environ.get('BROKER_CALLBACK_TOKEN'):
                return (False, 'broker_callback_token_missing')
            if not broker_feedback.is_fresh():
                return (False, 'broker_state_stale')
            if s.get('eod_cleanup'):
                return (False, 'cleanup_pending')
            if broker_feedback.broker_open() or broker_feedback.has_live_commitment():
                return (False, 'broker_commitment_open')
        elif _env('BROKER_FEEDBACK_REQUIRED', '1') == '1':
            return (False, 'broker_feedback_unavailable')
        ep = eval_progress()                                          # eval over? stop trading it
        if ep['passed']:   _latch('target_hit_6pct', hard=True);  return (False, 'target_hit')
        if ep['breached']: _latch('dd_breached', hard=True);      return (False, 'dd_breached')
        if ep.get('awaiting_pass_confirmation'):
            return (False, 'awaiting_pass_confirmation')
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

        if _active_pending_group(s):             return (False, 'group_pending')
        d = _day_stats()
        if d['openpos']:                        return (False, 'position_open')     # blocks OTHER setups; siblings share one batch
        pb = _peer_busy()                                                           # v27.2: one position across ALL
        if pb:                                  return (False, pb)                   # services sharing this broker acct
        if d['sent']   >= _envi('MAX_TRADES_DAY', 3):   return (False, 'max_trades_day')
        if d['losses'] >= _envi('DAY_LOSS_N', 2):
            _latch('day_loss_n');               return (False, 'day_loss_n')        # primary floor guard
        if d['net']    <= -_envf('DAY_LOSS_USD', 1000):
            _latch('day_loss_usd');             return (False, 'day_loss_usd')
        if d['net']    >=  _envf('DAY_TARGET_USD', 1500):
            _latch('profit_lock');              return (False, 'profit_lock')       # consistency-safe green stop

        # Include the worst-case SL of the group being authorized now. Looking
        # only at realized P&L could approve a setup whose full stop would
        # already exceed the daily USD limit.
        planned_day_risk = float(x.get('_planned_group_risk_usd') or 0.0)
        if (planned_day_risk > 0
                and d['net'] - planned_day_risk <= -_envf('DAY_LOSS_USD', 1000)):
            return (False, 'projected_day_loss')

        eq = _modeled_equity(s, d)                                              # absolute sync + post-sync delta
        buffer_now = eq - _dd_floor()
        dd_buffer = _envf('DD_BUFFER', 800)
        prox_mode = _dd_proximity_mode()
        x['_dd_buffer_now'] = round(buffer_now, 2)
        x['_dd_proximity_mode'] = prox_mode
        if prox_mode != 'off' and buffer_now < dd_buffer:
            if prox_mode == 'hard':
                _latch('dd_proximity', hard=True)
            # soft mode blocks only this setup. It does NOT persist a halt, flatten, or
            # count blocked/session signals as trades; it auto-resumes once the buffer recovers.
            return (False, 'dd_proximity')
        if _env('DD_PROJECTED_RISK', '1') == '1':
            planned = float(x.get('_planned_group_risk_usd') or 0.0)
            if planned <= 0:
                # A missing frozen plan means the exact final quantities are unknown.
                # Fail closed instead of underestimating a two-leg A/B + shallow group.
                return (False, 'planned_group_risk_missing')
            planned += max(0.0, _envf('DD_PROJECTED_EXTRA_USD', 0.0))
            projected = buffer_now - planned
            x['_projected_buffer_after_risk'] = round(projected, 2)
            x['_planned_group_risk_usd'] = round(planned, 2)
            if projected < dd_buffer:
                return (False, 'projected_dd_risk')
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
    if _env('CHALLENGE_MODE', '1') == '1':
        return None
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
        gid = x.get('_setup_group_id')
        group_already_sent = bool(gid and any(
            g.get('setup_group_id') == gid and g.get('decision') in ('sent', 'manual')
            for g in glog[-200:]))
        if decision == 'blocked':
            day = _today()
            if reason == 'duplicate':
                # Do not create a second fake trade row. Fold the re-detection into
                # the original sent/manual setup and preserve alternate target info.
                for g in reversed(glog):
                    if g.get('date') != day: continue
                    same_group = bool(gid and g.get('setup_group_id') == gid)
                    same_key = g.get('key') == k
                    if g.get('decision') in ('sent', 'manual') and (same_group or same_key):
                        g['duplicate_count'] = int(g.get('duplicate_count') or 0) + 1
                        g['last_duplicate_ts'] = _now_ms()
                        alts = list(g.get('candidate_tps') or [])
                        tp0 = x.get('TP')
                        if tp0 is not None and tp0 not in alts: alts.append(tp0)
                        g['candidate_tps'] = alts[-8:]
                        _save(GLOG, glog); return True
            # Any other identical blocked row is stored once per day/reason.
            for g in reversed(glog[-100:]):
                if g.get('date') != day: break
                if g.get('key') == k and g.get('decision') == 'blocked' and g.get('reason') == reason:
                    return True
        glog.append(dict(key=k, strat=x.get('_strat', 'A/B'), setup_group_id=gid,
                         ts=_now_ms(), bar_ms=int(x.get('bos_ms') or 0), date=_today(),
                         et=_et(_now_ms()).strftime('%Y-%m-%d %H:%M'),
                         sess=_sess_of(x), dir=x.get('dir'), entry=x.get('entry'), sl=x.get('SL'),
                         sl_src=x.get('sl_src'),   # v29.1: which anchor set the stop (struct | fvg_edge | fvg_edge+capped)
                         tp_src=x.get('tp_src'),   # v30: which rule set the target (swing | 2R)
                         legs=x.get('_legs'),      # v30: the brackets actually sent (qty+tp per leg; None pre-v30/blocked)
                         tp=(x.get('_exec_tp') if x.get('_exec_tp') is not None else x.get('TP')),
                         qty=(x.get('_sent_qty') if x.get('_sent_qty') is not None
                                              else x.get('_exec_qty_override')),
                         planned_group_risk_usd=x.get('_planned_group_risk_usd'),
                         projected_buffer_after_risk=x.get('_projected_buffer_after_risk'),
                         execution_id=x.get('_execution_id'),
                         batch_group_id=x.get('_batch_group_id'),
                         rollback_confirmed=x.get('_rollback_confirmed'),
                         decision=decision, reason=reason))
        _save(GLOG, glog)
        if decision in ('sent', 'manual'):
            if not group_already_sent:
                s = _state(); s['sent_total'] = int(s.get('sent_total', 0)) + 1; _set_state(s)
            _trade_alert(x, decision)                      # 🟢 push a Telegram line the moment auto places it
        return True
    except Exception as e:
        print('[guard] note err', e, flush=True)
        return False

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
        _legs = x.get('_legs') or []
        _lt = (' [%s]' % ' + '.join('%s@%s' % (l.get('qty'), l.get('tp')) for l in _legs)) if len(_legs) > 1 else ''
        msg = ('\U0001f7e2 %s · %s %s %s @ %s · SL %s (%s) / TP %s (%s)%s'
               % (tag, _sess_of(x), x.get('dir'), qtxt, x.get('entry'), x.get('SL'),
                  x.get('sl_src') or '?', x.get('TP'), x.get('tp_src') or '?', _lt))
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

def news_calendar_check(cal_age_h=None, market_open=None):
    """Persist calendar freshness and alert before the first blocked setup."""
    try:
        s = _state(); s['news_cal_checked_ms'] = _now_ms()
        s['news_cal_age_h'] = None if cal_age_h is None else round(float(cal_age_h), 2)
        strict = _env('NEWS_GUARD', '1') == '1' and _env('NEWS_STRICT', '1') == '1'
        stale = strict and (cal_age_h is None or float(cal_age_h) > _envf('NEWS_MAX_AGE_H', 24))
        was = bool(s.get('news_cal_alerted'))
        s['news_cal_alerted'] = bool(stale and market_open)
        _set_state(s)
        if _env('NEWS_CAL_ALERTS', '1') != '1' or not market_open or stale == was:
            return not stale
        url = os.environ.get('GUARD_ALERT_URL') or os.environ.get('WEBHOOK_URL')
        if url and requests is not None:
            if stale:
                msg = '⚠️ NEWS CALENDAR STALE/UNAVAILABLE — AUTO will block new orders (NEWS_STRICT=1).'
            else:
                msg = '✅ NEWS CALENDAR RECOVERED — news guard can verify events again.'
            try: requests.post(url, json={'text': msg, 'raw': msg}, timeout=6)
            except Exception: pass
        return not stale
    except Exception as e:
        print('[guard] news_calendar_check err', e, flush=True); return False


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
        pg = _active_pending_group(s)
        if pg:
            add('batch', 'warn', 'setup batch pending: %s (accepted=%s)' %
                (pg.get('group_id'), len(pg.get('accepted') or [])))
        else:
            add('batch', 'ok', 'no unresolved sibling batch')
        # 4 feed freshness (degraded, not fatal — sends just abort until fresh)
        if fa is None:
            add('feed', 'warn', 'feed age unknown (gate not consulted yet)')
        elif float(fa) > stale_min:
            add('feed', 'warn', 'feed %.0fm old > STALE_MIN %.0fm — sends aborting (%s)' % (float(fa), stale_min, fa_src))
        else:
            add('feed', 'ok', 'fresh (%.0fm)' % float(fa))
        if _env('NEWS_GUARD', '1') == '1' and _env('NEWS_STRICT', '1') == '1':
            ca = s.get('news_cal_age_h')
            if ca is None:
                add('news_cal', 'warn', 'calendar never verified — AUTO blocks until fetched')
            elif float(ca) > _envf('NEWS_MAX_AGE_H', 24):
                add('news_cal', 'warn', 'calendar %.1fh old — AUTO blocks' % float(ca))
            else:
                add('news_cal', 'ok', 'fresh (%.1fh)' % float(ca))
        else:
            add('news_cal', 'info', 'strict calendar guard disabled')
        # 5 broker truth freshness — AUTO never falls back to shadow/model state.
        if broker_feedback is not None and broker_feedback.feedback_required():
            bs = broker_feedback.status()
            add('broker', 'ok' if bs.get('fresh') else 'critical',
                ('fresh, pos=%s working=%s' % (bs.get('position_qty'), bs.get('working_orders_count')))
                if bs.get('fresh') else ('STALE/UNKNOWN broker state: ' + str(bs.get('error') or bs.get('age_sec'))))
        else:
            et = s.get('equity_ts', 0)
            if not et:
                add('equity_sync', 'warn', 'equity never synced — model fallback active')
            else:
                ah = (_mins_since(et) or 0) / 60.0
                add('equity_sync', 'warn' if ah > 36 else 'ok', 'synced %.0fh ago' % ah)
        # 6 drawdown floor is broker supplied or explicitly configured.
        _bfloor = None
        if broker_feedback is not None:
            try: _bfloor = broker_feedback.truth().get('drawdown_floor')
            except Exception: pass
        add('dd_floor', 'ok' if (_bfloor is not None or os.environ.get('DD_FLOOR')) else 'critical',
            ('broker floor=' + str(_bfloor)) if _bfloor is not None else
            (('DD_FLOOR=' + os.environ['DD_FLOOR']) if os.environ.get('DD_FLOOR') else 'drawdown floor unavailable'))
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
        # fired vs actually-filled, all-time over the visible book (v27.3c): 'fired' = every SENT/ARMED
        # row; 'filled' = those that really held a position (win/loss/timeout incl. reconciled)
        _sent_rows = [b for b in book if b.get('decision') in ('sent', 'manual')]
        fired_n = len(_sent_rows)
        filled_n = sum(1 for b in _sent_rows if b.get('outcome') in ('win', 'loss', 'timeout'))
        return jsonify(mode=exec_mode(), auto=os.environ.get('AUTO_SUBMIT', '0') == '1', kill=_kill_active(s),
                       kill_reason=s.get('kill_reason', ''), day=_today(), eval=eval_progress(),
                       openpos=bool(d.get('openpos')),   # v27.2: peers sharing this broker account read this
                       be=(os.environ.get('MANAGE_BE', '') == '1'), skip=_env('SKIP_SESSIONS', 'LO,ASIA,PREM,NYL'),
                       trades=d['sent'], max_trades=_envi('MAX_TRADES_DAY', 3),
                       losses=d['losses'], loss_n=_envi('DAY_LOSS_N', 2), loss_count_mode=d.get('loss_mode','group'),
                       day_net=round(d['net']), day_target=_envf('DAY_TARGET_USD', 1500),
                       equity_synced=round(float(s.get('equity') or 0), 2),
                       equity_sync_day=s.get('equity_sync_day', ''),
                       equity_day_net_at_sync=round(float(s.get('equity_day_net_at_sync') or 0), 2),
                       ramp_left=max(0, _envi('RAMP_TRADES', 3) - s.get('sent_total', 0)),
                       pending_group=_active_pending_group(s),
                       projected_dd_check=_env('DD_PROJECTED_RISK', '1') == '1',
                       dd_proximity_mode=_dd_proximity_mode(),
                       broker=(broker_feedback.status() if broker_feedback is not None else {'fresh': False}),
                       fired=fired_n, filled=filled_n,
                       health=health(), pine_days=book_days(), book=book)

    def _authed():
        """GUARD_TOKEN set -> every MUTATING endpoint requires ?t=<token>. These are open GETs on a
        public URL; without this, anyone who finds the URL can clear the hard latch or arm AUTO."""
        tok = os.environ.get('GUARD_TOKEN', '')
        return (not tok) or (request.args.get('t', '') == tok)

    def _sync():
        if not _authed(): return jsonify(ok=False, err='auth'), 401
        try:
            # The caller always supplies ABSOLUTE broker equity. Record today's guard P&L
            # alongside it, so eval_progress adds only P&L accrued after this snapshot.
            v = float(request.args.get('equity'))
            d = _day_stats()
            s = _state(); s['equity'] = v; s['equity_ts'] = _now_ms()
            s['equity_sync_day'] = _today()
            s['equity_day_net_at_sync'] = float(d.get('net') or 0.0)
            s['eq_high'] = max(float(s.get('eq_high') or 0), v)   # feeds the auto-trailing DD floor
            _set_state(s)
            return jsonify(ok=True, equity=v, modeled_equity=round(_modeled_equity(s, d), 2),
                           day_net_at_sync=round(s['equity_day_net_at_sync'], 2),
                           sync_day=s['equity_sync_day'], eq_high=s['eq_high'], floor=_dd_floor())
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
        if _env('CHALLENGE_MODE', '1') == '1':
            return jsonify(ok=False, err='external strategies disabled in challenge mode'), 403
        if not _authed(): return jsonify(ok=False, err='auth'), 401
        b = request.get_json(force=True, silent=True) or {}
        glog = _load(GLOG, [])
        if b.get('key') and b.get('ext_outcome'):                     # outcome update by key
            for g0 in reversed(glog):
                if g0.get('key') == b['key']:
                    if g0.get('ext_outcome') == 'canceled':           # v27.2e: a user cancel is FINAL —
                        return jsonify(ok=True, kept='canceled')      # the satellite's later model verdict
                    g0['ext_outcome'] = b['ext_outcome']; g0['ext_net'] = b.get('ext_net')   # can't overwrite it
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

    def _reconcile():
        """v27.3 — paste the Tradovate Performance CSV, the book overwrites MODEL outcomes with BROKER
        reality. POST /guard/reconcile?t=<token>, body = raw CSV text (or form field 'csv').
        Matching: direction from fill order (sold-first = SHORT), entry = first-leg price; a book SENT
        row matches when |entry diff| <= 2.0 pts (qty equality preferred). Matched rows get
        ext_outcome win/loss (pnl sign) + ext_net = broker pnl — from then on day counters, the
        2-loss halt and modeled equity run on real fills, not the touch-fill model. User 'canceled'
        stamps are preserved. Returns matched / unmatched so nothing fails silently."""
        if not _authed(): return jsonify(ok=False, err='auth'), 401
        try:
            raw = (request.form.get('csv') or request.get_data(as_text=True) or '').strip()
            if not raw: return jsonify(ok=False, err='empty body'), 400
            import csv as _csv, io as _io, datetime as _dt
            fills = []
            for r in _csv.DictReader(_io.StringIO(raw)):
                try:
                    qty = int(float(r.get('qty') or 0))
                    bp, sp = float(r['buyPrice']), float(r['sellPrice'])
                    pnl_s = (r.get('pnl') or '').replace('$', '').replace(',', '').strip()
                    pnl = -float(pnl_s.strip('()')) if pnl_s.startswith('(') else float(pnl_s or 0)
                    bt = _dt.datetime.strptime(r['boughtTimestamp'], '%m/%d/%Y %H:%M:%S')
                    st = _dt.datetime.strptime(r['soldTimestamp'], '%m/%d/%Y %H:%M:%S')
                    short = st < bt
                    fills.append(dict(qty=qty, dir='SHORT' if short else 'LONG',
                                      entry=(sp if short else bp), exit=(bp if short else sp),
                                      pnl=pnl, t=min(bt, st).isoformat()))
                except Exception as fe:
                    print('[guard] reconcile row skip:', fe, flush=True)
            glog = _load(GLOG, []); matched = []; unmatched = []
            sm = _shadow_by_key()                     # so the summary can show model->broker deltas
            used = set()
            for f in fills:
                best = None; bestd = 99.0
                for i in range(len(glog) - 1, -1, -1):
                    g = glog[i]
                    if i in used or g.get('decision') not in ('sent', 'manual'): continue
                    if g.get('ext_outcome') == 'canceled': continue
                    if g.get('dir') != f['dir'] or g.get('entry') is None: continue
                    dpx = abs(float(g['entry']) - f['entry'])
                    if dpx > 2.0: continue
                    score = dpx + (0 if (g.get('qty') and int(g['qty']) == f['qty']) else 0.5)
                    if score < bestd: bestd = score; best = i
                if best is None:
                    unmatched.append(f); continue
                g = glog[best]; used.add(best)
                prior = _actualize(g, sm.get(g.get('key'), {}))        # model verdict BEFORE overwrite
                g['model_outcome'] = prior.get('outcome'); g['model_net'] = prior.get('net')  # keep BOTH:
                g['ext_outcome'] = 'win' if f['pnl'] > 0 else ('loss' if f['pnl'] < 0 else 'timeout')
                g['ext_net'] = round(f['pnl']); g['outcome'] = g['ext_outcome']; g['reconciled'] = True
                matched.append(dict(key=g.get('key'), et=g.get('et'), broker_pnl=round(f['pnl']),
                                    outcome=g['ext_outcome'],
                                    was_outcome=prior.get('outcome'), was_net=prior.get('net')))
            _save(GLOG, glog)
            print('[guard] reconcile: %d matched, %d unmatched' % (len(matched), len(unmatched)), flush=True)
            return jsonify(ok=True, matched=matched, unmatched=unmatched,
                           note='matched rows now carry BROKER outcomes; sync equity too: /guard/sync?equity=<real>')
        except Exception as e:
            return jsonify(ok=False, err=str(e)), 500

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
    app.add_url_rule('/guard/reconcile', 'guard_reconcile', _reconcile, methods=['POST'])
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
<div class=sub>MFF Pro-100k eval · resting-limit A/B · fail-closed · MANUAL = review only / no broker orders · stale-data aborts AUTO send · <span id=day></span> · <span class=g>sync real equity: /guard/sync?equity=NNNNN · health JSON: /guard/health</span></div>
<div id=healthbar class=sub style="margin:-6px 0 12px;font-weight:600"></div>
<div class=modebar>
 <span class=lb>MODE</span>
 <a class="btn mauto" href="/guard/mode?set=auto" onclick="return flip('auto')">AUTO</a>
 <a class="btn mmanual" href="/guard/mode?set=manual" onclick="return flip('manual')">MANUAL (review only — no orders)</a>
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
<table><thead><tr><th>Strat</th><th>Time ET</th><th>Sess</th><th>Dir</th><th>Entry</th><th>SL</th><th>SL type</th><th>TP</th><th>TP type</th><th>Qty</th><th>Decision</th><th>Outcome</th><th>R</th><th>Net$ (model)</th><th>Real$ (broker)</th></tr></thead><tbody id=tb></tbody></table>
<div class="pinep">
 <div class="ph">🧾 <b>Reconcile with broker</b> — paste OR drag &amp; drop the Tradovate Performance CSV; matched rows switch from model outcomes to REAL fills
  <input type="file" id="recfile" accept=".csv,text/csv" style="display:none" onchange="recFile(this.files)">
  <button class="cpy" onclick="document.getElementById('recfile').click();return false">Choose file</button>
  <button class="cpy" onclick="return doRec(this)">Reconcile</button>
  <span class="g" id="recout">day counters + 2-loss halt then run on broker truth</span>
 </div>
 <textarea id="recbox" placeholder="drop the Performance .csv here, or paste its contents" style="min-height:70px"
  ondragover="event.preventDefault();this.style.borderColor='#3987e5'"
  ondragleave="this.style.borderColor=''"
  ondrop="event.preventDefault();this.style.borderColor='';recFile(event.dataTransfer.files)"></textarea>
 <div id="rectab"></div>
</div>
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
  ['Ramp · BE',(d.ramp_left>0?(d.ramp_left+'@1'):'sized')+' · BE '+(d.be?'ON@1R':'off')],
  ['Fired · Filled',(d.fired||0)+' · '+(d.filled||0)+' <span style="font-size:11px;color:#8a93a6">('+
    (d.fired?Math.round(100*(d.filled||0)/d.fired):0)+'% fill)</span>']
 ].map(c=>'<div class=c><div class=l>'+c[0]+'</div><div class=v>'+c[1]+'</div></div>').join('');
 let tpsrc=x=>{let v=x.tp_src||'';let legs=(x.legs&&x.legs.length>1)?(' · '+x.legs.length+' legs'):'';
  if(!v)return '<span style="color:#6b7688">—</span>';
  let lab=v=='swing'?'SWING':v=='shallow_3R'?'SHALLOW 3R':v=='shallow_2R'?'SHALLOW 2R':'2R';let col=v=='swing'?'#3ecb3e':v.indexOf('shallow_')===0?'#60a5fa':'#3987e5';
  let tt=x.legs?x.legs.map(function(l){return l.qty+'@'+l.tp;}).join(' + '):'';
  return '<span style="color:'+col+'" title="v30 target: last confirmed swing beyond 1R (capped 3R) or fixed 2R fallback. Brackets: '+tt+'">'+lab+legs+'</span>';};
 let slsrc=x=>{let v=x.sl_src||'';if(!v)return '<span style="color:#6b7688">—</span>';
  let lab=v=='struct'?'STRUCT':v=='fvg_edge'?'FVG edge':v=='fvg_edge+capped'?'FVG edge · capped 40':v;
  let col=v=='struct'?'#3ecb3e':v.indexOf('capped')>-1?'#e0a93b':'#3987e5';
  return '<span style="color:'+col+'" title="v29 stop anchor: struct when the displacement-leg extreme is within 30pt, else the far edge of the held FVG; re-anchored to MAX_STOP_R when wider">'+lab+'</span>';};
 let dec=x=>{let dup=x.duplicate_count?(' · dup×'+x.duplicate_count):'';
  return x.decision=='sent'?('<span class=sent>SENT'+(x.qty?(' ×'+x.qty):'')+dup+'</span>'):x.decision=='manual'?('<span class=sent>ARMED'+(x.qty?(' ×'+x.qty):'')+dup+'</span>'):('<span class=blk>BLOCK: '+(x.reason||'')+'</span>');};
 let oc=x=>{let o=x.outcome||'';let c=o=='win'?'win':o=='loss'?'loss':o=='open'?'open':'g';
  let h='<span class='+c+'>'+o+'</span>';
  if(o=='open'&&(x.decision=='sent'||x.decision=='manual'))
   h+=' <a href="#" data-k="'+encodeURIComponent(x.key||'')+'" title="I canceled this order at the broker — mark it canceled and free the slot" onclick="return cancelRow(this.dataset.k)" style="color:#e0a93b;text-decoration:none">✕</a>';
  return h;};
 let fired=x=>x.decision=='sent'||x.decision=='manual';
 window._dec=dec;window._oc=oc;window._fired=fired;window._slsrc=slsrc;window._tpsrc=tpsrc;window._book=d.book||[];
 renderBook();
 let ps=document.getElementById('pineday');
 let opts='<option value="">all trades</option>'+((d.pine_days||[]).map(dd=>'<option value="'+dd+'">'+dd+'</option>').join(''));
 if(ps.dataset.sig!==opts){let cur=ps.value;ps.innerHTML=opts;ps.dataset.sig=opts;if(cur)ps.value=cur;loadPine();}
}
let _filter='all';
function setf(f){_filter=f;document.querySelectorAll('.btn.fall,.btn.fsent,.btn.fblk').forEach(b=>b.classList.remove('act'));
 document.querySelector('.btn.f'+(f=='all'?'all':f=='sent'?'sent':'blk')).classList.add('act');renderBook();return false;}
function renderBook(){
 let dec=window._dec,oc=window._oc,fired=window._fired,slsrc=window._slsrc||(x=>''),tpsrc=window._tpsrc||(x=>'');if(!dec)return;
 let rows=(window._book||[]).filter(x=>_filter=='all'||(_filter=='sent'?fired(x):!fired(x)));
 document.getElementById('tb').innerHTML=rows.map(x=>{
  let isB=!fired(x);                         // blocked rows never traded -> model-priced R/Net$, render gray
  let rc=isB?' style="color:#6b7688" title="model-priced — trade was NOT executed"':'';
  let rec=!!x.reconciled;
  let rv=(x.R!=null?x.R:'');
  let nv=rec?(x.model_net!=null?x.model_net:''):(x.net!=null?x.net:'');   // Net$ column = MODEL verdict
  let real=rec?('<b>'+(x.net!=null?x.net:'')+'</b> <span title="broker-reconciled" style="color:#3ecb3e">✓</span>'):'';
  if(isB&&(rv!==''||nv!=='')){rv=rv!==''?('('+rv+')'):'';nv=nv!==''?('('+nv+')'):'';}
  let mc=rec?' style="color:#6b7688" title="model verdict — see Real$ for the broker result"':'';
  return '<tr><td><b>'+(x.strat||'A/B')+'</b></td><td>'+(x.et||'')+'</td><td>'+(x.sess||'')+'</td><td>'+(x.dir||'')+
  '</td><td>'+(x.entry||'')+'</td><td>'+(x.sl||'')+'</td><td>'+slsrc(x)+'</td><td>'+(x.tp||'')+'</td><td>'+tpsrc(x)+'</td><td>'+(x.qty||'')+'</td><td>'+dec(x)+'</td><td'+(isB?rc:'')+'>'+oc(x)+
  '</td><td'+rc+'>'+rv+'</td><td'+(isB?rc:mc)+'>'+nv+'</td><td>'+real+'</td></tr>';}).join('');
}
let _tok=new URLSearchParams(location.search).get('t');
try{ if(_tok) localStorage.setItem('guard_t',_tok); else _tok=localStorage.getItem('guard_t'); }catch(e){}
const _t=_tok?('&t='+encodeURIComponent(_tok)):'';
async function flip(m){await fetch('/guard/mode?set='+m+_t,{cache:'no-store'});load();return false;}
async function dokill(){await fetch('/guard/kill?on=1'+_t,{cache:'no-store'});load();return false;}
async function cancelRow(k){let r=await (await fetch('/guard/cancel?key='+k+(_t||''),{cache:'no-store'})).json();
 if(!r.ok){document.getElementById('healthbar').textContent='⚠ cancel: '+(r.err||'failed');}load();return false;}
function recFile(files){if(!files||!files.length)return false;
 let f=files[0];let rd=new FileReader();
 rd.onload=e=>{document.getElementById('recbox').value=e.target.result;
  document.getElementById('recout').textContent='loaded '+f.name+' ('+f.size+' B) — click Reconcile';};
 rd.readAsText(f);return false;}
async function doRec(btn){let b=document.getElementById('recbox').value.trim();let o=document.getElementById('recout');
 if(!b){o.textContent='paste the CSV first';return false;}
 btn.textContent='...';
 try{let r=await (await fetch('/guard/reconcile?x=1'+(_t||''),{method:'POST',body:b,cache:'no-store'})).json();
  if(r.ok){
   o.textContent='✓ matched '+r.matched.length+' · unmatched broker fills '+r.unmatched.length+' — now sync real equity!';
   let tot=r.matched.reduce((a,m)=>a+(m.broker_pnl||0),0);
   let rows=r.matched.map(m=>'<tr><td>'+m.et+'</td><td>'+(m.was_outcome||'?')+' → <b>'+m.outcome+'</b></td>'+
     '<td style="text-align:right">'+(m.was_net!=null?('('+m.was_net+')'):'—')+' → <b>'+m.broker_pnl+'</b></td></tr>').join('');
   let un=r.unmatched.map(u=>'<tr><td>'+(u.t||'')+'</td><td>'+u.dir+' '+u.qty+'× @ '+u.entry+'</td>'+
     '<td style="text-align:right">'+Math.round(u.pnl)+' (no book row!)</td></tr>').join('');
   document.getElementById('rectab').innerHTML='<table style="margin-top:8px;font-size:12px">'+
     '<thead><tr><th>Time ET</th><th>Outcome model → broker</th><th>Net$ model → broker</th></tr></thead>'+
     '<tbody>'+rows+un+'</tbody><tfoot><tr><td></td><td><b>broker total</b></td>'+
     '<td style="text-align:right"><b>'+Math.round(tot)+'</b></td></tr></tfoot></table>';
  } else o.textContent='⚠ '+(r.err||'failed');
 }catch(e){o.textContent='⚠ '+e;}
 btn.textContent='Reconcile';load();return false;}
async function doarm(){await fetch('/guard/kill?on=0'+_t,{cache:'no-store'});load();return false;}
async function loadPine(){let day=document.getElementById('pineday').value;
 let t=await (await fetch('/guard/pine?day='+encodeURIComponent(day),{cache:'no-store'})).text();
 document.getElementById('pinebox').value=t;}
function copyPine(btn){let b=document.getElementById('pinebox');b.select();navigator.clipboard.writeText(b.value);
 btn.textContent='Copied ✓';setTimeout(()=>{btn.textContent='Copy script';},1200);}
load();setInterval(load,30000);
</script></body></html>"""
