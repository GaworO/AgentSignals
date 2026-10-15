import io
import json
import tempfile
import unittest
from copy import deepcopy
from store import Store
from broker_feedback import BrokerFeedback
from live_quotes import LiveQuotes
from test_broker_feedback import CONFIG,event,NOW

QUOTE_CONFIG=dict(CONFIG,TANJA_QUOTE_FEED_TOKEN='q'*40,TANJA_QUOTE_SOURCE='independent-fixture',
    TANJA_QUOTE_CONTRACTS=json.dumps({s:s+'Z2026' for s in ('ES','NQ','MNQ')}))


def message(seq=1,kind='connected',symbol='MNQ',now=NOW,generation='connection-1'):
    return dict(source='independent-fixture',generation=generation,sequence=seq,kind=kind,sent_at=now,
        data={} if kind!='quote' else dict(symbol=symbol,contract=symbol+'Z2026',bid=100,ask=100.25,last=100,
            bid_at=now,ask_at=now,last_at=now))


class QuoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.feed=LiveQuotes(self.store,QUOTE_CONFIG)

    def fresh(self):
        self.feed.ingest(message(),NOW)
        for seq,symbol in enumerate(('ES','NQ','MNQ'),2):self.feed.ingest(message(seq,'quote',symbol),NOW)

    def test_independent_quotes_persist_and_expire(self):
        self.fresh();self.assertTrue(self.feed.state(NOW)['quotes_current'])
        self.assertTrue(LiveQuotes(self.store,QUOTE_CONFIG).state(NOW+1)['quotes_current'])
        self.assertFalse(self.feed.state(NOW+2.01)['quotes_current'])
        self.assertEqual(self.feed.ingest(message(4,'quote'),NOW),{'status':'duplicate'})

    def test_gap_invalidates_every_symbol_until_new_generation(self):
        self.fresh()
        with self.assertRaisesRegex(ValueError,'SEQUENCE_GAP'):self.feed.ingest(message(6,'quote'),NOW)
        self.assertFalse(self.feed.state(NOW)['quotes_current'])
        self.assertTrue(all(q is None for q in self.feed.state(NOW)['quotes'].values()))
        with self.assertRaisesRegex(ValueError,'RECONNECT'):self.feed.ingest(message(5,'quote'),NOW)
        self.feed.ingest(message(generation='connection-2'),NOW)
        with self.assertRaisesRegex(ValueError,'GENERATION_MISMATCH'):self.feed.ingest(message(5,'quote'),NOW)
        self.assertFalse(self.feed.state(NOW)['quotes_current'])

    def test_each_component_requires_its_own_real_fresh_clock(self):
        self.feed.ingest(message(),NOW)
        for field in ('bid_at','ask_at','last_at'):
            for at in (NOW-2.01,NOW+0.01):
                msg=message(2,'quote');msg['data'][field]=at
                with self.assertRaisesRegex(ValueError,'COMPONENT_STALE'):self.feed.ingest(msg,NOW)
        msg=message(2,'quote');del msg['data']['last_at']
        with self.assertRaisesRegex(ValueError,'CLOCKS_REQUIRED'):self.feed.ingest(msg,NOW)

    def test_price_contract_source_and_clock_regressions_rejected(self):
        self.feed.ingest(message(),NOW)
        for key,value in [('contract','NQZ2026'),('bid',100.1),('ask',99.75),('last',float('nan')),('symbol','MES')]:
            msg=message(2,'quote');msg['data'][key]=value
            with self.assertRaises(ValueError):self.feed.ingest(msg,NOW)
        msg=message(2,'quote');msg['data']['last_at']=NOW-.5;self.feed.ingest(msg,NOW)
        msg=message(3,'quote');msg['data']['last_at']=NOW-1
        with self.assertRaisesRegex(ValueError,'REGRESSION'):self.feed.ingest(msg,NOW)

    def test_disconnect_or_reconnect_clears_cached_prices(self):
        self.fresh();self.feed.ingest(message(5,'disconnected'),NOW)
        self.assertFalse(self.feed.state(NOW)['quotes_current'])
        self.feed.ingest(message(generation='connection-2'),NOW)
        self.assertTrue(all(q is None for q in self.feed.state(NOW)['quotes'].values()))
        with self.assertRaisesRegex(ValueError,'NEW_QUOTE_GENERATION'):self.feed.ingest(message(),NOW)

    def test_selected_stale_source_cannot_fall_back_to_broker_quotes(self):
        feedback=BrokerFeedback(self.store,QUOTE_CONFIG)
        for seq,symbol in enumerate(('ES','NQ','MNQ'),1):
            msg=event(seq,'quote');msg['data'].update(symbol=symbol,contract=symbol+'Z2026');feedback.ingest(msg,NOW)
        self.assertFalse(feedback.state(NOW)['quotes_current'])
        self.fresh();self.assertTrue(feedback.state(NOW)['quotes_current'])

    def test_http_quotes_cannot_submit_account_data_or_use_other_secrets(self):
        from app import App
        import time
        app=App(dict(QUOTE_CONFIG,DATA_DIR=self.tmp.name,TANJA_FEED_TOKEN='f'*40,TANJA_DASHBOARD_PASSWORD='p'*20))
        def request(token,body):
            raw=json.dumps(body).encode();result={}
            app(dict(PATH_INFO='/api/live-quotes',REQUEST_METHOD='POST',CONTENT_LENGTH=str(len(raw)),
                HTTP_AUTHORIZATION='Bearer '+token,**{'wsgi.input':io.BytesIO(raw)}),lambda status,headers:result.update(status=status))
            return result['status']
        msg=message(now=time.time())
        self.assertTrue(request(CONFIG['TANJA_BROKER_FEEDBACK_TOKEN'],msg).startswith('401'))
        self.assertTrue(request(QUOTE_CONFIG['TANJA_QUOTE_FEED_TOKEN'],msg).startswith('200'))
        self.assertTrue(request(QUOTE_CONFIG['TANJA_QUOTE_FEED_TOKEN'],event()).startswith('422'))
        with self.assertRaisesRegex(ValueError,'separate secret'):
            App(dict(QUOTE_CONFIG,DATA_DIR=self.tmp.name,TANJA_FEED_TOKEN='q'*40,TANJA_DASHBOARD_PASSWORD='p'*20))


if __name__=='__main__':unittest.main()
