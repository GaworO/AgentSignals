"""Saved AI thesis gate for new research entries, never an execution adapter.

ES/MNQ AI direction filters the independent ES/NQ selector; it does not replace
its POI, confirmation, MNQ stop or target calculations. Fifteen-minute lifetime
and touch invalidation are conservative engineering policies, not Tanja claims.
"""
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo
from entry_review import validate_decision
from context_v3 import point_price
from review_calendar import review_day

VERSION = 'saved-ai-thesis-gate-v1'
TTL_SECONDS = 900
NY = ZoneInfo('America/New_York')


def evaluate(row, markets, *, cutoff, now):
    out = dict(version=VERSION, state='BLOCKED', reasons=[], ai_run_id=None,
               market_cutoff=cutoff, checked_at=now, expires_at=None,
               direction=None, orders_enabled=False,
               scope='ES/MNQ AI thesis filter on ES/NQ mechanical entries; no cross-symbol price copying')
    def block(reason):
        out['reasons'] = [reason]
        return out
    if not review_day(now)['new_reviews_allowed']:
        return block('AI_CONTEXT_NON_TRADING_REVIEW_DAY')
    if row is None:
        return block('AI_CONTEXT_MISSING')
    out.update(ai_run_id=row['id'], review_status=row['status'], available_at=row.get('finished'), source_cutoff=row.get('cutoff'),packet_id=row.get('packet_id'))
    if row['status'] != 'validated':
        return block('AI_CONTEXT_LATEST_REVIEW_NOT_VALIDATED')
    try:
        source, started, finished = (row[k] for k in ('cutoff','started','finished'))
        if not all(type(t) in (int,float) and math.isfinite(t) for t in (source,started,finished,cutoff,now)):
            return block('AI_CONTEXT_INVALID_TIMESTAMPS')
        if not source <= started <= finished <= now or source > cutoff or cutoff > now:
            return block('AI_CONTEXT_INVALID_TIMESTAMPS')
        decision = json.loads(row['decision'])
        # Extended plans become available at response receipt. They never fill
        # on the historical trigger candle; the simulator needs a later quote.
        if finished > cutoff and not decision.get('entry_setup'):
            return block('AI_CONTEXT_NOT_AVAILABLE_AT_BAR_CLOSE')
        expiry = source + TTL_SECONDS
        out['expires_at'] = expiry
        if datetime.fromtimestamp(source,NY).date() != datetime.fromtimestamp(now,NY).date():
            return block('AI_CONTEXT_PREVIOUS_SESSION')
        if now >= expiry or cutoff >= expiry:
            return block('AI_CONTEXT_EXPIRED')
        snapshot = json.loads(row['snapshot'])
        packet = snapshot['packet']
        if packet['packet_id'] != row['packet_id'] or packet['as_of'] != source:
            return block('AI_CONTEXT_EVIDENCE_MISMATCH')
        if not source <= snapshot['frozen_at'] <= snapshot['processed_at'] <= started:
            return block('AI_CONTEXT_INVALID_TIMESTAMPS')
        checked = validate_decision(packet, decision)
        context = decision['context']
        out.update(direction=context['bias'], ai_decision=context['decision'], explanation=decision['rationale'])
        if context['decision'] != 'candidate':
            return block('AI_CONTEXT_'+context['decision'].upper())
        if context['bias'] not in ('long','short'):
            return block('AI_CONTEXT_DIRECTION_UNKNOWN')
        analysis=decision['analysis_symbol']
        if analysis not in ('MNQ','NQ') or decision['execution_symbol'] != 'MNQ':
            return block('AI_CONTEXT_UNSUPPORTED_SOURCE')
        if checked['context_review_state'] != 'CONDITIONS_MET' or checked['missing_plan_fields'] or context['missing_context']:
            return block('AI_CONTEXT_INCOMPLETE')
        if context['claims']['structure_intact']['value'] is not True:
            return block('AI_CONTEXT_STRUCTURE_NOT_CONFIRMED')
        if decision['entry_mode'] != 'immediate_after_confirmation' and not (decision['entry_mode']=='retracement' and checked.get('entry_setup_verified')):
            return block('AI_CONTEXT_UNSUPPORTED_ENTRY_MODE')
        if decision['invalidation'] is None:
            return block('AI_CONTEXT_INVALIDATION_REQUIRED')
        invalidation = point_price(packet, decision['invalidation'], analysis)
        stop = decision['initial_stop']['price']
        long = context['bias'] == 'long'
        # Either the explicit thesis anchor or the proposed stop can revoke it.
        boundary = (max(invalidation, stop) if long else min(invalidation, stop)) if analysis=='MNQ' else invalidation
        out.update(invalidation_price=boundary, invalidation_symbol=analysis, invalidation_mode='any closed M1 wick touch since source cutoff')
        own = sorted([b for b in markets[analysis] if source <= b['time'] and b['time']+60 <= cutoff and b.get('received_at',b['time']+60) <= now],key=lambda b:b['time'])
        expected = list(range(int(source),int(cutoff),60))
        if [b['time'] for b in own] != expected:
            return block('AI_CONTEXT_MONITORING_GAP')
        targets = [t['price'] for t in decision['management']['targets']]
        for b in own:
            if not all(type(b[k]) in (int,float) and math.isfinite(b[k]) for k in ('high','low')):
                return block('AI_CONTEXT_INVALID_PRICE')
            if b['low'] <= boundary if long else b['high'] >= boundary:
                out['invalidated_at'] = b['time']+60
                return block('AI_CONTEXT_PRICE_INVALIDATED')
        execution=sorted([b for b in markets['MNQ'] if source<=b['time'] and b['time']+60<=cutoff and b.get('received_at',b['time']+60)<=now],key=lambda b:b['time'])
        if [b['time'] for b in execution]!=expected:return block('MNQ_EXECUTION_MONITORING_GAP')
        for b in execution:
            if (b['low']<=stop if long else b['high']>=stop):return block('MNQ_EXECUTION_STOP_ALREADY_TOUCHED')
            if any(b['high'] >= t if long else b['low'] <= t for t in targets):
                out['invalidated_at'] = b['time']+60
                return block('AI_CONTEXT_OBJECTIVE_ALREADY_REACHED')
        out.update(state='PERMITTED',reasons=[])
        return out
    except (ValueError,TypeError,KeyError,IndexError,OverflowError):
        return block('AI_CONTEXT_AUDIT_REJECTED')


def apply(result, gate):
    """Keep mechanical evidence inspectable but remove any blocked compiled plan."""
    result['geometry_state'] = result['state']
    result['ai_context'] = gate
    if result['state'] != 'PLAN_READY':
        return result
    reasons = list(gate['reasons'])
    selection = result['selected']['selection']
    if gate['state'] == 'PERMITTED' and selection['direction'] != gate['direction']:
        reasons = ['AI_CONTEXT_DIRECTION_CONFLICT']
    if reasons or gate['state'] != 'PERMITTED':
        result.update(state='AI_CONTEXT_BLOCKED',reasons=reasons or ['AI_CONTEXT_BLOCKED'])
        result['compiled']['plan'] = None
    else:
        expiry = min(selection['context_valid_until'],gate['expires_at'])
        selection['context_valid_until'] = expiry
        result['compiled']['plan']['context_valid_until'] = expiry
        if selection.get('entry_mode')=='retracement':
            until=min(selection['entry_valid_until'],expiry)
            selection['entry_valid_until']=until
            result['compiled']['plan']['entry_valid_until']=until
        result['compiled']['plan']['ai_run_id'] = gate['ai_run_id']
    return result
