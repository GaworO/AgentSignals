"""Cached contextual entry filter. Never calls AI or a broker."""
import os

import ab_ai_context


def required():
    return os.environ.get('V3_AI_REVIEW_REQUIRED', 'false') == 'true'


def check(direction, candidate_id):
    if os.environ.get('AB_AI_TRIGGER', 'PERIODIC') not in ('PERIODIC', 'SETUP'):
        return 'v3_ai:invalid_trigger', None
    if os.environ.get('AB_AI_TRIGGER', 'PERIODIC') == 'SETUP':
        oid = candidate_id
        for suffix in (':candidate', ':entry', ':dispatch', ':native'):
            if oid.endswith(suffix):
                oid = oid[:-len(suffix)]
                break
        review = (ab_ai_context.INSTANCE.setup_annotation(oid, direction)
                  if ab_ai_context.INSTANCE is not None else
                  dict(owner='AB_DIRECTIONAL', state='UNAVAILABLE', executable=False))
        return (None if review['state'] == 'ALLOW' else 'v3_ai:' + review['state'].lower()), review
    flag = os.environ.get('V3_AI_REVIEW_REQUIRED', 'false')
    if flag == 'false':
        return None, None
    if flag != 'true':
        return 'v3_ai:invalid_required_flag', None
    if direction not in ('LONG', 'SHORT'):
        return 'v3_ai:invalid_direction', None
    review = ab_ai_context.annotate(
        dict(dir=direction), candidate_id, owner='AB_DIRECTIONAL')
    if review.get('owner') != 'AB_DIRECTIONAL':
        return 'v3_ai:owner_mismatch', review
    if review.get('state') != 'ALLOW':
        return 'v3_ai:' + str(review.get('state', 'UNAVAILABLE')).lower(), review
    return None, review


def queue_setup(order):
    if os.environ.get('AB_AI_TRIGGER', 'PERIODIC') != 'SETUP':
        return None
    try:
        if ab_ai_context.INSTANCE is None:
            return dict(state='DISABLED')
        return ab_ai_context.INSTANCE.queue_setup(order)
    except Exception:
        return dict(state='UNAVAILABLE')
