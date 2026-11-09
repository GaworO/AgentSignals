"""Authenticated scanner recovery. No AI/provider calls, ledger edits or permissions.

Only the existing guarded worker may be woken/restarted. Uncertain attempts are
never cleared; diagnostics use saved local evidence and preserve every charge.
"""
import hmac
import json
import secrets
import sqlite3
import time
from datetime import datetime

import ict_news_context
import ict_shadow_research

COOKIE='__Host-ict-ai-recovery'
ORIGIN='https://agent-50k-production.up.railway.app'


def inspect(cloud, now=None):
    from ict_shadow_cloud import NY, session, session_call_limit, daily_call_limit, DAILY_USD
    now=time.time() if now is None else now
    day=datetime.fromtimestamp(now,NY).date().isoformat()
    with cloud.db() as db:
        saved={r['key']:json.loads(r['value']) for r in db.execute("SELECT * FROM state WHERE key IN ('worker','market','last_check','budget_anomaly','ai_recovery_action')")}
        costs=[dict(r) for r in db.execute('SELECT * FROM costs WHERE day=?',(day,))]
    worker=saved.get('worker',{});fresh=0<=now-worker.get('at',0)<180
    checks=[];blockers=[]
    def check(label,state,detail,blocks=False):
        checks.append(dict(label=label,state=state,detail=detail))
        if blocks:blockers.append(detail)
    enabled=cloud.config.get('ICT_SHADOW_ENABLED')=='true'
    check('Scanner','ok' if enabled and fresh else 'wait',
          'Running' if enabled and fresh else 'No recent heartbeat — recovery can restart a stopped worker.' if enabled else 'Disabled in settings; this button cannot enable it.',not enabled)
    try:
        with sqlite3.connect('file:'+str(cloud.root.parent/'ab_ai_context.sqlite3')+'?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row
            rows=[dict(r) for r in db.execute('SELECT id,status,requested_at,review_kind,finished,error_code FROM reviews WHERE day=?',(day,))]
            queued=db.execute("SELECT count(*) FROM setup_requests WHERE state IN ('queued','running') AND expires>?",(now,)).fetchone()[0]
        reason=ict_shadow_research.production_defer_reason(rows,now,independent_cloud=True)
        detail=('Production AI is running. Wait for it to finish.' if reason=='DEFER_PRODUCTION_AI_BUSY' else
                'Production request has no confirmed finish. Operator investigation required; do not resend.' if reason else
                'Production setup has priority. Rechecks continue automatically.' if queued else
                'Completed failures remain charged and do not block new ICT reviews.')
        check('Production requests','wait' if reason or queued else 'ok',detail,bool(reason or queued))
        research_path=cloud.root/('ict_2022_nyam_'+day.replace('-',''))/'research_budget.sqlite3'
        pending=[]
        if research_path.exists():
            with sqlite3.connect('file:'+str(research_path)+'?mode=ro',uri=True) as db:
                pending=db.execute("SELECT id,status FROM reviews WHERE day=? AND status IN ('research_inflight','research_unknown')",(day,)).fetchall()
        check('ICT requests','wait' if pending else 'ok',
              'An ICT request is running or unresolved. Its result must be reconciled; this button cannot clear it.' if pending else 'No unresolved ICT request.',bool(pending))
    except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):
        check('Saved requests','error','Saved request evidence is unavailable. Operator investigation required.',True)
    news=ict_news_context.build(cloud.config,now)
    window=session(now)
    allowed=bool(window) and news['status']=='CLEAR'
    detail=('Outside London / NYAM / NYPM. London runs Tuesday–Friday. The scanner waits for the next permitted session.' if not window else
            'NFP week: new reviews stop Wednesday 11:00 New York through Friday.' if 'NFP_WEEK_AFTER_WEDNESDAY_1100' in news.get('active_policy_reasons',[]) else
            'FOMC morning: reviews are paused.' if 'FOMC_MORNING' in news.get('active_policy_reasons',[]) else
            'Scheduled news blackout. Wait until it ends.' if news['status']=='BLACKOUT' else
            'News calendar missing, stale or invalid. Restore the calendar feed.' if news['status']!='CLEAR' else
            'Session and news rules allow reviews.')
    check('Session & news','ok' if allowed else 'wait',detail,not allowed)
    cap=daily_call_limit(day);used=len(costs);amount=sum(r['amount'] if r['amount'] is not None else r['reserve'] for r in costs)
    session_used=sum(window[2]<=r['at']<window[1] for r in costs) if window else 0
    budget_ok=used<cap and amount<DAILY_USD and (not window or session_used<session_call_limit(window[0])) and not saved.get('budget_anomaly')
    check('AI budget','ok' if budget_ok else 'wait',f'{used}/{cap} calls today · ${amount:.2f}/${DAILY_USD:.2f} estimated or reserved'+(' · limit or reconciliation check prevents further calls.' if not budget_ok else ''),not budget_ok)
    last=saved.get('last_check',{})
    timing={
        'THREE_REVIEWS_PER_30_MINUTES':'Three reviews in 30 minutes. Waiting for the rolling allowance to reopen.',
        '90_SECOND_COOLDOWN':'Waiting for the minimum 90-second interval between reviews.',
        'WATCHING_CANDLES_NO_NEW_EVENT':'Watching for fresh structure; otherwise reassessment is due 20 minutes after the last review.',
        'WAIT_FIRST_CLOSED_CANDLE':'Waiting for the first closed candle of this session.',
    }
    wait=timing.get(last.get('state')) if 0<=now-last.get('at',0)<180 else None
    if last.get('state')=='BLOCKED' and 0<=now-last.get('at',0)<180:
        wait='Last scanner check failed: '+str(last.get('reason') or 'unknown reason')[:160]
    check('Review timing','wait' if wait else 'ok',wait or 'Fresh-event, cooldown and reassessment rules apply.',bool(wait))
    market=saved.get('market',{});market_fresh=0<=now-market.get('as_of',0)<=180
    check('Market observations','ok' if market_fresh else 'wait','Recent closed candles available.' if market_fresh else 'Waiting for fresh closed candles. Recovery cannot manufacture missing market data.')
    return dict(at=now,checks=checks,blockers=blockers,can_recheck=enabled and not any(c['state']=='error' for c in checks),
                state='WAITING' if blockers else 'READY' if fresh and market_fresh else 'CHECK_REQUIRED',
                message=blockers[0] if blockers else 'Normal scheduling, cooldowns and per-trade checks still apply.',
                last_action=saved.get('ai_recovery_action'),charges_preserved=True)


