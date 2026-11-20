import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from copy import deepcopy
from services.tanja.ict_agent.common import REPO,digest,encode
from services.tanja.ict_agent.market import advance,fresh_state,bounds,validate_bar,load_csv,SPECS
from services.tanja.ict_agent.store import Store,transition
from services.tanja.ict_agent.observer import Observer
from services.tanja.ict_agent.risk import Policy,check
from services.tanja.ict_agent.simulator import BrokerSimulator
from services.tanja.ict_agent.knowledge import uncovered,LatentIndex
from services.tanja.ict_agent.ai import validate,SCHEMA,review_once
from services.tanja.ict_agent.bridge import read_tanja

class FakeKnowledge:
    version='fixture-corpus';index_version='fixture-index'
    def retrieve(self,*args,**kwargs):return dict(records=[],audit_notes=[],knowledge_version=self.version,index_version=self.index_version)

def bar(ts=1777642200,symbol='NQ',high=101,low=99,close=100):
    return dict(symbol=symbol,contract='fixture:'+symbol,ts=ts,received_at=ts+60,source='SYNTHETIC_UNIT_TEST_ONLY',open=100.,high=float(high),low=float(low),close=float(close))

def policy():return Policy(100,300,250,4,1,1,10,30,True)
def fixtures():
    p=dict(symbol='MNQ',contract='MNQM2026',direction='LONG',entry=20000.,stop=19990.,target=20020.,available_at=1000,expires_at=2000)
    a=dict(as_of=1000,reconciled=True,connected=True,protection_ok=True,daily_pnl_usd=0,open_risk_usd=0,reserved_risk_usd=0,open_contracts=0,reserved_contracts=0)
    m=dict(symbol='MNQ',contract='MNQM2026',spec=SPECS['MNQ'],dated_contract_verified=True,session_open=True,connected=True,quote_at=1000,bid=19999.75,ask=20000.,news_clear=True)
    return p,a,m

