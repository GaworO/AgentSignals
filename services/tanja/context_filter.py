"""Offline audit of explicitly annotated momentum-long context, not a bar classifier.

All times use one caller-declared clock. Transcript elapsed seconds are NOT market
execution times. A REVIEW_CANDIDATE still needs a complete price/risk plan and is
never an order authorization. Qualitative observations are supplied, not inferred.
"""
BASE_REQUIREMENTS = (
    'bullish_thesis_supported', 'poi_reached', 'sellside_event_observed',
    'selected_inversion_closed', 'nasdaq_bullish_confirmation', 'target_identified',
)
CONDITIONAL_REQUIREMENTS = {
    'requires_swing_break': 'selected_swing_high_broken',
    'requires_smt': 'selected_smt_present',
    'requires_followthrough': 'bullish_followthrough',
}
WARNINGS = ('london_expanded', 'no_high_impact_news', 'range_near_atr', 'es_lagging')


def review_context(recipe, observations, *, as_of, clock_id):
    errors = []
    if type(as_of) is not int or as_of < 0 or not isinstance(clock_id, str) or not clock_id:
        raise ValueError('Explicit nonnegative cutoff and clock identity required')
    if recipe.get('kind') != 'momentum_long':
        errors.append('UNSUPPORTED_RECIPE')
    if type(recipe.get('timeframe_minutes')) is not int or recipe['timeframe_minutes'] not in (1, 2, 3, 5):
        errors.append('EXPLICIT_TIMEFRAME_REQUIRED')
    required = list(BASE_REQUIREMENTS)
    for flag, field in CONDITIONAL_REQUIREMENTS.items():
        if type(recipe.get(flag)) is not bool:
            errors.append('EXPLICIT_' + flag.upper() + '_REQUIRED')
        elif recipe[flag]:
            required.append(field)
    groups = {}
    future_ignored = 0
    for item in observations:
        if item.get('clock_id') != clock_id:
            errors.append('MIXED_CLOCK_OR_SESSION')
            continue
        at = item.get('available_at')
        if type(at) is not int or at < 0:
            errors.append('INVALID_AVAILABILITY')
            continue
        if at > as_of:
            future_ignored += 1
            continue
        if not isinstance(item.get('source'), str) or not item['source'].strip():
            errors.append('UNSOURCED_OBSERVATION')
            continue
        if type(item.get('value')) is not bool and item.get('value') is not None:
            errors.append('INVALID_OBSERVATION_VALUE')
            continue
        field = item.get('field')
        if field not in set(BASE_REQUIREMENTS) | set(CONDITIONAL_REQUIREMENTS.values()) | set(WARNINGS):
            errors.append('UNKNOWN_OBSERVATION_FIELD')
            continue
        if field == 'selected_inversion_closed' and item.get('timeframe_minutes') != recipe.get('timeframe_minutes'):
            # A closed M1 inversion does not confirm the chosen M5 setup.
            continue
        expiry = item.get('expires_at')
        if expiry is not None and (type(expiry) is not int or expiry <= at):
            errors.append('INVALID_OBSERVATION_EXPIRY')
            continue
        groups.setdefault(field, []).append(item)
    latest = {}
    for field, items in groups.items():
        newest = max(x['available_at'] for x in items)
        versions = [x for x in items if x['available_at'] == newest]
        if len({x['value'] for x in versions}) != 1:
            errors.append('CONFLICTING_OBSERVATIONS_' + field.upper())
            continue
        # Expired latest evidence becomes unknown; never revive an older True.
        if any(x.get('expires_at') is not None and x['expires_at'] <= as_of for x in versions):
            continue
        latest[field] = versions[-1]
    unknown = [field for field in required if latest.get(field, {}).get('value') is None]
    unmet = [field for field in required if latest.get(field, {}).get('value') is False]
    return {
        'state': 'INVALID_EVIDENCE' if errors else 'WAIT' if unmet else 'NEEDS_CONTEXT' if unknown else 'REVIEW_CANDIDATE',
        'as_of': as_of, 'clock_id': clock_id, 'unmet': unmet, 'unknown': unknown,
        'warnings': [field for field in WARNINGS if latest.get(field, {}).get('value') is True],
        'used_evidence': {field: item for field, item in latest.items() if field in required or field in WARNINGS},
        'errors': sorted(set(errors)), 'future_observations_ignored': future_ignored,
        'executable': False, 'automatic_bar_classification': False,
        'note': 'Annotation consistency only. Entry price, initial stop, target price, size, costs, account and broker checks are separate.',
    }
