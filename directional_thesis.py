"""Fresh original-setup evidence, independent of the model's delayed ALLOW.

Uses closed bars for structure/invalidation, never as executable prices.
The conservative no-consumed-setup rule must be explicitly policy-validated.
"""
import hashlib
import json
import time

import ab_ai_context
import ab_directional_entry_rules
import ab_v3_live
import continuation_live
import continuation_shadow
import directional_setup_review

VERSION='original_bos_bracket_ai_v1'
GEOMETRY=('strategy','direction','activation_ms','expiry_ms','entry_price','stop_price','target_price')


def structural_check(order, source, rows, now_ms):
    if any(source.get(k)!=order.get(k) for k in GEOMETRY):
        return 'original_setup_changed'
    if source.get('state') != 'PENDING':
        return 'original_setup_consumed_or_expired'
    if not order['activation_ms'] <= now_ms < order['expiry_ms']:
        return 'signal_expired'
    reason=ab_directional_entry_rules.dispatch_blocker(source) or continuation_live._v3_entry_filter(source)
    if reason:return reason
    evidence=json.loads(source['payload_json'])['entry_rules']
    by_time={r['ts_ms']:json.loads(r['bar_json']) for r in rows}
    bos=by_time.get(evidence['bos_ms'])
    if not bos or any(bos.get(k)!=v for k,v in evidence['bos_ohlc'].items()):
        return 'original_bos_reference_missing_or_changed'
    end=now_ms//60000*60000
    first=order['activation_ms']//60000*60000
    if any(t not in by_time for t in range(first,end,60000)):
        return 'post_setup_reference_gap'
    sign=1 if order['direction']=='LONG' else -1
    for at,bar in by_time.items():
        if at<first or at+60000>now_ms:continue
        if ((bar['low']<=order['stop_price'] or bar['high']>=order['target_price']) if sign==1
                else (bar['high']>=order['stop_price'] or bar['low']<=order['target_price'])):
            return 'original_bracket_touched_before_entry'
    return None


class ThesisValidator:
    def __init__(self, *, clock=time.time):self.clock=clock

    def __call__(self, order, *, pending=False):
        import execution_validation
        p=execution_validation.policy() if not pending else {'thesis_rule_version':VERSION}
        proof=dict(order_id=order['order_id'],valid=False,checked_ms=int(self.clock()*1000))
        if order.get('recovery_version') == 2:
            try:
                import directional_recovery_runtime
                return directional_recovery_runtime.validate_entry(ab_ai_context.INSTANCE,order,pending=pending)
            except Exception as exc:
                return dict(proof,reason=str(exc) if isinstance(exc,ValueError) else 'recovery_thesis_unavailable')
        if p.get('thesis_rule_version')!=VERSION:
            return dict(proof,reason='validated_thesis_rule_required')
        try:
            with continuation_shadow._connect() as db:
                row=db.execute('SELECT o.*,c.payload_json candidate_payload_json FROM continuation_orders o JOIN continuation_candidates c ON c.candidate_id=o.candidate_id WHERE o.order_id=?',(order['order_id'],)).fetchone()
            if row is None:return dict(proof,reason='original_setup_missing')
            source=dict(row)
            evidence=json.loads(source['payload_json'])['entry_rules']
            with ab_v3_live.connect() as db:
                rows=db.execute("SELECT ts_ms,bar_json FROM bars WHERE tf='M1' AND contract=? AND ts_ms>=? AND ts_ms+60000<=? ORDER BY ts_ms",
                                (order['contract'],evidence['bos_ms'],proof['checked_ms'])).fetchall()
            reason=structural_check(order,source,rows,proof['checked_ms'])
            if reason:return dict(proof,reason=reason)
            service=ab_ai_context.INSTANCE
            if service is None:return dict(proof,reason='setup_review_unavailable')
            # Fresh market-only GET. No model call, no review budget consumption.
            packet=service.packet(service.reader(),self.clock())
            end=int(self.clock())//60*60
            if packet['as_of']!=end:return dict(proof,reason='post_review_context_not_current')
            if getattr(service,'trigger',None)=='HYBRID':
                # Publish this newer GET before querying the approval cache.
                # Otherwise a material event seen here could be absent from
                # the worker's last refresh while its ALLOW remained reusable.
                ab_ai_context.hybrid_lifecycle.refresh(service,packet)
            review=service.setup_annotation(order['order_id'],order['direction'])
            if review.get('state')!='ALLOW':return dict(proof,reason='review_not_allowed')
            current_setup=directional_setup_review.describe(source,order['contract'])
            if current_setup != review.get('setup'):
                return dict(proof,reason='original_liquidity_or_setup_evidence_changed')
            for symbol in ('NQ','MNQ'):
                times={e['available_at'] for e in packet['evidence'].values()
                       if e['kind']=='bar' and e['timeframe']==1 and e['symbol']==symbol}
                if any(t not in times for t in range(int(review['cutoff'])+60,end+1,60)):
                    return dict(proof,reason='post_review_context_gap')
            service.invalidate(packet)
            latest=service.setup_annotation(order['order_id'],order['direction'])
            if latest.get('state')!='ALLOW' or latest.get('review_id')!=review['review_id']:
                return dict(proof,reason='review_invalidated_after_response')
            now=int(self.clock()*1000)
            if now//60000 != proof['checked_ms']//60000 or now>=order['expiry_ms']:
                return dict(proof,reason='setup_changed_during_validation')
            identity=json.dumps([source,[(r['ts_ms'],r['bar_json']) for r in rows],packet['packet_id'],latest['review_id']],sort_keys=True)
            return dict(proof,valid=True,evidence_id=hashlib.sha256(identity.encode()).hexdigest())
        except Exception:
            return dict(proof,reason='thesis_evidence_unavailable')
