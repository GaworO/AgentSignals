import copy
import json
from pathlib import Path

import pytest
import directional_market_data as market
import directional_thesis as thesis
import directional_account_limits as allowance
import execution_validation as gate
from test_execution_readiness import evidence


def quote():
    return dict(instrument='MNQ 12-26',type='Future',tickSize=.25,pointValue=2,
                dataSource=dict(status='Live'),epoch=1000,bid=99.75,ask=100,isMarketOpen=True)


def test_verified_snapshot_not_candle_or_collective():
    args=evidence();raw=quote()
    assert market.normalize(raw,args[0],args[5],args[-1])['ask']==100
    for change in ({'source':'collective','pricingBasis':'cluster_mean_mid'},
                   {'dataSource':{'status':'Delayed'}},{'instrument':'NQ 12-26'},
                   {'epoch':None},{'bid':100.1},{'ask':99},{'epoch':1001},{'epoch':998}):
        with pytest.raises(ValueError):market.normalize(dict(raw,**change),args[0],args[5],args[-1])


def test_net_2r_is_not_gross_2r_and_limit_is_unchanged():
    args=evidence();original=copy.deepcopy(args[0])
    args[5]['costs']['round_trip_usd']=1.9
    assert gate.evaluate(*args)=='net_reward_risk_below_minimum'
    assert args[0]==original
    args[0]['target_price']=130.;args[4]['setup']=copy.deepcopy(args[0])
    assert gate.evaluate(*args) is None
    args[5]['costs']['stop_slippage_ticks']=40
    assert gate.evaluate(*args)=='net_reward_risk_below_minimum'


def test_unknown_costs_and_negative_costs_never_pass():
    args=evidence()
    for value in ({},dict(args[5]['costs'],round_trip_usd=-1),dict(args[5]['costs'],round_trip_usd=float('nan'))):
        args[5]['costs']=value
        assert gate.evaluate(*args) is not None


def structural_fixture():
    import ab_directional_entry_rules as rules
    bos=dict(open=100.,high=104.,low=96.,close=104.)
    rule=rules.evaluate('PDH','LONG',bos,600000,660000)
    order=dict(order_id='one',strategy='AB_DIRECTIONAL',direction='LONG',activation_ms=660000,
               expiry_ms=900000,entry_price=100.,stop_price=90.,target_price=120.)
    source=dict(order,state='PENDING',payload_json=json.dumps(dict(entry_rules=rule,setup_category='PDH')),
                candidate_payload_json=json.dumps(dict(brk=2)))
    rows=[dict(ts_ms=600000,bar_json=json.dumps(bos)),dict(ts_ms=660000,
           bar_json=json.dumps(dict(open=102.,high=105.,low=101.,close=104.)))]
    return order,source,rows,730000


def test_original_setup_rechecked_with_complete_closed_references():
    args=structural_fixture()
    assert thesis.structural_check(*args) is None
    for state in ('FILLED','UNFILLED_EXPIRED','CANCELLED'):
        source=dict(args[1],state=state)
        assert thesis.structural_check(args[0],source,*args[2:])=='original_setup_consumed_or_expired'
    assert thesis.structural_check(args[0],args[1],args[2][:1],args[3])=='post_setup_reference_gap'
    args[2][1]['bar_json']=json.dumps(dict(low=89,high=105))
    assert thesis.structural_check(*args)=='original_bracket_touched_before_entry'


def test_bos_and_geometry_cannot_drift():
    args=structural_fixture();args[1]['entry_price']=100.25
    assert thesis.structural_check(*args)=='original_setup_changed'
    args=structural_fixture();args[2][0]['bar_json']='{}'
    assert thesis.structural_check(*args)=='original_bos_reference_missing_or_changed'


