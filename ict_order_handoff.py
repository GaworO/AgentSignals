"""ICT proposal -> durable preview and separately armed automatic dispatch.

Research approval is not production execution approval. Keep the independent
identity, original geometry and expiry. No permission writes; actual execution uses shared durable broker journals.
"""
import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR

STRATEGY = 'ICT_AI'
VERSION = 'ICT_LIMIT_AUTOMATIC_ROUTE_V1'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def proposal(record, contract, now, *, family="ICT2022"):
    """Recheck the original saved evidence, not merely a cached valid flag."""
    import ict_shadow_research as research
    if record.get('family') == 'IFVG' or record.get('parent_review_id') or str(record.get('id','')).endswith(':IFVG'):
        raise ValueError('IFVG_REVIEW_ONLY_NO_BROKER_PERMISSION')
    if family not in ('ICT2022','IFVG'):raise ValueError('UNKNOWN_ICT_FAMILY')
    packet, parent = record['packet'], record['decision']
    decision=parent if family=='ICT2022' else parent['ifvg_review']
    if decision.get('decision') not in ('PROPOSE_LONG', 'PROPOSE_SHORT'):
        raise ValueError('NO_ENTRY_PROPOSED')
    if not re.fullmatch(r'MNQ[HMUZ]20\d{2}', str(contract)) or packet.get('contract_configured') != contract:
        raise ValueError('CONTRACT_MISMATCH')
    if not packet.get('cloud_shadow'):
        raise ValueError('LEGACY_RESEARCH_NOT_FORWARD_ROUTED')
    if record.get('state') != 'research_completed' or record.get('error'):
        raise ValueError('REVIEW_NOT_COMPLETED')
    raw = record.get('response') or {}
    texts = [c['text'] for item in raw.get('output', []) if item.get('type') == 'message'
             for c in item.get('content', []) if c.get('type') == 'output_text']
    if raw.get('status') != 'completed' or len(texts) != 1 or json.loads(texts[0]) != parent:
        raise ValueError('SAVED_AI_RESPONSE_MISMATCH')
    received = record.get('received_at')
    if type(received) not in (float, int) or not math.isfinite(received) or received > now:
        raise ValueError('RESPONSE_TIMESTAMP_INVALID')
    packet_body = {k: v for k, v in packet.items() if k != 'packet_id'}
    if research.digest(packet_body) != packet.get('packet_id'):
        raise ValueError('SAVED_PACKET_CHANGED')
    v = research.validate(packet, parent, received)
    saved = record['validation']
    if family=='IFVG':
        v=v['ifvg_validation'];saved=saved['ifvg_validation']
    if v.get('state')!='VALIDATED_SHADOW_PROPOSAL':raise ValueError('NO_VALIDATED_PROPOSAL')
    for key in ('state', 'entry', 'stop', 'target', 'activation_at', 'expires_at', 'gross_rr'):
        if saved.get(key) != v.get(key):
            raise ValueError('SAVED_APPROVAL_CHANGED')
    if not received <= now < v['expires_at']:
        raise ValueError('PROPOSAL_EXPIRED')
    plan = decision['plan']
    target=v['entry']+(1 if plan['direction']=='LONG' else -1)*2*abs(v['entry']-v['stop'])
    binding = dict(review_id=record['id'], packet_id=packet['packet_id'], direction=plan['direction'],
                   contract=contract, entry=v['entry'], stop=v['stop'], target=target,
                   ai_liquidity_target=v.get('ai_liquidity_target',v['target']), target_rule='FIXED_2R_GROSS',
                   activation_at=received, expires_at=v['expires_at'])
    if family=='IFVG':binding.update(family='IFVG',inversion_bar_id=plan['inversion_bar_id'])
    fingerprint = hashlib.sha256(canonical(binding).encode()).hexdigest()
    return dict(binding, strategy=STRATEGY, order_id='ICT-'+fingerprint[:40], fingerprint=fingerprint,
                order_type='LIMIT', gross_rr=2.0, ai_gross_rr=v['gross_rr'], risk_usd_per_mnq=v['risk_usd_per_mnq'],
                invalidation_condition=plan.get('invalidation_condition'),
                execution_authorized=False)


def market_reason(plan, packet, now):
    """Current closed candles can invalidate; they cannot establish live BBO."""
    if packet.get('contract_configured') != plan['contract']:
        return 'CURRENT_CONTRACT_MISMATCH'
    cutoff = packet.get('as_of', 0)
    if cutoff != int(now)//60*60 or packet.get('source_observed_at', 0) > now:
        return 'CURRENT_CLOSED_CANDLE_MISSING'
    bars = [b for b in packet.get('evidence', {}).values()
            if b.get('kind') == 'bar' and b.get('symbol') == 'MNQ' and b.get('timeframe') == 1]
    times = {b['time'] for b in bars if b.get('available_at') == b['time']+60 and b['available_at'] <= cutoff}
    start = int(plan['activation_at'])//60*60
    if any(t not in times for t in range(start, int(cutoff), 60)):
        return 'POST_REVIEW_CANDLE_GAP'
    for b in bars:
        if b['time'] < start or b.get('available_at', math.inf) > cutoff:
            continue
        if plan['direction'] == 'LONG' and (b['low'] <= plan['stop'] or b['high'] >= plan['target']):
            return 'BRACKET_TOUCHED_BEFORE_SUBMISSION'
        if plan['direction'] == 'SHORT' and (b['high'] >= plan['stop'] or b['low'] <= plan['target']):
            return 'BRACKET_TOUCHED_BEFORE_SUBMISSION'
    return None


