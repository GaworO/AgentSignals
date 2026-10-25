"""Disabled-by-default 0/10/20 recovery journal for Directional.

This module has NO broker-writing or AI transport API. Price plans and exact
review results share the existing AI SQLite connection and review budget.
Live scheduling/dispatch is not enabled by preparing or recording a plan.
"""
import hashlib
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo
import directional_hybrid_context

MINUTE = 60_000
VERSION = 'directional-nofill-v1'


def enabled(config=None):
    return (os.environ if config is None else config).get('AB_DIRECTIONAL_NO_FILL_RECOVERY','false') == 'true'


def encode(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def init(db):
    db.execute('''CREATE TABLE IF NOT EXISTS nofill_opportunities(
      opportunity_id TEXT PRIMARY KEY, original TEXT NOT NULL, original_hash TEXT NOT NULL,
      activation_ms INTEGER NOT NULL, deadline_ms INTEGER NOT NULL,
      state TEXT NOT NULL, review_id TEXT UNIQUE, replacement_id TEXT UNIQUE,
      last_broker TEXT, decision TEXT, updated_ms INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS nofill_events(
      id INTEGER PRIMARY KEY, opportunity_id TEXT NOT NULL, at_ms INTEGER NOT NULL,
      event TEXT NOT NULL, detail TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS nofill_review_results(
      review_id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL,
      received_ms INTEGER NOT NULL, response_body TEXT NOT NULL, body_format TEXT NOT NULL,
      response_text TEXT, decision TEXT, selected_plan TEXT, status TEXT NOT NULL, error TEXT,
      latency_ms INTEGER NOT NULL)''')


def event(db,oid,now,kind,detail):
    db.execute('INSERT INTO nofill_events(opportunity_id,at_ms,event,detail) VALUES(?,?,?,?)',
               (oid,now,kind,encode(detail)))


def register(db,order,now):
    """Only called for an authenticated original native intent, not backtest rows."""
    if order.get('strategy')!='AB_DIRECTIONAL' or order.get('recovery_version'):
        raise ValueError('original_directional_required')
    start=order['activation_ms']
    if type(start) is not int or not start<=now<start+10*MINUTE:
        raise ValueError('original_opportunity_registration_expired')
    if order['expiry_ms']!=start+10*MINUTE:raise ValueError('original_ten_minute_expiry_required')
    value=encode(order);fingerprint=hashlib.sha256(value.encode()).hexdigest()
    row=db.execute('SELECT original_hash FROM nofill_opportunities WHERE opportunity_id=?',(order['order_id'],)).fetchone()
    if row:
        if row[0]!=fingerprint:raise ValueError('original_opportunity_changed')
        return False
    db.execute('INSERT INTO nofill_opportunities VALUES(?,?,?,?,?,?,NULL,NULL,NULL,NULL,?)',
               (order['order_id'],value,fingerprint,start,start+20*MINUTE,'ORIGINAL',now))
    event(db,order['order_id'],now,'REGISTERED',dict(original_hash=fingerprint))
    return True


def broker_reason(proof,oid,now):
    """ACK is never cancellation; zero exposure must be independently reconciled."""
    if proof.get('order_id')!=oid:return 'BROKER_ORDER_MISMATCH'
    at=proof.get('observed_ms')
    if type(at) is not int or not 0<=now-at<=3000:return 'BROKER_EVIDENCE_STALE'
    if proof.get('verified') is not True:return 'BROKER_EVIDENCE_UNVERIFIED'
    if type(proof.get('entry_quantity')) is not int or proof['entry_quantity']<0:return 'FILLS_UNKNOWN'
    if proof['entry_quantity']>0:return 'EXPOSURE_EXISTS'
    if proof.get('positions')!=[] or proof.get('working_orders')!=[]:return 'EXPOSURE_OR_WORKING_ORDER'
    ids=proof.get('broker_ids')
    if (not isinstance(ids,dict) or set(ids)!=set(('orderId','oso1Id','oso2Id'))
            or any(type(n) is not int or n<=0 for n in ids.values()) or len(set(ids.values()))!=3):
        return 'BROKER_IDS_UNKNOWN'
    if proof.get('state')!='NO_FILL' or proof.get('entry_status') not in ('Canceled','Cancelled','Expired'):
        return 'CANCELLATION_NOT_CONFIRMED'
    since=proof.get('no_fill_verified_since_ms')
    if type(since) is not int or not 15000<=now-since:return 'NO_FILL_SETTLEMENT_PENDING'
    return None


def advance(db,oid,proof,now):
    row=db.execute('SELECT * FROM nofill_opportunities WHERE opportunity_id=?',(oid,)).fetchone()
    if not row:raise ValueError('opportunity_not_registered')
    expected=row['replacement_id'] or oid
    reason=broker_reason(proof,expected,now)
    # Actual late/partial fills have precedence over any deadline or AI state.
    if reason=='EXPOSURE_EXISTS':state,action='EXPOSURE','PRESERVE_PROTECTION_RECONCILE'
    elif row['state']=='EXPOSURE':state,action='EXPOSURE','PRESERVE_PROTECTION_RECONCILE'
    elif now>=row['deadline_ms']:
        state,action=('EXPIRED','NONE') if reason is None else ('CANCEL_UNCERTAIN','RECONCILE_CANCEL')
    elif row['state'] in ('WAIT','CANCEL','REJECT','REJECTED','EXPIRED','REVIEW_RESERVED','REVIEW_UNKNOWN','SELECTED','REPLACEMENT_RESERVED'):
        state,action=row['state'],'NONE'
    elif now<row['activation_ms']+10*MINUTE:state,action='ORIGINAL','NONE'
    elif reason is None:state,action='REVIEW_ELIGIBLE','BUILD_CURRENT_CONTEXT'
    else:state,action='CANCEL_UNCERTAIN','RECONCILE_CANCEL'
    db.execute('UPDATE nofill_opportunities SET state=?,last_broker=?,updated_ms=? WHERE opportunity_id=?',
               (state,encode(proof),now,oid))
    event(db,oid,now,action,dict(state=state,reason=reason))
    return dict(state=state,action=action,reason=reason,deadline_ms=row['deadline_ms'])


def reserve_review(db,oid,request,external_calls,now,*,config):
    """Caller holds BEGIN IMMEDIATE on the EXISTING AI database.

    Inserts into its existing reviews table: recovery consumes SETUP allocation,
    never another pool. The reservation survives crashes and cannot be resent.
    """
    if not enabled(config):return dict(allowed=False,reason='NO_FILL_RECOVERY_DISABLED')
    if not db.in_transaction:raise ValueError('atomic_review_transaction_required')
    row=db.execute('SELECT * FROM nofill_opportunities WHERE opportunity_id=?',(oid,)).fetchone()
    if not row or row['state']!='REVIEW_ELIGIBLE' or row['review_id']:
        return dict(allowed=False,reason='RECOVERY_ALREADY_CONSUMED_OR_INELIGIBLE')
    if not row['activation_ms']+10*MINUTE<=now<row['deadline_ms']:
        return dict(allowed=False,reason='RECOVERY_EXPIRED')
    why=broker_reason(json.loads(row['last_broker']),oid,now)
    if why:return dict(allowed=False,reason=why)
    if db.execute("SELECT 1 FROM setup_requests WHERE state='queued' AND expires>? LIMIT 1",(now/1000,)).fetchone():
        return dict(allowed=False,reason='ORIGINAL_SETUP_PRIORITY')
    if type(external_calls) is not int or external_calls<0:return dict(allowed=False,reason='AI_USAGE_UNKNOWN')
    day=datetime.fromtimestamp(now/1000,ZoneInfo('America/New_York')).date().isoformat()
    rows=db.execute('SELECT * FROM reviews WHERE day=?',(day,)).fetchall()
    if any(r['status']=='running' for r in rows):return dict(allowed=False,reason='AI_REQUEST_ALREADY_RUNNING')
    why=directional_hybrid_context.budget_reason(rows,external_calls,'SETUP',int(config.get('AB_AI_MAX_CALLS_PER_DAY','12')))
    if why:return dict(allowed=False,reason=why)
    rid=hashlib.sha256(encode([VERSION,oid,row['original_hash'],request]).encode()).hexdigest()
    # A reserved request is counted even if transport never returns. No automatic retries.
    db.execute('INSERT INTO reviews(id,day,started,cutoff,status,request,review_kind) VALUES(?,?,?,?,?,?,?)',
               (rid,day,now/1000,now//1000,'running',encode(request),'SETUP'))
    db.execute("UPDATE nofill_opportunities SET state='REVIEW_RESERVED',review_id=?,updated_ms=? WHERE opportunity_id=?",
               (rid,now,oid))
    event(db,oid,now,'REVIEW_RESERVED',dict(review_id=rid))
    return dict(allowed=True,review_id=rid,opportunity_id=oid,plan_version=2,deadline_ms=row['deadline_ms'])


def finish_without_entry(db,oid,review_id,decision,now):
    """WAIT/CANCEL/timeout are terminal for this opportunity; never a third review."""
    if decision not in ('WAIT','CANCEL','REJECTED','REVIEW_UNKNOWN'):raise ValueError('non_entry_decision_required')
    row=db.execute('SELECT * FROM nofill_opportunities WHERE opportunity_id=?',(oid,)).fetchone()
    if not row or row['review_id']!=review_id or row['state']!='REVIEW_RESERVED':
        raise ValueError('review_not_owned_or_completed')
    state='EXPIRED' if now>=row['deadline_ms'] else decision
    db.execute('UPDATE nofill_opportunities SET state=?,decision=?,updated_ms=? WHERE opportunity_id=?',
               (state,encode(dict(decision=decision)),now,oid))
    db.execute('UPDATE reviews SET status=?,finished=? WHERE id=?',(decision.lower(),now/1000,review_id))
    event(db,oid,now,'NO_REPLACEMENT',dict(decision=decision,state=state))


def prepare_review(db, oid, packet, selections, external_calls, now, *, config):
    """Build wick-based alternatives, then atomically reserve existing SETUP capacity.

    Explicit source-candle association must come from the caller's verified
    detector mapping. There is deliberately no first/middle/third fallback.
    No API is called here; returned payload can use the existing AI transport.
    """
    import directional_recovery_plans as plans
    import directional_recovery_review as review
    from ab_ai_context import session_at
    if not enabled(config):return dict(allowed=False, reason='NO_FILL_RECOVERY_DISABLED')
    if config.get('AB_AI_ENABLED','false')!='true' or config.get('AB_AI_OWNER')!='AB_DIRECTIONAL':
        return dict(allowed=False,reason='DIRECTIONAL_AI_DISABLED_OR_WRONG_OWNER')
    if session_at(now/1000) is None:return dict(allowed=False,reason='OUTSIDE_AB_SESSIONS')
    if not db.in_transaction:raise ValueError('atomic_review_transaction_required')
    row=db.execute('SELECT * FROM nofill_opportunities WHERE opportunity_id=?',(oid,)).fetchone()
    if not row:return dict(allowed=False,reason='OPPORTUNITY_UNKNOWN')
    original=json.loads(row['original'])
    # Same existing buffer convention; do not silently invent a new one.
    try:buffer=float(config.get('SL_ANCHOR_BUF','0.25'))
    except (ValueError,TypeError):return dict(allowed=False,reason='WICK_BUFFER_INVALID')
    candidates=[plans.build(original,packet,s,now,stop_buffer_points=buffer) for s in selections]
    try:payload=review.request(original,packet,candidates,config.get('OPENAI_MODEL'))
    except ValueError as exc:return dict(allowed=False,reason=str(exc),candidates=candidates)
    reserved=reserve_review(db,oid,payload,external_calls,now,config=config)
    return dict(reserved,request=payload if reserved['allowed'] else None,candidates=candidates)


def complete_review(db, oid, review_id, response_body, fresh_packet, broker_proof, now):
    """Persist the real response and select a plan, never submit it to a broker.

    Caller holds BEGIN IMMEDIATE. First response wins; retries/restarts cannot
    overwrite it. A transport timeout is recorded with response_body=None and
    consumes the one review. Broker cancel proof is re-read after model latency.
    """
    import directional_recovery_review as review
    if not db.in_transaction:raise ValueError('atomic_review_transaction_required')
    row=db.execute('SELECT * FROM nofill_opportunities WHERE opportunity_id=?',(oid,)).fetchone()
    saved=db.execute('SELECT * FROM reviews WHERE id=?',(review_id,)).fetchone()
    if (not row or not saved or row['review_id']!=review_id or row['state']!='REVIEW_RESERVED'
            or db.execute('SELECT 1 FROM nofill_review_results WHERE review_id=?',(review_id,)).fetchone()):
        raise ValueError('REVIEW_NOT_OWNED_OR_ALREADY_COMPLETED')
    decision=selected=response_text=None; error=None; state='REJECTED'
    original_body=getattr(response_body,'response_text',None)
    body_format=(getattr(response_body,'response_body_format',None)
                 or ('original_http_utf8' if isinstance(response_body,str) or original_body is not None
                     else 'transport_json_reserialized'))
    body=original_body if original_body is not None else response_body if isinstance(response_body,str) else encode(response_body)
    try:
        if broker_reason(broker_proof,oid,now)=='EXPOSURE_EXISTS':
            state='EXPOSURE';raise ValueError('EXPOSURE_EXISTS')
        if response_body is None:
            state='REVIEW_UNKNOWN';raise ValueError('AI_TIMEOUT_OR_MISSING_RESPONSE')
        # The existing streaming transport returns parsed JSON plus original
        # SSE bytes. Preserve those bytes while validating its parsed response.
        raw=response_body if isinstance(response_body,dict) else json.loads(body)
        if raw.get('status')!='completed':raise ValueError('AI_RESPONSE_INCOMPLETE')
        parts=[c for item in raw.get('output',[]) if item.get('type')=='message' for c in item.get('content',[])]
        texts=[c['text'] for c in parts if c.get('type')=='output_text']
        if len(texts)!=1 or any(c.get('type')=='refusal' for c in parts):
            raise ValueError('AI_STRUCTURED_RESPONSE_MISSING')
        response_text=texts[0];decision=json.loads(response_text)
        payload=json.loads(saved['request']);selected=review.validate(decision,payload)
        if now>=row['deadline_ms']:
            state='EXPIRED';raise ValueError('RECOVERY_EXPIRED')
        if selected is None:
            state=decision['decision']
        else:
            why=broker_reason(broker_proof,oid,now)
            if why:
                if why=='EXPOSURE_EXISTS':state='EXPOSURE'
                raise ValueError(why)
            review.revalidate(selected,payload,fresh_packet,now)
            state='SELECTED'
    except (ValueError,KeyError,TypeError,AttributeError) as exc:
        error=str(exc) if isinstance(exc,ValueError) else 'RECOVERY_RESPONSE_INVALID'
        selected=None
    db.execute('INSERT INTO nofill_review_results VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (review_id,oid,now,body,body_format,response_text,encode(decision),encode(selected),state,error,
         max(0,now-int(saved['started']*1000))))
    db.execute('UPDATE nofill_opportunities SET state=?,decision=?,last_broker=?,updated_ms=? WHERE opportunity_id=?',
        (state,encode(dict(ai=decision,selected_plan=selected,error=error)),encode(broker_proof),now,oid))
    db.execute('UPDATE reviews SET status=?,finished=? WHERE id=?',(state.lower(),now/1000,review_id))
    event(db,oid,now,'REVIEW_RESULT',dict(state=state,error=error,plan_id=selected['plan_id'] if selected else None))
    return dict(state=state,reason=error,selected_plan=selected,execution_authorized=False)