def allowance_fixture(tmp_path,monkeypatch):
    now=1000000
    proof=dict(provider='MFFU',account_binding='account',source_reference='TEST_ONLY',verified=True,
               observed_at_ms=now-1000,expires_at_ms=now+60000,minimum_balance=48000,
               daily_loss_limit_usd=1000,max_micro_contracts=40)
    path=tmp_path/'risk.json';path.write_text(json.dumps(proof))
    monkeypatch.setenv('MFFU_RISK_EVIDENCE_FILE',str(path));monkeypatch.setenv('DAY_LOSS_USD','550')
    snapshot=dict(read_started_at=999.9,account=dict(balance=dict(netLiq=48721.05,totalCashValue=48721.05,
             cashSOD=48961.55,realizedPnL=-240.5,openPnL=0,fundsInTransit=0,fundsInTransitAppliedToCash=0)))
    order=dict(quantity=2,entry_price=100,stop_price=90)
    return [snapshot,'account',order,now,1.9],proof,path


def test_actual_account_allowance_requires_bound_current_evidence(tmp_path,monkeypatch):
    args,proof,path=allowance_fixture(tmp_path,monkeypatch)
    assert allowance.check(*args) is None
    for changes in (dict(account_binding='other'),dict(expires_at_ms=args[3]),dict(verified=False)):
        path.write_text(json.dumps(dict(proof,**changes)))
        assert allowance.check(*args)=='mffu_allowances_expired_or_unbound'
    path.unlink();assert allowance.check(*args)=='mffu_allowances_unavailable'


def test_realized_unrealized_missing_or_inconsistent_pnl_blocks(tmp_path,monkeypatch):
    args,proof,path=allowance_fixture(tmp_path,monkeypatch)
    args[0]['account']['balance'].pop('openPnL')
    assert allowance.check(*args)=='account_wide_pnl_unknown'
    args[0]['account']['balance']['openPnL']=-20
    assert allowance.check(*args)=='account_wide_pnl_unreconciled'


def test_tighter_daily_and_trailing_floor_include_costs(tmp_path,monkeypatch):
    args,proof,path=allowance_fixture(tmp_path,monkeypatch)
    args[2]['quantity']=15
    assert allowance.check(*args)=='account_projected_daily_loss_breach'
    args[2]['quantity']=10
    path.write_text(json.dumps(dict(proof,minimum_balance=48510)))
    assert allowance.check(*args)=='mffu_projected_drawdown_breach'


def test_final_sources_fetch_quote_after_slow_risk_and_context(monkeypatch,tmp_path):
    args=evidence();called=[]
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    monkeypatch.setattr(gate,'policy',lambda:args[5])
    monkeypatch.setattr(gate,'_SOURCES',{
        name:(lambda order,n=name,i=i:called.append(n) or args[i])
        for name,i in [('market',1),('thesis',2),('risk',3)]})
    assert gate.check(args[0],args[4],clock=lambda:args[-1]/1000) is None
    assert called==['risk','thesis','market']