def limit_preview(plan):
    """Matches supported cloud Tradovate native OSO fields; quantity needs account validation.

    Preview only. Account identity, risk, shared ownership
    reservation, approved execution policy and final revalidation are not implied.
    """
    return dict(instrument=plan['contract'][:-4]+plan['contract'][-1],
                action='buy' if plan['direction']=='LONG' else 'sell', qty=None,
                orderType='limit', limitPrice=plan['entry'], stopLoss=plan['stop'],
                takeProfit=plan['target'], tif='gtd',
                expireTime=datetime.fromtimestamp(plan['expires_at'], timezone.utc).isoformat().replace('+00:00','Z'),
                clOrdId='ict-'+plan['fingerprint'][:56], requireMarketPosition='flat', maxPositions=1)


class Handoff:
    def __init__(self, root, config, sizing=None):
        self.path = root/'ict_order_handoff.sqlite3'
        self.config = config
        self.sizing = sizing or sizing_preview
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS intents(review_id TEXT PRIMARY KEY, fingerprint TEXT, created REAL, updated REAL, body TEXT)')

    def db(self):
        db=sqlite3.connect(self.path, timeout=15);db.row_factory=sqlite3.Row;return db

    def refresh(self, records, packet, now):
        for r,family in [(r,f) for r in records for f in ('ICT2022','IFVG')]:
            decision=(r.get('decision') or {})
            if family=='IFVG':decision=decision.get('ifvg_review') or {}
            if not decision.get('plan'):continue
            rid=r['id']+(':IFVG' if family=='IFVG' else '')
            body=dict(review_id=rid, family=family, checked_at=now, execution_authorized=False,
                      mode='PREVIEW_ONLY', broker_order_sent=False, quantity=None,
                      version=VERSION, blockers=[], plan=None, command_preview=None)
            fingerprint=None
            try:
                p=proposal(r, self.config.get('AB_V3_CONTRACT'), now,family=family)
                fingerprint=p['fingerprint'];body['plan']=p
                body['command_preview']=limit_preview(p)
                size=self.sizing(p,now);body['sizing']=size
                if size.get('allowed') is True:
                    body['quantity']=size['max_quantity']
                    body['command_preview']['qty']=size['max_quantity']
                else:body['blockers'].append(size.get('reason','ACCOUNT_CAPACITY_UNKNOWN'))
                why=market_reason(p, packet, now)
                if why:body['blockers'].append(why)
                import ict_execution
                runtime=ict_execution.status()
                body['blockers'] += runtime['ifvg']['blockers'] if family=='IFVG' else runtime['blockers']
                contract=ict_execution.IFVG_VERSION if family=='IFVG' else ict_execution.VERSION
                key='ifvg_execution_contract' if family=='IFVG' else 'execution_contract'
                if r['packet'].get(key)!=contract or decision['plan'].get('invalidation_mode')!=contract:
                    body['blockers'].append('ICT_INSTRUCTION_INVALIDATION_NOT_MACHINE_VALIDATED')
                body['state']='INVALIDATED' if why=='BRACKET_TOUCHED_BEFORE_SUBMISSION' else 'PREVIEW_BLOCKED'
            except (AssertionError, KeyError, TypeError, ValueError, OverflowError, IndexError):
                # Stable messages only; never expose full input or account credentials.
                state='EXPIRED' if (r.get('validation') or {}).get('expires_at',math.inf)<=now else 'INVALID_PROPOSAL'
                body.update(state=state,blockers=[state])
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                old=db.execute('SELECT fingerprint,body FROM intents WHERE review_id=?',(rid,)).fetchone()
                if old:
                    previous=json.loads(old['body'])
                    if (old['fingerprint'] and fingerprint and old['fingerprint']!=fingerprint) or previous['state']=='CHANGED_PROPOSAL':
                        body.update(state='CHANGED_PROPOSAL',blockers=['SAVED_PROPOSAL_CHANGED'],command_preview=None)
                        fingerprint=old['fingerprint']
                    if previous['state'] in ('EXPIRED','INVALIDATED','INVALID_PROPOSAL'):
                        body.update(state=previous['state'],blockers=previous['blockers'],command_preview=None)
                    db.execute('UPDATE intents SET fingerprint=COALESCE(fingerprint,?),updated=?,body=? WHERE review_id=?',
                               (fingerprint,now,canonical(body),rid))
                else:
                    db.execute('INSERT INTO intents VALUES(?,?,?,?,?)',(rid,fingerprint,now,now,canonical(body)))

            if self.config.get('ICT_EXECUTION_ENABLED','false')=='true' and body['state']=='PREVIEW_BLOCKED' and body.get('plan'):
                import ict_execution
                body['execution_result']=ict_execution.process(r,family=family)
                body['mode']='GUARDED_AUTOMATIC'
                with self.db() as db:
                    db.execute('UPDATE intents SET body=? WHERE review_id=?',(canonical(body),rid))

    def state(self):
        with self.db() as db:rows=[json.loads(r[0]) for r in db.execute('SELECT body FROM intents ORDER BY created DESC LIMIT 100')]
        import ict_execution
        runtime=ict_execution.status();executions=execution_rows()
        return dict(mode='GUARDED_AUTOMATIC' if runtime['enabled'] else 'EXECUTION_DISABLED', execution_authorized=False, broker_order_sent=any(r.get('broker_ack') for r in executions),
                    runtime=runtime, broker_lifecycle=executions,
                    risk_percent=0.5, target_rule='FIXED_2R_GROSS',
                    strategy=STRATEGY, destination='Existing CrossTrade cloud Tradovate connection',
                    route_status='CONNECTED' if runtime['submission_connected'] else 'RUNTIME_NOT_CONNECTED', intents=rows)