def register(app,private,cloud,*,allow_worker_start=True):
    from flask import request,jsonify
    def response(value,token=None,status=200):
        r=jsonify(value);r.status_code=status
        if token:r.set_cookie(COOKIE,token,secure=True,httponly=True,samesite='Strict',path='/',max_age=1200)
        return r
    def state():
        token=request.cookies.get(COOKIE)
        if not token or len(token)<32:token=secrets.token_urlsafe(32)
        try:return response(dict(inspect(cloud),csrf=token),token)
        except Exception:return response(dict(state='UNAVAILABLE',message='Recovery status unavailable. No recovery performed.',checks=[],can_recheck=False,csrf=token),token,503)
    def recover():
        token=request.cookies.get(COOKIE,'')
        if (request.headers.get('Origin')!=ORIGIN or request.headers.get('Sec-Fetch-Site','same-origin')!='same-origin'
                or not request.is_json or request.content_length is None or request.content_length>1024
                or len(token)<32 or not hmac.compare_digest(request.headers.get('X-ICT-Recovery-CSRF',''),token)):
            return response(dict(error='Refresh the dashboard and retry from this page.'),status=403)
        body=request.get_json(silent=True)
        if not isinstance(body,dict) or body.get('recover') is not True:
            return response(dict(error='Explicit recovery request required.'),status=400)
        if not allow_worker_start:return response(dict(error='This dashboard instance cannot start the worker.'),status=409)
        try:
            report=inspect(cloud)
            if not report['can_recheck']:return response(dict(report,action='BLOCKED'))
            # Shared durable cooldown makes double taps/retries idempotent across workers.
            now=time.time()
            with cloud.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute("SELECT value FROM state WHERE key='ai_recovery_action'").fetchone()
                prior=json.loads(row[0]) if row else None
                if prior and 0<=now-prior['at']<30:
                    return response(dict(report,action='ALREADY_REQUESTED',message='A check was already requested. Wait for its result.'))
                action=dict(at=now,state='CHECK_REQUESTED')
                db.execute("INSERT OR REPLACE INTO state VALUES('ai_recovery_action',?)",(json.dumps(action),))
            # No direct tick/call or permission changes. The normal worker applies all gates.
            result=cloud.start_worker()
            return response(dict(report,action=result,message='Normal scanner check requested. '+report['message']))
        except Exception:
            return response(dict(error='Recovery could not be confirmed. Refresh status before trying again.'),status=503)
    app.add_url_rule('/ict-shadow/ai-recovery','ict_ai_recovery_status',private(state))
    app.add_url_rule('/ict-shadow/ai-recovery/check','ict_ai_recovery_check',private(recover),methods=['POST'])