def test_fresh_thesis_rechecks_ai_invalidation_without_paid_call(monkeypatch,tmp_path):
    import ab_ai_context as ai
    import ab_v3_live
    import continuation_shadow as shadow
    import test_ab_routing as af
    from test_directional_setup_review import order as ai_order
    f=af.ContextTests();f.setUp()
    try:
        f.config.update(AB_AI_OWNER='AB_DIRECTIONAL',AB_AI_TRIGGER='SETUP',AB_V3_CONTRACT='MNQZ2026')
        service=ai.ContextSupport(f.config,reader=f.source,transport=f.transport,clock=lambda:f.now)
        monkeypatch.setattr(ai,'INSTANCE',service)
        monkeypatch.setattr(ai,'SETUP_COMPLETE_CALLBACK',None,raising=False)
        monkeypatch.setattr(gate,'policy',lambda:dict(thesis_rule_version=thesis.VERSION))
        monkeypatch.setattr(shadow,'DB_PATH',tmp_path/'shadow.sqlite3');shadow._init_db()
        monkeypatch.setenv('AB_V3_DB',str(tmp_path/'v3.sqlite3'))
        o=ai_order(f);o['contract']='MNQZ2026'
        with shadow._connect() as db:
            db.execute("INSERT INTO continuation_candidates(candidate_id,strategy,direction,event_kind,decision_ms,trading_day,stage,status,eligible,forward_eligible,payload_json,first_seen_at,updated_at) VALUES(?,?,'LONG','CANONICAL_OUTPUT',?,'2026-10-19','ORDER','PENDING',1,1,'{\"brk\":2}','x','x')",(o['candidate_id'],o['strategy'],o['activation_ms']))
            db.execute("INSERT INTO continuation_orders(order_id,strategy,direction,candidate_id,instrument_id,activation_ms,expiry_ms,entry_price,stop_price,target_price,risk_points,dol_id,state,payload_json,created_at,updated_at) VALUES(?,?, 'LONG',?,1,?,?,100,90,120,10,'DOL','PENDING',?,'x','x')",(o['order_id'],o['strategy'],o['candidate_id'],o['activation_ms'],o['expiry_ms'],o['payload_json']))
        bos=json.loads(o['payload_json'])['entry_rules']
        with ab_v3_live.connect() as db:
            db.execute('INSERT INTO bars VALUES(?,?,?,?,?)',('M1',o['contract'],bos['bos_ms'],json.dumps(bos['bos_ohlc']),int(f.now*1000)))
        service.queue_setup(o);assert service.process_one();assert len(f.calls)==1
        validator=thesis.ThesisValidator(clock=lambda:f.now)
        assert validator(o)['valid']
        f.now+=61
        with ab_v3_live.connect() as db:
            db.execute('INSERT INTO bars VALUES(?,?,?,?,?)',('M1',o['contract'],o['activation_ms'],json.dumps(dict(open=100,high=104,low=99,close=102)),int(f.now*1000)))
        original_reader=service.reader
        def changed_context():
            source=original_reader();packet=source['context']['packet']
            for e in packet['evidence'].values():
                if e['symbol']=='NQ' and e['timeframe']==1 and e['available_at']==packet['as_of']:
                    e.update(open=89.,high=90.,low=88.,close=89.)
            packet['packet_id']=ai.digest({k:v for k,v in packet.items() if k!='packet_id'})
            return source
        service.reader=changed_context
        proof=validator(o)
        assert not proof['valid'] and proof['reason']=='review_invalidated_after_response'
        assert len(f.calls)==1
    finally:f.doCleanups()


def test_setup_ai_to_native_ack_fill_bracket_restart_close_accounting(monkeypatch,tmp_path):
    import ab_ai_context as ai
    import builder_crosstrade as native
    import test_ab_routing as af
    import test_builder_crosstrade as nf
    a=af.ContextTests();a.setUp();f=nf.NativeTests();f.setUp()
    try:
        f.now=a.now
        f.order.update(activation_ms=int(f.now*1000),expiry_ms=int((f.now+600)*1000))
        f.prepare()
        a.config.update(AB_AI_OWNER='AB_DIRECTIONAL',AB_AI_TRIGGER='SETUP',AB_V3_CONTRACT='MNQZ2026')
        service=ai.ContextSupport(a.config,reader=a.source,transport=a.transport,clock=lambda:a.now)
        monkeypatch.setattr(ai,'INSTANCE',service)
        monkeypatch.setattr(ai,'SETUP_COMPLETE_CALLBACK',None,raising=False)
        from test_directional_setup_review import order as ai_order
        setup=dict(f.order,candidate_id='integration-one',payload_json=ai_order(a)['payload_json'])
        assert service.queue_setup(setup)['state']=='queued'
        assert service.process_one()
        review=service.setup_annotation(f.order['order_id'],'LONG')
        assert review['state']=='ALLOW'
        args=evidence();args[5]['costs']['round_trip_usd']=1.2
        args[1].update(bid=29999.75,ask=30000.,as_of_ms=int(f.now*1000))
        args[2].update(order_id=f.order['order_id'],checked_ms=int(f.now*1000))
        args[3].update(order_id=f.order['order_id'],checked_ms=int(f.now*1000),account_binding=f.sync.binding)
        f.final_gate.side_effect=lambda o,r:gate.evaluate(o,args[1],args[2],args[3],review,args[5],int(f.now*1000))
        result=f.submit()
        assert result['sent'] and f.local()['position_json'] is None
        f.accept(f.order,dict(state='SENT',quantity=2,broker=result));f.open()
        assert f.local()['state']=='BROKER_CONFIRMED_OPEN'
        restarted=native.Transport(f.api,f.sync,request=f.request,clock=lambda:f.now)
        snapshot,net=f.complete(winning=False)
        restarted.reconcile_one(restarted.row(f.order['order_id']),snapshot)
        assert f.local()['state']=='CLOSED' and net==-42.4
        assert len(a.calls)==1 and len(f.posts)==1
    finally:
        f.doCleanups();a.doCleanups()


