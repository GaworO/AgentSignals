"""Entry grounding, TEST-only network boundary, Shadow persistence and no retries."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store import Store
from transcript_ingestion import TranscriptJournal
from transcript_trade_interpreter import Interpreter
from transcript_entry_bridge import EntryBridge, send_entry_test, validate_test_payload
from connection_test import TransportError

URL = 'https://webhooks.traderspost.io/trading/webhook/00000000-0000-0000-0000-000000000000/test_password_only'
LIMIT = 'I placed my limit order: buy 1 MNQ, limit at 20000, SL at 19990, TP at 20020.'
MARKET = 'I clicked market: sell 1 MNQ, entry at 20000, SL at 20010, TP at 19980.'


class EntryBridgeTests(unittest.TestCase):
    def setUp(self):
        quiet = patch('sys.stdout', io.StringIO())
        quiet.start(); self.addCleanup(quiet.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.journal = TranscriptJournal(Store(self.temp.name))
        self.session, self.attempt, _ = self.journal.start(dict(id='unit',audio_url='synthetic-text:unit',started_at=1000,authorization='Invented unit quotes'), 'synthetic_fixture')
        self.bridge = EntryBridge(self.journal,'MNQZ2026',URL)

    def tearDown(self):
        self.temp.cleanup()

    def event(self, quote=LIMIT, historical=False):
        statement = dict(quote=quote,source_seconds=0,source_timestamp='00:00:00',source_line=1,sentence_index=0)
        event = Interpreter().interpret(statement,hashlib.sha256(quote.encode()).hexdigest())[0]
        event.update(historical_replay=historical,executable=False,orders_enabled=False)
        return event

    def preview(self, event=None, at=1000, kind='synthetic_fixture', now=1000):
        event = event or self.event()
        self.journal.record_decision(self.session,self.attempt,event)
        return self.bridge.preview(self.session,'decision:'+event['event_id'],event,at,kind,now)

    def test_limit_market_direction_prices_and_shadow(self):
        limit = self.preview()['payload']
        market = self.preview(self.event(MARKET))['payload']
        self.assertEqual((limit['orderType'],limit['action'],limit['limitPrice']),('limit','buy',20000))
        self.assertEqual(limit['stopLoss'],{'type':'stop','stopPrice':19990})
        self.assertEqual(limit['takeProfit'],{'limitPrice':20020})
        self.assertEqual((market['orderType'],market['action'],market['sentiment']),('market','sell','bearish'))
        self.assertNotIn('limitPrice',market)
        self.assertTrue(all(p['test'] is True for p in (limit,market)))
        self.assertEqual(len(self.journal.shadow()),2)
        self.assertEqual(self.journal.shadow()[0]['entry_bridge'][0]['payload'],limit)

    def test_missing_historical_wrong_instrument_no_resizing(self):
        cases = [
            (self.event('I have my limit in.'),'MISSING_ENTRY'),
            (self.event(historical=True),'HISTORICAL_SOURCE_NOT_SENDABLE'),
            (self.event(LIMIT.replace('MNQ','ES')),'INSTRUMENT_NOT_MNQ_NO_AUTOMATIC_CONVERSION'),
            (self.event(LIMIT.replace('buy 1','buy 2')),'TEST_QUANTITY_MUST_BE_ONE_NO_AUTOMATIC_RESIZING')]
        for event,reason in cases:
            with self.subTest(reason=reason):
                preview=self.preview(event)
                self.assertIsNone(preview['payload']);self.assertIn(reason,preview['reasons'])

    def test_plans_fill_reports_and_conditional_placement_are_not_entries(self):
        for quote in ('I would buy 1 MNQ, entry at 20000, SL at 19990.',
                      'I just got filled.', 'If I placed my limit order: buy 1 MNQ, limit at 20000, SL at 19990.'):
            self.assertIsNone(self.preview(self.event(quote))['payload'])

    def test_freshness_and_grounding(self):
        for captured in (984,1001,None):
            # Different event source identities keep separate rejected observations.
            event=self.event();event['event_id']=hashlib.sha256(str(captured).encode()).hexdigest()
            preview=self.preview(event,at=captured)
            self.assertIn('STALE_FUTURE_OR_UNKNOWN_CAPTURE_TIME',preview['reasons'])
        event=self.event();event['fields']['entry']=19000
        self.assertIn('FIELDS_NOT_GROUNDED_IN_CURRENT_QUOTE',self.preview(event)['reasons'])

    def test_invalid_stop_target_and_tick(self):
        for quote in (LIMIT.replace('SL at 19990','SL at 20010'),
                      LIMIT.replace('SL at 19990','SL at 19990.1'),
                      LIMIT.replace('TP at 20020','TP at 19980')):
            self.assertIsNone(self.preview(self.event(quote))['payload'])

    def test_network_boundary_forbids_live_and_mutated_payloads(self):
        payload=self.preview()['payload']
        changes=[{'test':False},{'test':1},{'quantity':2},{'quantity':True},
                 {'action':'exit'},{'ticker':'ESZ2026'},{'limitPrice':19900},
                 {'stopLoss':{'type':'stop','stopPrice':20010}}, {'cancel':True}]
        with patch('transcript_entry_bridge.urllib.request.build_opener') as opener:
            for change in changes:
                with self.subTest(change=change), self.assertRaises((ValueError,TypeError)):
                    send_entry_test(URL,dict(payload,**change))
            opener.assert_not_called()
        with self.assertRaisesRegex(ValueError,'STALE_OR_FUTURE'):
            validate_test_payload(payload,now=1016)

    def test_transport_preserves_payload_and_redacts_response(self):
        payload=self.preview()['payload']
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=json.dumps(dict(success=True,id='accepted-id',logId='log-id',message=URL)).encode()
        with patch('transcript_entry_bridge.time.time',return_value=1000), patch('transcript_entry_bridge.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=response
            receipt=send_entry_test(URL,payload)
            request=opener.return_value.open.call_args.args[0]
            self.assertEqual(json.loads(request.data),payload)
            self.assertEqual(receipt,dict(success=True,id='accepted-id',logId='log-id'))
            self.assertNotIn('test_password_only',json.dumps(receipt))

    def test_concurrent_delivery_once_and_shadow_receipt(self):
        preview=self.preview();sent=[]
        def transport(url,payload):
            sent.append(deepcopy(payload));return dict(success=True,id='test-id')
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.bridge.deliver_test(self.session,preview['event_id'],transport,1000),range(4)))
        self.assertEqual(len(sent),1)
        self.assertTrue(all(r['status'] in ('sending','test_received') for r in results))
        self.assertEqual(self.bridge.deliver_test(self.session,preview['event_id'],transport,2000)['status'],'test_received')
        self.assertEqual(len(sent),1)
        saved=self.journal.shadow()[0]['entry_bridge'][0]['test_delivery']
        self.assertFalse(saved['broker_fill_verified']);self.assertFalse(saved['account_routing_verified'])
        self.assertNotIn('test_password_only',json.dumps(saved))
        duplicate=self.bridge.preview(self.session,'decision:'+preview['event_id'],self.event(),1000,'synthetic_fixture',1000)
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(len(self.journal.shadow()[0]['entry_bridge']),1)

    def test_unknown_result_is_never_retried_after_restart(self):
        preview=self.preview()
        def fail(*args):raise TransportError('NETWORK_OUTCOME_UNKNOWN')
        first=self.bridge.deliver_test(self.session,preview['event_id'],fail,1000)
        restarted=EntryBridge(self.journal,'MNQZ2026',URL)
        transport=MagicMock()
        again=restarted.deliver_test(self.session,preview['event_id'],transport,2000)
        self.assertEqual((first['status'],again['status']),('unknown','unknown'))
        transport.assert_not_called()

    def test_second_delivery_rate_limit_and_destination_conflict(self):
        p=self.preview();transport=lambda *a:dict(success=True,id='ok')
        self.bridge.deliver_test(self.session,p['event_id'],transport,1000)
        q=self.preview(self.event(MARKET),at=1001,now=1001)
        with self.assertRaisesRegex(ValueError,'RATE_LIMIT'):
            self.bridge.deliver_test(self.session,q['event_id'],transport,1001)
        changed=EntryBridge(self.journal,'MNQZ2026',URL+'changed')
        with self.assertRaisesRegex(ValueError,'DESTINATION_CONFLICT'):
            changed.deliver_test(self.session,p['event_id'],transport,1002)

    def test_finalized_live_journal_text_reaches_preview_and_shadow(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'live_transcription'))
        from local_gpt_test import LiveJournal
        capture=SimpleNamespace(epoch=time.monotonic()-5)
        live=LiveJournal(Path(self.temp.name)/'live',capture,'MNQZ2026')
        session,attempt,_=live.start(dict(id='fake-live-protocol-unit',audio_url='avfoundation:unit',started_at=time.time(),authorization='Offline protocol test'), 'macos_device')
        event=dict(type='conversation.item.input_audio_transcription.completed',item_id='unit-final',event_id='unit-provider-event',transcript=LIMIT)
        self.assertTrue(live.ingest(session,attempt,event,(0,5)))
        shadow=live.shadow()[0]
        self.assertEqual(shadow['entry_bridge'][0]['status'],'READY_TEST_PAYLOAD')
        self.assertIs(shadow['entry_bridge'][0]['payload']['test'],True)
        self.assertFalse(live.ingest(session,attempt,event,(0,5)))


if __name__ == '__main__':
    unittest.main()
