import ast
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import ab_ai_context as ai
import ab_native_entry as ab
import execution_policy as policy
import test_builder_crosstrade as fixture_module
from test_builder_crosstrade import functions, ROOT


def ab_env():
    return dict(EXEC_STRATEGY_POLICY='AB_ONLY',AB_ENTRY_ARMED='1',BUILDER_ORDER_PROVIDER='crosstrade',
                AB_V3_MODE='SHADOW',AB_V3_CONTRACT='MNQZ2026',EXEC_TICKER='MNQZ2026',
                AB_V3_DETECTOR_CONTRACT_VERIFIED='1',ACCOUNT_PLAN='builder50',ACCOUNT='50000',
                RISK_PCT='.5',EXEC_MAX_QTY='15',EXEC_CANCEL_AFTER_SEC='600',PRICE_OFFSET='0',ENTRY_OFFSET_PTS='0',
                EXEC_FX='0',EXEC_TICK='.25',POINT_VALUE='2',GUARD_TOKEN='fixture-admin-token-32-characters-long')


class RoutingTests(unittest.TestCase):
    def test_only_original_ab_can_enter(self):
        with patch.dict(os.environ,ab_env()):
            base=dict(_strat='A/B',dir='LONG',entry=100.,SL=90.,TP=125.)
            self.assertIsNone(policy.entry_blocker(base))
            for strategy in ('A/B Directional LONG','AB_DIRECTIONAL','TANJA','A/B-shallow','DOL_DELIVERY_REVERSAL','Continuation LONG'):
                self.assertEqual(policy.entry_blocker(dict(base,_strat=strategy)), 'shadow_strategy:AB_ONLY')
            self.assertIsNotNone(policy.entry_blocker(dict(base,model='A/B Directional')))
            self.assertIsNotNone(policy.entry_blocker(dict(base,_v3_directional=True)))
            self.assertEqual(policy.status()['entry_variant'],'CANONICAL_AB_NATIVE_BRACKET')
            with patch.dict(os.environ, ENTRY_OFFSET_PTS='1'):
                self.assertIsNone(policy.entry_blocker(base))
            for key,value in [('AB_ENTRY_ARMED','0'),('AB_V3_MODE','LIVE'),('BUILDER_ORDER_PROVIDER','traderspost'),('EXEC_FX','1'),('PRICE_OFFSET','1')]:
                with patch.dict(os.environ,{key:value}):self.assertIsNotNone(policy.entry_blocker(base))

    def test_single_native_bracket_has_no_shallow_or_partial(self):
        signal=dict(_strat='A/B',entry=100.,SL=90.,TP=130.)
        with patch.dict(os.environ,ab_env()):
            rows=ab.configure_signal(signal)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['TP'],130.)
        self.assertTrue(rows[0]['_disable_partial']);self.assertEqual(rows[0]['_risk_budget_usd'],250.)

    def test_directional_frozen_manager_ignores_ab(self):
        # Exercise actual monitor function with an A/B row; it must not read
        # manager features or create a manager action even if flags drift.
        class DB:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def execute(self,sql,*args):
                if not sql.startswith('SELECT o.*'):raise AssertionError('A/B reached Directional manager')
                return self
            def fetchall(self):return [dict(order_json=json.dumps(dict(strategy='A/B')))]
        ns=dict(mode=lambda:'SHADOW',exit_mode=lambda:'shadow',now_ms=lambda:100000,
                connect=lambda:DB(),json=json)
        functions(ROOT/'ab_v3_live.py',{'monitor_frozen'},ns)
        ns['monitor_frozen']()

    def test_native_ab_waits_for_owned_fill_and_deduplicates(self):
        # Reuse the existing fully mocked broker fixture without importing the live app.
        fixture=fixture_module.NativeTests();fixture.setUp()
        try:
            with patch.dict(os.environ,ab_env()):
                s=dict(_strat='A/B',dir='LONG',entry=30000.,SL=29990.,TP=30030.,bos_ms=int(fixture.now*1000)-60000)
                ab.configure_signal(s)
                payload=fixture.payload()
                oid=ab.prepare(s,payload,'ab-unique-setup',fixture.api)
                result=fixture.t.submit(payload,oid);ab.record_result(fixture.api,oid,result)
                with fixture.api.connect() as db:r=db.execute('SELECT * FROM orders WHERE order_id=?',(oid,)).fetchone()
                self.assertEqual(r['state'],'WAITING_BROKER_FILL');self.assertIsNone(r['position_json'])
                self.assertEqual(json.loads(r['order_json'])['strategy'],'A/B')
                with self.assertRaises(ValueError):ab.prepare(s,payload,'ab-unique-setup',fixture.api)
                self.assertEqual(len(fixture.posts),1)
                with patch.dict(os.environ,AB_ENTRY_ARMED='0'):
                    with self.assertRaises(ValueError):fixture.t.submit(payload,oid)
                self.assertEqual(len(fixture.posts),1)
        finally:fixture.doCleanups()

    def test_actual_executor_sends_ab_rounded_bracket_and_never_legacy_webhook(self):
        import datetime as dt
        import types
        from unittest.mock import Mock
        import builder_crosstrade as native
        fixture=fixture_module.NativeTests();fixture.setUp()
        try:
            fixture.api.mode=lambda:'SHADOW'
            requests=Mock();requests.post.side_effect=AssertionError('legacy webhook used')
            namespace=dict(os=os,dt=dt,builder_crosstrade=native,ab_v3_live=fixture.api,
                ab_native_entry=ab,requests=requests,execution_policy=policy,
                live_emit=types.SimpleNamespace(key=lambda s:'canonical-setup',size_for_budget=lambda *a:(2,10,20,40)),
                guardrails=types.SimpleNamespace(_exec_route_id=lambda:'test-route'),
                _entry_cancel_after_sec=lambda:600,_signal_reject_after_sec=lambda:60)
            functions(ROOT/'agent.py',{'_exec_order'},namespace)
            with patch.dict(os.environ,dict(ab_env(),EXEC_WEBHOOK='',SESSION_SIZE_MULT='',ENTRY_OFFSET_PTS='1')):
                signal=dict(_strat='A/B',dir='LONG',entry=30000.03,SL=29990.07,TP=30030.13,
                            bos_ms=int(fixture.now*1000)-60000,_exec_qty_override=2)
                ab.configure_signal(signal)
                result=namespace['_exec_order'](signal)
                self.assertTrue(result['sent'],result)
                self.assertEqual(fixture.posts[-1][1]['takeProfit'],30030.25)
                self.assertEqual(fixture.posts[-1][1]['limitPrice'],30000.)
                self.assertEqual(signal['TP'],30030.13)
                self.assertIn('_ab_native_order_id',signal)
                requests.post.assert_not_called()
                fixture.prepare()
                with self.assertRaises(native.TransportError):fixture.t.submit(fixture.payload(),'order-one')
                self.assertEqual(len(fixture.posts),1)
        finally:fixture.doCleanups()


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        # Monday Oct 19 2026 10:00 NY.
        self.now=1792418400.;self.calls=[]
        self.config=dict(DATA_DIR=self.tmp.name,AB_AI_ENABLED='true',AB_AI_INTERVAL_MINUTES='60',
                         AB_AI_MAX_CALLS_PER_DAY='12',OPENAI_MODEL='fixture',OPENAI_API_KEY='fixture')
        self.used=0
        self.support=ai.ContextSupport(self.config,reader=self.source,transport=self.transport,clock=lambda:self.now)

    def source(self):
        cutoff=int(self.now)//60*60;ev={}
        for symbol in ('ES','NQ','MNQ'):
            for tf,n in ((1,64),(60,4),(240,4)):
                end=cutoff//(tf*60)*(tf*60)
                for i in range(n):
                    available=end-i*tf*60
                    ev[f'{symbol}:{tf}:{available}']=dict(kind='bar',symbol=symbol,timeframe=tf,
                        time=available-tf*60,available_at=available,open=100.,high=110.,low=90.,close=100.)
        p=dict(as_of=cutoff,evidence=ev,input_mode='market_only',labels_included=False)
        p['packet_id']=ai.digest(p)
        return dict(context=dict(packet=p,frozen_at=self.now,processed_at=self.now),
                    ai_status='DISABLED',calls_today=self.used,calendar=dict(status='READY'))

    def transport(self,payload):
        self.calls.append(payload);p=json.loads(payload['input'][1]['content'])
        ref=next(k for k,e in p['evidence'].items() if e['symbol']=='NQ' and e['timeframe']==1)
        s=dict(permission='ALLOW',thesis='reversal',reason_pl='Potwierdzony kontekst',evidence_ids=[ref],
               contrary_evidence_pl='Trend przeciwny',invalidation=dict(evidence_id=ref,field='low',price=90.,relation='below'))
        d=dict(packet_id=p['packet_id'],long_scenario=s,short_scenario=s,liquidity_reading_pl='Hipoteza OHLC',
               target_2r_3r_pl='Potrzebne entry i SL',missing_context_pl=['Brak pełnego D1'])
        return dict(status='completed',usage=dict(input_tokens=10,output_tokens=10),
                    output=[dict(type='message',content=[dict(type='output_text',text=json.dumps(d))])])

    def test_disabled_service_does_not_read_or_spend(self):
        self.support.enabled=False
        self.support.reader=lambda:(_ for _ in ()).throw(AssertionError('source read'))
        self.assertFalse(self.support.process_one());self.assertEqual(self.calls,[])

    def test_migrated_budget_counts_tanja_calls_and_persists_after_restart(self):
        self.used=11
        self.assertTrue(self.support.process_one())
        self.now+=3600
        restarted=ai.ContextSupport(self.config,reader=self.source,transport=self.transport,clock=lambda:self.now)
        self.assertFalse(restarted.process_one());self.assertEqual(len(self.calls),1)
        self.assertEqual(restarted.status,'DAILY_LIMIT_REACHED')

    def test_hourly_cache_and_annotation_never_changes_signal(self):
        signal=dict(dir='SHORT',entry=100,SL=110,TP=80);before=copy.deepcopy(signal)
        self.assertTrue(self.support.process_one())
        result=self.support.annotate(signal,'candidate1')
        self.assertEqual(result['state'],'ALLOW');self.assertFalse(result['executable']);self.assertEqual(signal,before)
        self.now+=60;self.assertFalse(self.support.process_one());self.assertEqual(len(self.calls),1)
        self.now+=3600;self.assertEqual(self.support.annotate(signal,'candidate2')['state'],'UNAVAILABLE')

    def test_unknown_paid_call_is_not_retried_after_restart(self):
        def timeout(payload):self.calls.append(payload);raise TimeoutError()
        self.support.transport=timeout;self.assertTrue(self.support.process_one());self.now+=3600
        restarted=ai.ContextSupport(self.config,reader=self.source,transport=self.transport,clock=lambda:self.now)
        self.assertFalse(restarted.process_one());self.assertEqual(len(self.calls),1)

    def test_invalid_evidence_is_rejected_and_consumes_budget(self):
        def bad(payload):
            r=self.transport(payload);d=json.loads(r['output'][0]['content'][0]['text'])
            d['long_scenario']['evidence_ids']=['invented'];r['output'][0]['content'][0]['text']=json.dumps(d);return r
        self.support.transport=bad;self.assertTrue(self.support.process_one())
        self.assertEqual(self.support.status,'rejected')
        self.assertEqual(self.support.annotate(dict(dir='LONG'),'candidate')['state'],'UNAVAILABLE')
        with self.support.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM reviews').fetchone()[0],1)

    def test_invalidation_latches_even_if_price_recovers(self):
        self.support.process_one();self.now+=60
        p=self.support.packet(self.source(),self.now)
        bar=next(e for e in p['evidence'].values() if e['symbol']=='NQ' and e['timeframe']==1 and e['available_at']==p['as_of'])
        bar['close']=89.;self.support.invalidate(p)
        self.assertEqual(self.support.annotate(dict(dir='LONG'),'one')['state'],'INVALIDATED')
        bar['close']=100.;self.support.invalidate(p)
        self.assertEqual(self.support.annotate(dict(dir='LONG'),'two')['state'],'INVALIDATED')

    def test_stale_future_or_active_tanja_blocks_before_api(self):
        for change in ('stale','future','enabled'):
            source=self.source()
            if change=='enabled':source['ai_status']='READY'
            else:source['context']['packet']['as_of']+=300 if change=='future' else -300
            with self.assertRaises(ValueError):self.support.packet(source,self.now)
        self.assertEqual(self.calls,[])

    def test_concurrent_workers_share_durable_reservation(self):
        barrier=threading.Barrier(2);answers=[]
        def read():barrier.wait();return self.source()
        workers=[ai.ContextSupport(self.config,reader=read,transport=self.transport,clock=lambda:self.now) for _ in range(2)]
        threads=[threading.Thread(target=lambda w=w:answers.append(w.process_one())) for w in workers]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(sorted(answers),[False,True]);self.assertEqual(len(self.calls),1)


if __name__=='__main__':unittest.main()
