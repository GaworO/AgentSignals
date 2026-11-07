"""Recording-backed choices, separate from the previous user-custom core.

This validates explicit, contemporaneous selections. It does not infer the
trader's discretion from an outcome or turn one recorded trade into a default.
"""
import hashlib
import math

VERSION = 'tanja-source-selections-v1'
PROFILES = {
    'm5_momentum': {'timeframes': [5], 'source': 'May 22, 01:19:30–01:22:36', 'swing_close': True},
    'selected_inversion': {'timeframes': [1, 2, 3, 5], 'source': 'May 26 / July 17, selected inversion', 'swing_close': False},
    'manipulation_retracement_inversion': {'timeframes': [1], 'source': 'User-reviewed sequence, October 13; both directions', 'swing_close': False},
    'htf_fvg_retracement_inversion': {'timeframes': [1], 'source': 'User-reviewed H1/H4 → M15 → M1 sequence, October 13; both directions', 'swing_close': False},
}


def contract():
    return dict(version=VERSION, profiles=PROFILES, automatic_discretion_verified=False,
        confirmation='Setup-specific timeframe and referenced confirmation assessment',
        target='Explicit context objective or explicitly selected false-move projection; no nearest-level default',
        partial='Selected objective and explicit contracts; no universal percentage or ES session-extreme trigger',
        trail='Selected protected swing and confirmation; buffer explicitly selected',
        additions='Require a separately reviewed catalyst/reconfirmation and lot plan; automatic add executor unresolved',
        account_overlay=dict(risk_fraction=.005, news_entry_before_seconds=300, news_entry_after_seconds=300,
            news_stop_seconds_before=60, adverse_entry_offset_points=1.5),
        broker_orders_enabled=False, semantic_fidelity_verified=False)