def broker_status():
    """Read the existing synchronizer; no new provider request and no commands."""
    try:
        import builder_broker_sync
        s=builder_broker_sync.instance().state()
        return {k:s.get(k) for k in ('enabled','provider','state','current','age_seconds','open_positions','working_orders')}
    except Exception:
        return dict(state='UNAVAILABLE',current=False)


def sizing_preview(plan, now):
    """Use existing verified account controls, never ask AI to choose quantity.

    Reads the existing broker synchronizer and account allowance adapter only.
    Does not refresh permissions, reserve exposure or touch the broker journal.
    """
    result=dict(allowed=False,max_quantity=0,risk_percent=0.5,reason='ACCOUNT_CAPACITY_UNKNOWN')
    try:
        import builder_broker_sync
        import directional_account_limits as account
        import execution_validation
        sync=builder_broker_sync.instance()
        if not sync.enabled or sync.state().get('current') is not True:
            return dict(result,reason='FRESH_BROKER_SNAPSHOT_REQUIRED')
        snapshot=sync.snapshot()
        # Sharing verified fee data does not approve Directional's execution
        # policy for ICT. The latter remains a separate mandatory blocker.
        parameters=execution_validation.policy()
        if parameters['contract']!=plan['contract']:return dict(result,reason='COST_CONTRACT_MISMATCH')
        fees=execution_validation.costs(parameters)
        equity=snapshot['account']['balance']['netLiq']
        if type(equity) not in (int,float) or not math.isfinite(equity) or equity<=0:
            return dict(result,reason='CURRENT_EQUITY_UNKNOWN')
        budget=float((Decimal(str(equity))*Decimal('0.005')).quantize(Decimal('.01'),rounding=ROUND_FLOOR))
        costs=fees['round_trip_usd']+fees['stop_slippage_ticks']*.5
        order=dict(order_id=plan['order_id'],contract=plan['contract'],direction=plan['direction'],
                   entry_price=plan['entry'],stop_price=plan['stop'],target_price=plan['target'])
        size=account.capacity(snapshot,sync.binding,order,int(now*1000),costs,budget)
        size.update(risk_percent=0.5,approved_budget_usd=budget,preview_only=True)
        if size.get('allowed'):
            per=Decimal(str(size['risk_per_contract_usd']))
            qty=min(size['max_quantity'],int(Decimal(str(budget))/per))
            if qty<1:return dict(size,allowed=False,max_quantity=0,reason='RISK_BELOW_ONE_CONTRACT')
            size.update(max_quantity=qty,total_risk_usd=float(per*qty))
        return size
    except Exception:
        return result


def execution_rows():
    try:
        import continuation_live,ab_v3_live
        with continuation_live._connect() as db:
            rows=[dict(r) for r in db.execute("SELECT order_id,state,guard_reason,quantity,updated_at FROM live_dispatches WHERE strategy='ICT_AI' ORDER BY created_at DESC LIMIT 100")]
        with ab_v3_live.connect() as db:
            for r in rows:
                n=db.execute('SELECT state,receipt_json FROM native_orders WHERE order_id=?',(r['order_id'],)).fetchone()
                r['broker_state']=n['state'] if n else None
                receipt=json.loads(n['receipt_json']) if n and n['receipt_json'] else {}
                r['broker_ack']={k:receipt.get(k) for k in ('orderId','oso1Id','oso2Id')} if receipt else None
        return rows
    except Exception:return []
