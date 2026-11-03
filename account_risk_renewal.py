"""Same-day risk renewal core. Called only by explicit authenticated operator actions.

prepare writes inactive candidates only. install requires interactive consent.
No broker orders, policy approvals, environment or ledger edits are performed.
Renewal can unblock ALL already-armed strategies sharing this evidence file.
"""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import fcntl

NY = ZoneInfo('America/New_York')
VERSION = 'RISK_RENEWAL_WEB_V1'
ROOT = Path('/data/builder50/operator/risk_renewal')
PHRASE = 'RENEW SHARED ACCOUNT RISK FOR TODAY'
BINDING = 'bdfeee63d33d2d6c18b657a283cb9e65e495aa76e9f7f5e36f1993b6e7b39dfa'

def require(ok, why):
    if not ok:
        raise ValueError(why)

def digest(b): return hashlib.sha256(b).hexdigest()
def encoded(q): return json.dumps(q, sort_keys=True, indent=2, allow_nan=False).encode()+b'\n'
def read(p): return json.loads(Path(p).read_bytes())
def now_ms(): return int(time.time()*1000)

def atomic(p, b):
    p = Path(p)
    tmp = p.with_name(p.name+'.renew-'+str(os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(b); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if tmp.exists(): tmp.unlink()

def window(at):
    d = datetime.fromtimestamp(at/1000, NY)
    require(d.weekday()<5, 'Renew only on the intended weekday trading date.')
    expiry = int(d.replace(hour=15, minute=55, second=0, microsecond=0).timestamp()*1000)
    require(at < expiry, 'Today\'s 15:55 New York deadline has passed.')
    return expiry

def check_rules(q):
    require(q.get('provider')=='MFFU' and q.get('account_binding')==BINDING
            and q.get('account_suffix')=='5021' and q.get('account_stage')=='Evaluation', 'Unexpected account identity.')
    require(q.get('verified') is True and q.get('account_controls_source')=='CROSSTRADE_RECONCILED', 'Unexpected evidence method.')
    require((q.get('minimum_balance'), q.get('max_micro_contracts'), q.get('daily_loss_policy'), q.get('daily_loss_limit_usd'))
            == (48161.90, 40, 'NONE', None), 'Account rules changed; obtain a reviewed rule update instead of renewing.')
    require(q.get('margin_policy',{}).get('mode')=='CONTRACT_CAP_ONLY', 'Margin rule changed; stop.')

def candidate(q, at):
    check_rules(q)
    expiry = window(at)
    out = copy.deepcopy(q)
    ref = 'ACCOUNT_HOLDER_RECONFIRMATION_'+datetime.fromtimestamp(at/1000, NY).isoformat()
    for row in (out, out['margin_policy']):
        row.update(observed_at_ms=at, expires_at_ms=expiry, source_reference=ref,
                   confirmation_source='ACCOUNT_HOLDER', independently_provider_verified=False)
    out['provider_rules_independently_verified'] = False
    out['validity_note'] = 'Same-day account-holder reconfirmation; fresh broker accounting required. Expires '+datetime.fromtimestamp(expiry/1000, NY).isoformat()
    return out

def context():
    release = os.environ.get('RAILWAY_GIT_COMMIT_SHA','')
    require(len(release)==40 and all(c in '0123456789abcdef' for c in release), 'Unknown release; stop.')
    for key, value in {'V3_EXIT_REAL_EXECUTION':'false', 'V3_EXIT_MODE':'shadow',
                       'AB_MANAGER_MODE':'shadow', 'V3_RUNNER_ENABLED':'false'}.items():
        require(os.getenv(key,value)==value, 'Advanced exit permissions differ; stop.')
    risk = Path(os.environ['MFFU_RISK_EVIDENCE_FILE'])
    paths = {k: os.environ[k] for k in ('ICT_EXECUTION_POLICY_FILE','DIRECTIONAL_EXECUTION_POLICY_FILE')}
    policies = {k: {'path':v,'sha256':digest(Path(v).read_bytes())} for k,v in paths.items()}
    p = read(paths['ICT_EXECUTION_POLICY_FILE'])
    require(p.get('approved') is True, 'ICT policy must already be approved; this tool cannot approve it.')
    import execution_validation as gate
    import ab_v3_live
    gate.validate_parameters(p); gate.costs(p)
    why = ab_v3_live.native_bracket_readiness(p)
    require(why is None, 'Native protection check: '+str(why))
    # Compare configuration without printing secrets or persisting their values.
    names = [k for k in os.environ if k.startswith(('EXEC_', 'ICT_', 'AB_', 'V3_', 'BUILDER_', 'DAY_', 'LOSS_', 'DD_', 'MFFU_', 'SETUP_'))]
    env_hash = digest(encoded({k:os.environ[k] for k in sorted(names)}))
    return risk, {'release':release, 'renewal_version':VERSION, 'risk_path':str(risk), 'policies':policies, 'settings_sha256':env_hash}

def losses(c, at):
    n = int(os.environ['DAY_LOSS_N'])
    streak = int(os.environ.get('LOSS_STREAK_N','3'))
    hours = float(os.environ.get('LOSS_STREAK_COOLDOWN_H','24'))
    require(n>=0 and streak>=0 and math.isfinite(hours) and hours>0, 'Invalid loss-rule settings.')
    require(not n or c['losses_today']<n, 'Existing one-loss daily stop is active.')
    if streak and c['consecutive_losses']>=streak:
        last = c.get('last_loss_closed_ms')
        require(type(last) in (int,float) and math.isfinite(last) and 0<last<=at, 'Unknown loss history.')
        require(at>=last+hours*3600000, 'Existing loss-streak cooldown remains active.')

def assess(q):
    import builder_broker_sync as broker
    import directional_account_reconciliation as rec
    import directional_account_limits as limits
    sync = broker.instance()
    require(sync.binding==BINDING, 'Broker account mismatch.')
    require(sync.config.get('AB_V3_CONTRACT')==q['margin_policy'].get('contract'), 'Contract mismatch; review contract rollover.')
    # Wait for the normal poller; never loop provider requests on failures.
    deadline = time.monotonic()+50
    while True:
        s = sync.snapshot()
        if s and s['read_started_at']*1000>=q['observed_at_ms'] and sync.state().get('current'): break
        require(time.monotonic()<deadline, 'No fresh broker snapshot after confirmation; try again after broker recovery.')
        time.sleep(1)
    rows = rec.history(sync)
    margin = rec.margin_row(sync)
    s = sync.snapshot()
    require(sync.state().get('current'), 'Broker is not CURRENT; no renewal installed.')
    result = rec.assess(s, rows, margin, q, sync.binding, sync.config.get('AB_V3_CONTRACT'), now_ms())
    require(result.get('verified') is True, 'Account check blocked: '+', '.join(result.get('missing',[])))
    losses(result['controls'], now_ms())
    floor, reserve = limits.floor_and_reserve(q)
    require(min(result['controls']['cash'],result['controls']['equity'])>floor+reserve, 'No drawdown headroom after reserve.')
    return result

def view():
    """Saved state only. Opening/refreshing the dashboard never polls CrossTrade."""
    import directional_account_reconciliation as rec
    risk = Path(os.environ['MFFU_RISK_EVIDENCE_FILE'])
    q = read(risk)
    check_rules(q)
    s = rec.state()
    import builder_broker_sync as broker
    matching = s.get('rules_sha256')==digest(broker.encode(q).encode())
    verified = s.get('verified') is True and matching and now_ms()<q['expires_at_ms']
    try: window(now_ms()); allowed=True
    except ValueError: allowed=False
    return dict(state='VERIFIED' if verified else 'EXPIRED' if now_ms()>=q['expires_at_ms'] else 'CHECK_REQUIRED',
                expires_at_ms=q['expires_at_ms'], can_prepare=allowed,
                rules_hash=digest(risk.read_bytes()),
                rules=dict(account='MFFU Evaluation …5021', floor=q['minimum_balance'], max_mnq=q['max_micro_contracts'],
                           daily_loss='No provider daily-loss limit', margin='Contract cap only; no separate per-contract margin'),
                blockers=s.get('missing',[]) if not verified else [],
                confirmation_source='ACCOUNT_HOLDER_NOT_PROVIDER_VERIFIED')

def prepare_stage(folder, expected_rules_hash):
    risk,ctx=context()
    original=risk.read_bytes()
    require(digest(original)==expected_rules_hash,'Account rules changed since you opened the form; reopen it.')
    q=candidate(json.loads(original),now_ms())
    result=assess(q)
    require(context()==(risk,ctx) and risk.read_bytes()==original,'Configuration changed during preparation.')
    m=dict(context=ctx, original_sha256=digest(original), candidate_sha256=digest(encoded(q)),
           prepared_at_ms=q['observed_at_ms'], expires_at_ms=q['expires_at_ms'])
    atomic(folder/'candidate.json',encoded(q));atomic(folder/'prepared.json',encoded(m))
    atomic(folder/'validation.json',encoded(result))
    return dict(equity=result['controls']['equity'],losses_today=result['controls']['losses_today'],
                consecutive_losses=result['controls']['consecutive_losses'], expires_at_ms=q['expires_at_ms'])

def draft(folder):
    risk,ctx=context();original=risk.read_bytes()
    m,q=read(folder/'prepared.json'),read(folder/'candidate.json')
    at=now_ms()
    require(window(at)==m['expires_at_ms'] and 0<=at-m['prepared_at_ms']<=15*60000,'Draft expired; start a new check.')
    require(ctx==m['context'] and digest(original)==m['original_sha256'] and digest(encoded(q))==m['candidate_sha256'],'Configuration changed; start a new check.')
    require(candidate(json.loads(original),m['prepared_at_ms'])==q,'Candidate changed rules beyond renewal metadata.')
    return risk,ctx,original,m,q

def await_runtime(q):
    import builder_broker_sync as broker
    import directional_account_reconciliation as rec
    target=digest(broker.encode(q).encode());deadline=time.monotonic()+60
    while True:
        s=rec.state()
        if s.get('verified') is True and s.get('rules_sha256')==target:
            losses(s['controls'],now_ms());return
        require(now_ms()<q['expires_at_ms'] and time.monotonic()<deadline,'Runtime did not verify renewed evidence: '+str(s.get('missing')))
        time.sleep(1)

def install_stage(folder):
    risk,ctx,original,m,q=draft(folder)
    assess(q)
    require(draft(folder)==(risk,ctx,original,m,q),'Configuration changed during final check.')
    backup=folder/'original-risk.json'
    require(not backup.exists(),'Installation already attempted; inspect saved state instead of retrying.')
    atomic(backup,original)
    atomic(risk,encoded(q))
    try:
        await_runtime(q)
        require(context()==(risk,ctx) and risk.read_bytes()==encoded(q),'Configuration changed during verification.')
    except Exception:
        if risk.read_bytes()==encoded(q):atomic(risk,original)
        raise ValueError('Renewal not confirmed. Original risk restored if unchanged. Check current status; any broker orders already submitted are not undone.')
    return dict(expires_at_ms=q['expires_at_ms'],state='APPLIED',policies_unchanged=True)