def test_market_account_and_thesis_have_separate_freshness_clocks():
    args=evidence()
    args[5].update(max_quote_age_ms=250,max_account_age_ms=3000,max_thesis_age_ms=2000)
    args[3]['checked_ms']-=2500
    args[2]['checked_ms']-=1500
    assert gate.evaluate(*args) is None
    args[1]['as_of_ms']-=251
    assert gate.evaluate(*args)=='quote_stale_or_future'
    args[1]['as_of_ms']+=251
    args[3]['checked_ms']-=501
    assert gate.evaluate(*args)=='account_risk_not_revalidated'


def test_native_directional_activity_uses_fill_without_certifying_costs(monkeypatch):
    import datetime as dt
    from types import SimpleNamespace
    import guardrails
    import builder_broker_sync
    monkeypatch.setenv('EXEC_STRATEGY_POLICY','V3_ONLY')
    monkeypatch.setenv('BUILDER_ORDER_PROVIDER','crosstrade')
    monkeypatch.setenv('ACCOUNT_PLAN','builder50')
    monkeypatch.setenv('ACCOUNT_STARTED_ON','2026-09-01')
    monkeypatch.setenv('INACTIVITY_DAYS','7')
    monkeypatch.setattr(builder_broker_sync,'instance',lambda:SimpleNamespace(
        last_confirmed_fill=lambda:dict(date='2026-10-20',et='2026-10-20T09:45:00-04:00',qty=5)))
    result=guardrails.inactivity_status([],now=dt.datetime(2026,10,20,20,tzinfo=dt.timezone.utc))
    assert result['status']=='ok' and result['last_trade_date']=='2026-10-20'
    assert result['source']=='broker' and result['last_trade']['net'] is None
    monkeypatch.setattr(builder_broker_sync,'instance',lambda:SimpleNamespace(last_confirmed_fill=lambda:None))
    assert guardrails.inactivity_status([],now=dt.datetime(2026,10,20,20,tzinfo=dt.timezone.utc))['status']=='unverified'


def test_activity_history_is_account_bound_and_excludes_future_voided_fills(tmp_path):
    import builder_broker_sync as b
    cfg=dict(DATA_DIR=str(tmp_path),ACCOUNT_PLAN='builder50',BUILDER_BROKER_SYNC_ENABLED='1',
        BUILDER_BROKER_ACCOUNT_VERIFIED='1',BUILDER_BROKER_ACCOUNT_ID='1',
        BUILDER_BROKER_ACCOUNT_NAME='fixture',BUILDER_BROKER_ENVIRONMENT='demo',BUILDER_BROKER_API_TOKEN='fixture')
    sync=b.AccountSync(cfg,clock=lambda:1792530000)
    base=dict(id=1,qty=1,action='Buy',active=True,timestamp='2026-10-20T13:45:00Z')
    with sync.connect() as db:
        for i,changes in enumerate(({},dict(active=False,timestamp='2026-10-20T15:00:00Z'),
            dict(timestamp='2026-10-21T15:00:00Z'),dict(accountId=2,timestamp='2026-10-20T16:00:00Z'))):
            f=dict(base,id=i+1,**changes)
            db.execute('INSERT INTO broker_fills VALUES(?,?,?,?,?)',(sync.binding,str(i+1),0,0,json.dumps(f)))
        db.execute('INSERT INTO broker_fills VALUES(?,?,?,?,?)',('foreign','99',0,0,json.dumps(dict(base,timestamp='2026-10-20T17:00:00Z'))))
    assert sync.last_confirmed_fill()['et']=='2026-10-20T09:45:00-04:00'
