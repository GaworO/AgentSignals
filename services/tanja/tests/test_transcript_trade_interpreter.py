"""Focused safety checks; invented phrases are unit inputs, not historical labels."""
import sys
from pathlib import Path
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from transcript_trade_interpreter import Interpreter, PaperCopy, numeric_fields, statements
from transcript_ingestion import TranscriptJournal
from store import Store

ACCOUNT = dict(balance=50000, minimum_balance=48000, daily_loss_remaining=1000, committed_risk=0)


def interpret(engine, quote, t=0):
    return engine.interpret(dict(source_timestamp='00:00:%02d' % t, source_seconds=t,
                                 source_line=1, sentence_index=t, quote=quote), 'unit-source')


class SafetyTests(unittest.TestCase):
    def test_limit_reports_and_independent_fills(self):
        i = Interpreter(); p = PaperCopy(ACCOUNT)
        e = interpret(i, 'I placed my limit order: buy 2 MNQ, limit at 20000, SL at 19990, TP at 20020.')[0]
        a = p.apply(e, 0)
        self.assertEqual(a['disposition'], 'PAPER_ORDER_SUBMITTED_NO_FILL')
        self.assertEqual(i.state, 'PENDING_ORDER')
        fill = interpret(i, 'Okay, I just got filled.', 1)[0]
        p.apply(fill, 1)
        self.assertEqual(i.state, 'OPEN_POSITION')
        self.assertEqual(p.state, 'PENDING_ORDER')
        with self.assertRaisesRegex(ValueError, 'LIMIT_PRICE_VIOLATION'):
            p.acknowledge_fill(e['event_id'], 'bad', 20000.25, 2, 1)
        p.acknowledge_fill(e['event_id'], 'independent-fill', 20000, 2, 1)
        p.apply(interpret(i, 'I moved my stop to 19995.', 2)[0], 2)
        self.assertEqual(p.position['stop'], 19995)
        self.assertEqual(p.apply(interpret(i, 'I moved my stop to 19980.', 3)[0], 3)['reasons'], ['STOP_WIDENING_REJECTED'])
        partial = interpret(i, 'I took partials, quantity 1, entry at 20010.', 4)[0]
        self.assertEqual(p.apply(partial, 4)['disposition'], 'PAPER_EXIT_REQUEST_NO_FILL')
        self.assertEqual(p.position['quantity'], 2)
        p.acknowledge_fill(e['event_id'], 'exit-one', 20010, 1, 4, exit=True)
        self.assertEqual(p.state, 'PARTIAL')
        closed = interpret(i, 'I closed my entire position, entry at 20010.', 5)[0]
        self.assertEqual(i.state, 'CLOSED')
        p.apply(closed, 5)
        p.acknowledge_fill(e['event_id'], 'exit-rest', 20010, 1, 5, exit=True)
        self.assertEqual(p.state, 'CLOSED')
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_SIMULATED_FILL'):
            p.acknowledge_fill(e['event_id'], 'exit-rest', 20010, 1, 5, exit=True)

    def test_market_submission_is_not_fill(self):
        i = Interpreter(); p = PaperCopy(ACCOUNT)
        e = interpret(i, 'I clicked market: buy 2 MNQ, entry at 20000, SL at 19990.')[0]
        self.assertEqual(e['action'], 'MARKET_ORDER')
        p.apply(e, 0)
        p.apply(interpret(i, 'I just got filled.', 1)[0], 1)
        self.assertEqual(p.state, 'PENDING_ORDER')
        p.acknowledge_fill(e['event_id'], 'market-sim', 20000, 2, 1)
        self.assertEqual(p.state, 'OPEN_POSITION')

    def test_future_negation_hold_and_idiom(self):
        i = Interpreter()
        self.assertEqual(interpret(i, 'See if I get filled here.'), [])
        self.assertEqual(interpret(i, 'I did not get filled.'), [])
        self.assertEqual(interpret(i, 'Hold the fort down.'), [])
        e = interpret(i, 'I am holding my position.')[0]
        self.assertEqual(e['action'], 'HOLD')
        self.assertEqual(i.state, 'NO_POSITION')
        self.assertEqual(e['transition_error'], 'NO_REPORTED_POSITION')

    def test_no_future_backfill_or_plan_parameter_leak(self):
        i = Interpreter()
        plan = interpret(i, 'I would buy 5 MNQ, entry at 20000, SL at 19990.')[0]
        self.assertEqual(plan['action'], 'TRADE_PLAN')
        placed = interpret(i, 'I have my limit in.', 1)[0]
        self.assertEqual(placed['reported_position']['entry'], 'UNKNOWN')
        interpret(i, 'I set my stop at 19995.', 2)
        self.assertEqual(placed['reported_position']['stop'], 'UNKNOWN')
        self.assertEqual(placed['fields']['quantity'], 'UNKNOWN')

    def test_conflicting_side_rejected(self):
        i = Interpreter()
        interpret(i, 'I placed my limit order: buy 2 MNQ, entry at 20000, SL at 19990.')
        e = interpret(i, 'I shorted MNQ.', 1)[0]
        self.assertEqual(e['transition_error'], 'CONTRADICTORY_SIDE')
        self.assertEqual(i.state, 'PENDING_ORDER')

    def test_unknown_numbers_and_symbols(self):
        for phrase in ['entry at 20000 or 20010', 'entry at 20000-20010', 'entry at 20000s', 'entry at 9:30', 'entry at about 20000']:
            self.assertEqual(numeric_fields(phrase)['entry'], 'UNKNOWN', phrase)
        self.assertEqual(numeric_fields('buy MNQ and ES')['instrument'], 'UNKNOWN')
        self.assertEqual(numeric_fields('partials like half off the table')['quantity'], 'UNKNOWN')

    def test_stale_duplicate_and_risk(self):
        e = interpret(Interpreter(), 'I placed my limit order: buy 100 MNQ, entry at 20000, SL at 19990.')[0]
        p = PaperCopy(ACCOUNT)
        a = p.apply(e, 0)
        self.assertEqual(a['paper_order']['quantity'], 12)
        self.assertLessEqual(12*10*2, 50000*.005)
        self.assertIn('DUPLICATE_EVENT', p.apply(e, 0)['reasons'])
        self.assertIn('STALE_OR_FUTURE_EVENT', PaperCopy(ACCOUNT).apply(e, 16)['reasons'])
        wrong = interpret(Interpreter(), 'I placed my limit order: buy 2 MNQ, entry at 20000, SL at 20010.')[0]
        self.assertIn('INVALID_STOP_DIRECTION', PaperCopy(ACCOUNT).apply(wrong, 0)['reasons'])
        prop = dict(ACCOUNT, minimum_balance=49999)
        self.assertIn('RISK_LIMIT_NO_CONTRACT_FITS', PaperCopy(prop).apply(e, 0)['reasons'])

    def test_limit_modify_cancel(self):
        i = Interpreter(); p = PaperCopy(ACCOUNT)
        p.apply(interpret(i, 'I placed my limit order: buy 2 MNQ, entry at 20000, SL at 19990.')[0], 0)
        modified = interpret(i, 'I moved my limit to 20001, SL at 19990.', 1)[0]
        self.assertEqual(p.apply(modified, 1)['disposition'], 'PAPER_LIMIT_REPLACED_NO_FILL')
        canceled = interpret(i, 'I cancelled my limit.', 2)[0]
        self.assertEqual(p.apply(canceled, 2)['disposition'], 'PAPER_ORDER_CANCELLED')
        self.assertEqual(i.state, 'NO_POSITION')
        self.assertEqual(p.state, 'NO_POSITION')

    def test_shadow_and_sentence_timing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'source.txt'
            path.write_text('00:00:01\nI have my limit in. Okay, I just got filled.\n')
            ss = list(statements(path))
            self.assertEqual([s['source_seconds'] for s in ss], [1, 1])
            i = Interpreter(); journal = TranscriptJournal(Store(tmp))
            session, attempt, _ = journal.start(dict(id='unit', audio_url=str(path), started_at=0, authorization='unit'), 'historical_transcript')
            event = i.interpret(ss[0], 'hash')[0]
            self.assertTrue(journal.record_decision(session, attempt, event))
            self.assertFalse(journal.record_decision(session, attempt, event))
            shadow = journal.shadow()
            self.assertEqual(len(shadow), 1)
            self.assertEqual(shadow[0]['structured_decision']['action'], 'LIMIT_ORDER_PLACED')
            self.assertFalse(shadow[0]['executable'])
            self.assertFalse(shadow[0]['orders_enabled'])


if __name__ == '__main__': unittest.main()