def _price(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0 and value*4 == round(value*4)


def review_selection(packet, selection):
    """Require auditable judgments for choices candle geometry alone cannot make."""
    missing = []
    profile = PROFILES.get(selection.get('setup_profile'))
    if profile is None:
        return ['SUPPORTED_SETUP_PROFILE_REQUIRED']
    if selection.get('timeframe_minutes') not in profile['timeframes']:
        missing.append('PROFILE_TIMEFRAME_MISMATCH')
    if profile['swing_close'] and (selection.get('requires_swing_break') is not True or selection.get('swing_break_mode') != 'close'):
        missing.append('M5_MOMENTUM_REQUIRES_SWING_CLOSE')
    if selection.get('confirmation_policy') != VERSION:
        missing.append('SOURCE_CONFIRMATION_POLICY_REQUIRED')
    # An assertion is an explicit judgment, not proof of fidelity. References
    # and receipt clocks prevent future labels from serving as current evidence.
    for field in ('setup_context', 'confirmation_strength', 'entry_timing', 'stop_invalidation', 'target_priority'):
        value = selection.get('assessments', {}).get(field, {})
        if not isinstance(value, dict):
            missing.append(field.upper()+'_REVIEW_REQUIRED'); continue
        refs = value.get('evidence_ids')
        at = value.get('available_at')
        if (value.get('accepted') is not True or not isinstance(value.get('reason'), str) or not value['reason'].strip()
            or not isinstance(refs, list) or not refs or any(ref not in packet['evidence'] for ref in refs)
            or type(at) is not int or not 0 <= at <= selection['selected_at']
            or any(packet['evidence'][ref]['available_at'] > at for ref in refs)
            or value.get('origin') not in ('human_review', 'model_review','saved_context_rule')):
            missing.append(field.upper()+'_REVIEW_REQUIRED')
        if value.get('origin')=='saved_context_rule':
            provenance=selection.get('context_provenance',{})
            if (not provenance or provenance!=packet.get('context_provenance')
                or not provenance.get('ai_run_id') or not provenance.get('packet_id')
                or type(provenance.get('available_at')) is not int or provenance['available_at']>packet['as_of']
                or type(provenance.get('expires_at')) is not int or provenance['expires_at']<=selection['selected_at']):
                missing.append('SAVED_CONTEXT_PROVENANCE_REQUIRED')
    # Old geometry-proxy target/stop choices must be replaced, not simply relabelled.
    if selection.get('selection_origin') != 'explicit_context_review':
        missing.append('EXPLICIT_CONTEXT_SELECTION_REQUIRED')
    return missing


def validate_management(spec, now):
    if not isinstance(spec, dict) or spec.get('version') != VERSION:
        raise ValueError('Source management selections required')
    rules = spec.get('partials')
    if not isinstance(rules, list): raise ValueError('Explicit partial list required; use [] for none')
    ids = set()
    for rule in rules:
        if not isinstance(rule, dict): raise ValueError('Partial selection must be an object')
        identity = rule.get('id')
        if not isinstance(identity, str) or not identity or identity in ids: raise ValueError('Unique partial ID required')
        ids.add(identity)
        if rule.get('quantity_policy') == 'one_third_original_floor':
            if 'quantity' in rule: raise ValueError('Choose a partial quantity or a quantity policy, not both')
        elif type(rule.get('quantity')) is not int or rule['quantity'] < 1:
            raise ValueError('Explicit partial contracts or reviewed one-third policy required')
        objective = rule.get('objective', {})
        if not isinstance(objective, dict): raise ValueError('Objective must be an object')
        if (objective.get('symbol') not in ('MNQ', 'ES') or not _price(objective.get('price'))
            or type(objective.get('available_at')) is not int or not 0 <= objective['available_at'] <= now
            or not objective.get('source') or not rule.get('reason')):
            raise ValueError('Dated selected objective and reason required')
        if objective.get('direction') not in ('above', 'below'): raise ValueError('Objective crossing direction required')
    if spec.get('trail') != 'explicit_confirmed_swing': raise ValueError('Explicit structural trail required')
    if spec.get('additions') != 'separate_review_required': raise ValueError('Automatic additions are not established by the recordings')
    return spec


def partial_quantity(rule, original_quantity):
    return original_quantity//3 if rule.get('quantity_policy')=='one_third_original_floor' else rule['quantity']


def partial_proposal(*, spec, position_id, remaining_quantity, completed, now, quotes, original_quantity=None):
    """Touch of a previously selected objective using a fresh supplied quote.

    ES objectives may be weekly/session/other context levels, but are never
    invented as today's newest extreme. Quantities remain case-specific.
    """
    validate_management(spec, now)
    for rule in spec['partials']:
        if rule['id'] in completed: continue
        if rule.get('quantity_policy') and (type(original_quantity) is not int or original_quantity<1):
            return dict(action='NONE',reason='ORIGINAL_POSITION_QUANTITY_REQUIRED')
        quantity=partial_quantity(rule,original_quantity)
        if quantity<1:continue
        objective = rule['objective']; quote = quotes.get(objective['symbol'], {})
        at = quote.get('at'); value = quote.get('price')
        if (type(at) is not int or not objective['available_at'] <= at <= now or now-at > 2 or not _price(value)): continue
        sign = 1 if objective['direction'] == 'above' else -1
        if sign*(value-objective['price']) < 0: continue
        if quantity >= remaining_quantity:
            return dict(action='NONE', reason='SELECTED_PARTIAL_MUST_PRESERVE_RUNNER')
        intent = hashlib.sha256(f'{VERSION}:{position_id}:{rule["id"]}'.encode()).hexdigest()
        return dict(action='REVIEW_PARTIAL_PROPOSAL', quantity=quantity, rule_id=rule['id'],
            intent_id=intent, objective=objective, reason=rule['reason'], is_order=False)
    return dict(action='NONE', reason='NO_SELECTED_OBJECTIVE_REACHED')


def structural_stop(*, selection, bars, direction, opened_at, now, current_stop, market_price):
    """Explicit protected swing after its selected closed-bar confirmation.

    'break_prior_extreme' models July 17's dip then new high; 'rebound' allows
    a separately reviewed swing recovery. Neither invents a fixed pivot width.
    """
    no = dict(action='NONE', reason='STRUCTURAL_SELECTION_REQUIRED')
    if not isinstance(selection, dict) or direction not in ('long', 'short'): return no
    sign = 1 if direction == 'long' else -1
    if not all(_price(v) for v in (current_stop, market_price)): return no
    selected_at = selection.get('selected_at'); buffer = selection.get('buffer_ticks')
    if (type(selected_at) is not int or not opened_at <= selected_at <= now
        or type(buffer) is not int or buffer < 0 or not selection.get('reason')): return no
    visible={}
    for b in bars:
        t=b.get('time')
        if type(t) is not int or t<0 or t%60: return no
        if t+60>now: continue
        received=b.get('received_at',t+60)
        if type(received) not in (int,float) or not math.isfinite(received) or received<t+60:return no
        if received>now:continue
        if t in visible:return no
        visible[t]=b
    closed={t:b for t,b in visible.items() if t+60<=selected_at and b.get('received_at',t+60)<=selected_at}
    anchor = closed.get(selection.get('anchor_time')); confirmation = closed.get(selection.get('confirmation_time'))
    if not anchor or not confirmation or not opened_at <= anchor['time'] < confirmation['time']: return no
    if any(not all(_price(b.get(k)) for k in ('open','high','low','close')) or not b['low'] <= min(b['open'],b['close']) <= max(b['open'],b['close']) <= b['high'] for b in closed.values()): return no
    path = sorted((b for b in closed.values() if anchor['time'] <= b['time'] <= confirmation['time']), key=lambda b:b['time'])
    if [b['time'] for b in path] != list(range(anchor['time'], confirmation['time']+1, 60)): return no
    adverse = 'low' if direction == 'long' else 'high'
    favorable = 'high' if direction == 'long' else 'low'
    if any(sign*(b[adverse]-anchor[adverse]) < 0 for b in path[1:]): return no
    mode = selection.get('confirmation')
    if mode == 'break_prior_extreme':
        level = selection.get('prior_extreme', {})
        if (not _price(level.get('price')) or type(level.get('available_at')) is not int
            or not 0 <= level['available_at'] <= anchor['time'] or not level.get('source')
            or sign*(confirmation['close']-level['price']) <= 0): return no
    elif mode == 'rebound':
        if sign*(confirmation['close']-anchor[favorable]) <= 0: return no
    else: return no
    stop = anchor[adverse]-sign*.25*buffer
    # A selection cannot revive a swing already invalidated by later known data.
    if any(t not in visible for t in range(anchor['time'],now//60*60,60)):return no
    later = [b for t,b in visible.items() if t > confirmation['time']]
    if any(not _price(b.get(adverse)) or sign*(b[adverse]-anchor[adverse]) <= 0 for b in later): return no
    if not _price(stop) or sign*(stop-current_stop) <= 0 or sign*(market_price-stop) <= 0: return no
    return dict(action='REVIEW_STOP_PROPOSAL', stop_price=stop, reason='SELECTED_CONFIRMED_STRUCTURAL_SWING', is_order=False)


def require_source_selection(result):
    """Keep proxy scans visible without promoting them into faithful trade plans."""
    result['strategy_policy'] = contract()
    if result.get('compiled', {}).get('plan'):
        result['diagnostic_plan'] = result['compiled']['plan']
        result['compiled']['plan'] = None
        result['compiled']['state'] = 'NEEDS_SOURCE_SELECTION'
    if result.get('state') == 'PLAN_READY':
        result['state'] = 'NEEDS_SOURCE_SELECTION'
        result['reasons'] = ['SETUP_SPECIFIC_CONFIRMATION_STOP_TARGET_AND_MANAGEMENT_REQUIRED']
    return result
