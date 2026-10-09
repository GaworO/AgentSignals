"""Explicit operator-only context test; not an HTTP route or scheduled worker."""
import json
import os
import time
from ai_review import AIReview, MAX_REQUEST_BYTES, encoded
from store import Store


class ManualObservationReview(AIReview):
    def gate(self, now, context):
        status = super().gate(now, context)
        if status == 'OUTSIDE_REVIEW_WINDOW':
            # Only the pilot schedule is waived. Quality, freshness, size, daily
            # budget, duplicate prevention and ambiguous-failure rules remain.
            return 'READY' if len(encoded(self.make_payload(context['packet'])).encode()) <= MAX_REQUEST_BYTES else 'INPUT_TOO_LARGE'
        return status

    def latest_context(self):
        context = super().latest_context()
        if context:
            context['review_purpose'] = 'USER_REQUESTED_MANUAL_OBSERVATION_TEST'
            context['schedule_exception'] = 'One explicit test outside pilot hours; no execution'
        return context


if __name__ == '__main__':
    review = ManualObservationReview(Store(os.environ.get('DATA_DIR','/data')), os.environ)
    context = review.latest_context()
    print(json.dumps({'preflight':review.gate(time.time(),context),'orders_enabled':False}),flush=True)
    attempted = review.process_one()
    print(json.dumps({'attempted':attempted,'ai':review.state(time.time(),review.latest_context())}),flush=True)