class Tests(unittest.TestCase):
    def test_cue_coverage_union(self):
        self.assertEqual(uncovered(10,[[0,2],[1,5],[7,9]]),[[5,7],[9,10]])
        with self.assertRaises(ValueError):uncovered(10,[[0,11]])

    def test_bar_contract_bad_data(self):
        for patch in ({'close':float('nan')},{'ts':1},{'received_at':0},{'high':98},{'symbol':'BTC'},{'close':100.1},{'ts':True}):
            with self.assertRaises(ValueError):validate_bar(dict(bar(),**patch))

    def test_aggregate_closed_and_missing(self):
        start=1777642200;s=fresh_state('NQ','fixture:NQ');out=[]
        for i in range(4):out+=advance(s,bar(start+i*60))[0]
        self.assertFalse(any(f['timeframe']==5 for f in out))
        fs,ev=advance(s,bar(start+240));self.assertTrue(any(f['timeframe']==5 for f in fs))
        s=fresh_state('NQ','fixture:NQ');out=[]
        for i in (0,1,3,4):out+=advance(s,bar(start+i*60))[0]
        self.assertFalse(any(f['timeframe']==5 for f in out))

    def test_no_lookahead_pivot(self):
        s=fresh_state('NQ','fixture:NQ');start=1777642200
        advance(s,bar(start,high=101));_,e=advance(s,bar(start+60,high=103));self.assertFalse(any(x['kind']=='PIVOT' for x in e))
        _,e=advance(s,bar(start+120,high=102));p=next(x for x in e if x['kind']=='PIVOT')
        self.assertEqual(p['available_at'],start+180)

    def test_fvg_then_inversion(self):
        s=fresh_state('NQ','fixture:NQ');start=1777642200
        seq=[dict(open=100,high=101,low=99,close=100),dict(open=100,high=105,low=100,close=104),dict(open=104,high=106,low=103,close=105),dict(open=105,high=106,low=99,close=100)]
        all_events=[]
        for i,prices in enumerate(seq):all_events+=advance(s,dict(bar(start+i*60),**prices))[1]
        f=next(e for e in all_events if e['kind']=='FVG');v=next(e for e in all_events if e['kind']=='IFVG')
        self.assertLess(f['available_at'],v['available_at']);self.assertEqual(v['direction'],'SHORT')

    def test_dst_anchor(self):
        ny=ZoneInfo('America/New_York')
        for month,day,utc_hour in [(1,5,23),(7,5,22)]:
            t=int(datetime(2026,month,day,18,tzinfo=ny).timestamp());a,b=bounds(t,240)
            self.assertEqual(a,t);self.assertEqual(datetime.fromtimestamp(a,ZoneInfo('UTC')).hour,utc_hour)

    def test_reuse_tanja_aggregate_parity(self):
        import sys
        sys.path.insert(0,str(REPO/'services/tanja/vendor'))
        from tanya_replay import Bar,aggregate
        from context_layer import session_aggregate
        raw,_=load_csv(Path(__import__('os').environ.get('ICT_TEST_DATA_DIR',REPO/'research_tanja_month_202605_20261014'))/'NQ.csv','NQ');raw=raw[:1000]
        s=fresh_state('NQ',raw[0]['contract']);formed=[]
        for b in raw:formed+=advance(s,b)[0]
        legacy=[Bar(b['ts'],b['open'],b['high'],b['low'],b['close']) for b in raw]
        for tf in (1,5,15,60,240):
            expect=aggregate(legacy,tf,raw[-1]['received_at']) if tf<60 else session_aggregate(legacy,tf,raw[-1]['received_at'])
            actual=[f for f in formed if f['timeframe']==tf]
            self.assertEqual([(b.time,b.open,b.high,b.low,b.close) for b in expect],[(b['ts'],b['open'],b['high'],b['low'],b['close']) for b in actual])

    def test_restart_duplicates_and_transaction(self):
        with tempfile.TemporaryDirectory() as d:
            st=Store(Path(d)/'s.db',{'test':1});ob=Observer(st,FakeKnowledge());b=bar()
            ob.push([b]);before=st.stats();self.assertTrue(Observer(st,FakeKnowledge()).push([b])['duplicate'])
            self.assertEqual(before,st.stats())
            with self.assertRaises(ValueError):ob.push([dict(b,close=100.25)])
            self.assertEqual(before,st.stats())
            with self.assertRaises(ValueError):ob.push([bar(b['ts']-60)])

    def test_replay_prefix_and_restart_equal(self):
        with tempfile.TemporaryDirectory() as d:
            a=Store(Path(d)/'a.db',{'test':1});b=Store(Path(d)/'b.db',{'test':1})
            oa=Observer(a,FakeKnowledge());ob=Observer(b,FakeKnowledge())
            for i in range(12):
                batch=[bar(1777642200+i*60,s) for s in ('NQ','ES','MNQ')]
                oa.push(batch)
                if i==6:ob=Observer(b,FakeKnowledge())
                ob.push(batch)
            self.assertEqual(a.snapshot(),b.snapshot())
            ident=a.snapshot()['id'];saved=a.snapshot(ident)
            oa.push([bar(1777642200+12*60)])
            self.assertEqual(saved,a.snapshot(ident))

    def test_contract_roll_reset(self):
        with tempfile.TemporaryDirectory() as d:
            st=Store(Path(d)/'a.db',{});o=Observer(st,FakeKnowledge());o.push([bar()])
            o.push([dict(bar(1777642260),contract='different-contract')])
            s=st.snapshot();self.assertEqual(s['context']['NQ']['contract'],'different-contract')
            self.assertIn('CONTRACT_CHANGE',s['event_kinds'])

    def test_state_machine(self):
        s={'id':'x','state':'DISCOVERED'}
        transition(s,'WAITING_FOR_CONFIRMATION',1,'waiting',[]);transition(s,'READY',2,'confirmed',[])
        transition(s,'ORDER_PENDING',3,'risk',[]);transition(s,'ENTERED',4,'fill',[]);transition(s,'MANAGING',5,'manage',[]);transition(s,'CLOSED',6,'exit',[])
        with self.assertRaises(ValueError):transition(s,'READY',7,'reopen',[])

    def test_risk_units_and_reservations(self):
        p,a,m=fixtures();r=check(p,policy(),a,m,1000)
        self.assertTrue(r['approved']);self.assertEqual(r['risk_per_contract_usd'],23);self.assertEqual(r['quantity'],4)
        self.assertFalse(check(p,policy(),dict(a,daily_pnl_usd=-290),m,1000)['approved'])
        self.assertFalse(check(p,policy(),dict(a,open_risk_usd=245),m,1000)['approved'])

    def test_risk_fail_closed(self):
        p,a,m=fixtures()
        for changed in ({'quote_at':900},{'contract':'NQM2026'},{'spec':SPECS['NQ']},{'dated_contract_verified':False},{'news_clear':None},{'session_open':False},{'bid':None},{'ask':float('nan')}):
            self.assertFalse(check(p,policy(),a,dict(m,**changed),1000)['approved'],changed)
        self.assertFalse(check(p,policy(),a,m,1000,kill=True)['approved'])
        self.assertFalse(check(p,policy(),a,m,1000,duplicate=True)['approved'])
        self.assertFalse(check(p,None,a,m,1000)['approved'])
        self.assertFalse(check(dict(p,stop=20001),policy(),a,m,1000)['approved'])

    def test_simulator_partial_fill_recovery(self):
        p,a,m=fixtures()
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'sim.db';sim=BrokerSimulator(path)
            self.assertEqual(sim.submit('opp',p,policy(),a,m,1000)['status'],'REJECTED')
            sim.reconcile(1000,{})
            o=sim.submit('opp',p,policy(),a,m,1000);self.assertEqual(o['quantity'],4)
            self.assertTrue(sim.submit('opp',p,policy(),a,m,1000)['duplicate'])
            sim.event('ack',o['id'],'ACK',1001)
            f=sim.event('f1',o['id'],'FILL',1002,quantity=1,price=20000.,protective_stop_ack=True)
            self.assertEqual(f['status'],'PARTIALLY_FILLED')
            sim=BrokerSimulator(path);self.assertTrue(sim.event('f1',o['id'],'FILL',1002,quantity=1,price=20000.,protective_stop_ack=True)['duplicate'])
            with self.assertRaises(ValueError):sim.event('f1',o['id'],'FILL',1002,quantity=2,price=20000.,protective_stop_ack=True)
            sim.event('disconnect',o['id'],'DISCONNECT',1003)
            self.assertFalse(sim.reconcile(1004,{})['reconciled'])
            self.assertTrue(sim.reconcile(1004,{'MNQM2026':1})['reconciled'])
            sim.event('cancel',o['id'],'CANCEL',1005)
            out=sim.event('exit',o['id'],'EXIT',1006,quantity=1,price=20010.)
            self.assertEqual(out['status'],'CLOSED');self.assertEqual(out['realized_usd'],17)

    def test_unprotected_fill_kills(self):
        p,a,m=fixtures()
        with tempfile.TemporaryDirectory() as d:
            sim=BrokerSimulator(Path(d)/'sim.db');sim.reconcile(1000,{})
            o=sim.submit('a',p,policy(),a,m,1000)
            sim.event('fill',o['id'],'FILL',1001,quantity=1,price=20000.)
            self.assertEqual(sim.submit('b',p,policy(),a,m,1001)['status'],'REJECTED')
            self.assertFalse(sim.reconcile(1001,{'MNQM2026':1})['reconciled'])

    def test_ai_schema_unknown_sources_and_escalation(self):
        p={'snapshot':{'id':'snapshot','as_of':1000,'setups':[],'decision':{'candidate_ids':[]}},'source_records':[{'id':'rule'}],'evidence':{'e':{'available_at':1000}}}
        answer=dict(snapshot_id='snapshot',as_of=1000,decision='NO_TRADE',preferred_hypothesis_id=None,
            claims=[dict(text='uncertain',evidence_ids=['e'],source_rule_ids=['rule'],status='INFERENCE',missing=[])],
            rejected_hypotheses=[],next_observable_event='new bar',missing_conditions=['source'],explanation='Wait')
        self.assertTrue(validate(answer,p,1010)['valid'])
        for changed in ({'snapshot_id':'fake'},{'decision':'READY_FOR_RISK_CHECK'},{'decision':'TRADE_CANDIDATE'},{'preferred_hypothesis_id':'fake'}):
            with self.assertRaises(ValueError):validate(dict(answer,**changed),p,1010)
        b=deepcopy(answer);b['claims'][0]['source_rule_ids']=['fake']
        with self.assertRaises(ValueError):validate(b,p,1010)
        with self.assertRaises(ValueError):validate(answer,p,1201)

    def test_local_lsa_retrieves_without_network(self):
        idx=LatentIndex(['liquidity sweep reversal stop','displacement fair value gap reversal','yield bond inflation interest','order stop liquidity'])
        sem,lex=idx.scores('liquidity stop');self.assertEqual(int(lex.argmax()),3)
        self.assertEqual(len(sem),4)

    def test_scenario_sequence_dedupe_and_target_expiry(self):
        from services.tanja.ict_agent.scenarios import update_setups
        ctx={'NQ':dict(contract='x',latest={'id':'bar','ts':1000,'close':100,'high':101,'low':99},ranges={},active_levels=[dict(id='target',price=90,side='LOW',available_at=900)],stale=False)}
        root=dict(id='sweep',symbol='NQ',contract='x',available_at=1000,timeframe=5,kind='SWEEP',direction='SHORT',price=102,extreme=103)
        setups={};tr,ret=update_setups(setups,[root,dict(root,id='same-bar-h1',timeframe=60)],ctx,1000,FakeKnowledge())
        self.assertEqual(len(setups),1);s=next(iter(setups.values()));ident=s['id']
        self.assertEqual(len(s['correlated_root_events']),2)
        es=[dict(id=k,symbol='NQ',timeframe=1,direction='SHORT',available_at=1060,kind=k) for k in ('DISPLACEMENT','STRUCTURE_BREAK','FVG')]
        es[-1].update(lower=100,upper=101)
        ctx['NQ']['latest'].update(ts=1000,close=99,high=100,low=98)
        update_setups(setups,es,ctx,1060,FakeKnowledge());self.assertEqual(s['state'],'READY');self.assertEqual(s['plan']['entry'],100)
        ctx['NQ']['latest'].update(ts=1060,close=91,high=99,low=90)
        update_setups(setups,[],ctx,1120,FakeKnowledge());self.assertEqual(s['state'],'EXPIRED');self.assertEqual(s['id'],ident)

    def test_htf_level_not_evicted_by_m1(self):
        s=fresh_state('NQ','fixture:NQ');s['levels']=[dict(id='daily',kind='PRIOR_SESSION_LEVEL',side='HIGH',price=200,timeframe=1440,available_at=0,consumed=False)]
        for i in range(90):advance(s,bar(1777642200+i*60,high=101+i%3))
        self.assertIn('daily',[l['id'] for l in s['levels']])

    def test_smt_requires_shared_windows(self):
        from services.tanja.ict_agent.market import intermarket
        a=dict(start=100,end=200,complete=True,high=110,low=90,high_id='h',low_id='l')
        p=dict(start=0,end=50,complete=True,high=105,low=85,high_id='ph',low_id='pl')
        states={'NQ':{'rth':a,'previous_rth':p},'ES':{'rth':dict(a,high=100),'previous_rth':p}}
        self.assertTrue(intermarket(states,200)['sides']['high']['divergence'])
        states['ES']['rth']['end']=190
        self.assertEqual(intermarket(states,200)['status'],'UNSYNCHRONIZED_WINDOWS')

    def test_tanja_bridge_preserves_receipts_and_source(self):
        import sqlite3,hashlib
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'tanja.db'
            with sqlite3.connect(path) as db:
                for table in ('bars','collection_bars'):db.execute(f'CREATE TABLE {table}(symbol TEXT,start INTEGER,end INTEGER,received REAL,payload TEXT)')
                for symbol in ('NQ','ES','MNQ'):
                    b=bar(symbol=symbol);p=dict(symbol=symbol,ticker=symbol+'1!',confirmed=True,standard=True,bar_open_ms=b['ts']*1000,bar_close_ms=(b['ts']+60)*1000,**{k:b[k] for k in ('open','high','low','close')})
                    table='collection_bars' if symbol=='NQ' else 'bars'
                    db.execute(f'INSERT INTO {table} VALUES(?,?,?,?,?)',(symbol,b['ts'],b['ts']+60,b['ts']+62.5,encode(p)))
            before=hashlib.sha256(path.read_bytes()).hexdigest()
            batches,gaps=read_tanja(path,frozen_at=1777642300)
            self.assertEqual(len(batches),1);self.assertEqual(batches[0][0]['received_at'],1777642263)
            self.assertEqual(before,hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(read_tanja(path,frozen_at=1777642260)[0],[])

    def test_readonly_dashboard_chart_cutoff(self):
        from services.tanja.ict_agent.web import create_app
        with tempfile.TemporaryDirectory() as d:
            st=Store(Path(d)/'a.db',{});o=Observer(st,FakeKnowledge());o.push([bar()]);old=st.snapshot()
            o.push([bar(1777642260)])
            client=create_app(st.path).test_client();response=client.get('/api/snapshots/'+old['id']+'/charts')
            self.assertEqual(response.status_code,200)
            self.assertTrue(all(b['available_at']<=old['as_of'] for b in response.json['1']))
            self.assertEqual(client.post('/api/state',json={'trade':True}).status_code,405)
            self.assertEqual(client.get('/api/snapshots/'+old['id']+'/charts?symbol=BTC').status_code,400)

if __name__=='__main__':unittest.main()
