"""Candidate tests use mock model responses, never historical gold as inference."""
import sys
from pathlib import Path
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from context_trade_interpreter import ContextInterpreter, GPTCandidates
from transcript_trade_interpreter import PaperCopy, numeric_fields


def stmt(quote, index=0):
    return dict(quote=quote, source_timestamp='00:00:%02d' % index,
                source_seconds=index, source_line=1, sentence_index=index)


def candidate(action, quote, status='SELF_REPORTED', link='UNKNOWN', **fields):
    return dict(action=action, quote=quote, status=status, link_to=link,
                fields=dict({k: 'UNKNOWN' for k in numeric_fields('')}, **fields))


class ContextTests(unittest.TestCase):
    def test_context_is_previous_only(self):
        requests = []
        def provider(c): requests.append(c); return dict(candidates=[])
        i = ContextInterpreter(provider, window_size=2)
        for n in range(5): i.interpret(stmt('statement %s' % n, n), 'hash')
        self.assertEqual([x['quote'] for x in requests[-1]['previous']], ['statement 2', 'statement 3'])
        self.assertNotIn('statement 4', str(requests[-1]['previous']))

    def test_hallucinated_quote_numbers_and_contract(self):
        quote = 'I have my limit in.'
        proposed = candidate('LIMIT_ORDER_PLACED', quote, entry='20000', instrument='MNQ')
        i = ContextInterpreter(lambda _: dict(candidates=[proposed]))
        event = i.interpret(stmt(quote), 'hash')[0]
        self.assertEqual(event['fields']['entry'], 'UNKNOWN')
        self.assertEqual(event['fields']['instrument'], 'UNKNOWN')
        self.assertEqual(len(event['ai_field_rejections']), 2)
        self.assertEqual(PaperCopy().apply(event, 0)['disposition'], 'REJECTED')
        bad = candidate('LIMIT_ORDER_FILLED', 'I got filled.')
        j = ContextInterpreter(lambda _: dict(candidates=[bad]))
        self.assertEqual(j.interpret(stmt('The gap filled.'), 'hash'), [])
        self.assertEqual(j.rejections[0]['reason'], 'UNANCHORED_OR_NONUNIQUE_QUOTE')

    def test_cannot_hide_negation_or_intention(self):
        for text in ['I might say I just got filled.', 'I did not say I just got filled.']:
            c = candidate('LIMIT_ORDER_FILLED', 'I just got filled.')
            i = ContextInterpreter(lambda _: dict(candidates=[c]))
            self.assertEqual(i.interpret(stmt(text), 'hash'), [])
            self.assertEqual(i.validator.state, 'NO_POSITION')
        i = ContextInterpreter(lambda _: dict(candidates=[candidate('FULL_EXIT', 'close this')]))
        e = i.interpret(stmt("I'm going to close this before it gets lower."), 'hash')[0]
        self.assertEqual(e['evidence_status'], 'INTENTION_OR_AMBIGUOUS')
        self.assertEqual(i.validator.state, 'NO_POSITION')

    def test_linked_limit_fill_does_not_fill_paper(self):
        responses = []
        i = ContextInterpreter(lambda _: responses.pop(0))
        quote = 'I have my limit in.'
        responses.append(dict(candidates=[candidate('LIMIT_ORDER_PLACED', quote)]))
        placed = i.interpret(stmt(quote), 'hash')[0]
        responses.append(dict(candidates=[candidate('LIMIT_ORDER_FILLED', 'I just got filled.', link=placed['event_id'])]))
        fill = i.interpret(stmt('I just got filled.', 1), 'hash')[0]
        self.assertEqual(i.validator.state, 'OPEN_POSITION')
        p = PaperCopy(); p.apply(placed, 0); p.apply(fill, 1)
        self.assertEqual(p.state, 'NO_POSITION')
        self.assertEqual(placed['reported_position']['entry'], 'UNKNOWN')

    def test_unsupported_links_market_and_hold(self):
        i = ContextInterpreter(lambda _: dict(candidates=[candidate('LIMIT_ORDER_FILLED', 'I just got filled.', link='future-id')]))
        self.assertEqual(i.interpret(stmt('I just got filled.'), 'hash'), [])
        self.assertEqual(i.rejections[0]['reason'], 'UNSUPPORTED_TRADE_LINK')
        i = ContextInterpreter(lambda _: dict(candidates=[candidate('MARKET_ORDER', 'I entered.')]))
        self.assertEqual(i.interpret(stmt('I entered.'), 'hash'), [])
        i = ContextInterpreter(lambda _: dict(candidates=[candidate('HOLD', "I'm comfortable with this trade.")]))
        e = i.interpret(stmt("I'm comfortable with this trade."), 'hash')[0]
        self.assertEqual(i.validator.state, 'NO_POSITION')
        self.assertEqual(e['transition_error'], 'NO_REPORTED_POSITION')

    def test_ambiguous_numbers_stay_unknown(self):
        q = 'I put my TP in 265s.'
        i = ContextInterpreter(lambda _: dict(candidates=[candidate('TARGET_SET', q, target='23265')]))
        e = i.interpret(stmt(q), 'hash')[0]
        self.assertEqual(e['fields']['target'], 'UNKNOWN')
        self.assertIn('target', e['ai_field_rejections'])

    def test_approval_gate_no_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'PAID_API_APPROVAL_REQUIRED'):
                GPTCandidates('not-a-key', Path(tmp)/'ledger', .50)
            with self.assertRaisesRegex(ValueError, 'BUDGET_MUST'):
                GPTCandidates('not-a-key', Path(tmp)/'ledger', 1, approved=True)
            client = GPTCandidates('not-a-key', Path(tmp)/'ledger', .0000001, approved=True)
            with self.assertRaisesRegex(RuntimeError, 'APPROVED_BUDGET_EXHAUSTED'):
                client(dict(current=stmt('hello')))


if __name__ == '__main__': unittest.main()
