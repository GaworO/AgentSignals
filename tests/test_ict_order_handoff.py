"""No paid requests and no broker writes; all proposals below are mocked."""
import copy
import json
import pytest
import ict_order_handoff as h
import ict_shadow_research as research
from test_ict_shadow_research import sample, T


def record(short=False):
    p,d=sample(short)
    p.pop('packet_id');p.update(cloud_shadow=True,session_end=T+1800,contract_configured='MNQZ2026')
    p['packet_id']=research.digest(p);d['packet_id']=p['packet_id']
    raw=dict(status='completed',output=[dict(type='message',content=[dict(type='output_text',text=json.dumps(d))])])
    return dict(id='mock_1',state='research_completed',packet=p,decision=d,response=raw,
                received_at=T+10,validation=research.validate(p,d,T+10))


def current(at=T):
    return dict(contract_configured='MNQZ2026',as_of=at,source_observed_at=at+10,evidence={})


@pytest.mark.parametrize('short',[False,True])
def test_bound_native_limit_bracket_preview(short):
    r=record(short);p=h.proposal(r,'MNQZ2026',T+11);b=h.limit_preview(p)
    assert p['strategy']=='ICT_AI' and p['execution_authorized'] is False
    assert b['action']==('sell' if short else 'buy') and b['instrument']=='MNQZ6'
    assert b['limitPrice']==r['decision']['plan']['entry_price']
    assert b['stopLoss']==r['decision']['plan']['stop_price']
    assert b['takeProfit']==p['entry']+(-1 if short else 1)*2*abs(p['entry']-p['stop'])
    assert p['ai_liquidity_target']==r['decision']['plan']['target_price']
    assert b['tif']=='gtd' and b['qty'] is None and b['maxPositions']==1
    assert len(b['clOrdId'])<=64 and h.limit_preview(p)==b


@pytest.mark.parametrize('change',['decision','packet','validation','contract','expired','future_response','unknown_response','legacy','wait'])
def test_invalid_not_staged(change):
    r=record();contract='MNQZ2026';now=T+11
    if change=='decision':r['decision']['plan']['entry_price']+=.25
    if change=='packet':r['packet']['as_of']-=60
    if change=='validation':r['validation']['stop']-=.25
    if change=='contract':contract='MNQH2027'
    if change=='expired':now=T+601
    if change=='future_response':r['received_at']=now+1
    if change=='unknown_response':r['state']='research_unknown'
    if change=='legacy':r['packet']['cloud_shadow']=False
    if change=='wait':r['decision'].update(decision='WAIT',plan=None)
    with pytest.raises((ValueError,AssertionError)):h.proposal(r,contract,now)


@pytest.mark.parametrize('short',[False,True])
def test_new_candles_invalidating_plan(short):
    p=h.proposal(record(short),'MNQZ2026',T+11)
    q=current(T+60)
    assert h.market_reason(p,q,T+70)=='POST_REVIEW_CANDLE_GAP'
    q['evidence']['bar']=dict(kind='bar',symbol='MNQ',timeframe=1,time=T,available_at=T+60,low=113 if short else 104,high=116 if short else 107)
    assert h.market_reason(p,q,T+70) is None
    q['evidence']['bar']['high' if short else 'low']=p['stop']
    assert h.market_reason(p,q,T+70)=='BRACKET_TOUCHED_BEFORE_SUBMISSION'
    q['as_of']=T
    assert h.market_reason(p,q,T+70)=='CURRENT_CLOSED_CANDLE_MISSING'


def test_restart_duplicate_and_expiry_never_replay(tmp_path):
    c=h.Handoff(tmp_path,{'AB_V3_CONTRACT':'MNQZ2026'})
    r=record();c.refresh([r],current(),T+11);c.refresh([r],current(),T+12)
    c=h.Handoff(tmp_path,c.config)
    assert len(c.state()['intents'])==1
    assert c.state()['intents'][0]['state']=='PREVIEW_BLOCKED'
    c.refresh([r],current(),T+601)
    c.refresh([r],current(),T+20) # clock rollback cannot revive expiry
    assert c.state()['intents'][0]['state']=='EXPIRED'
    assert not c.state()['execution_authorized']


def test_changed_same_review_id_quarantined(tmp_path):
    c=h.Handoff(tmp_path,{'AB_V3_CONTRACT':'MNQZ2026'})
    c.refresh([record()],current(),T+11)
    c.refresh([record(True)],current(),T+12)
    assert c.state()['intents'][0]['state']=='CHANGED_PROPOSAL'
    c.refresh([record()],current(),T+13)
    assert c.state()['intents'][0]['state']=='CHANGED_PROPOSAL'


def test_wait_produces_no_intent(tmp_path):
    c=h.Handoff(tmp_path,{})
    c.refresh([{'id':'wait','decision':{'decision':'WAIT','plan':None}}],{},T)
    assert not c.state()['intents']


def test_no_submission_capability_even_with_existing_flags(tmp_path):
    c=h.Handoff(tmp_path,dict(AB_V3_CONTRACT='MNQZ2026',LIVE_EXECUTION_ENABLED='true',V3_ENTRY_ARMED='1',ICT_ENTRY_ARMED='1'))
    c.refresh([record()],current(),T+11)
    assert c.state()['mode']=='EXECUTION_DISABLED' and not c.state()['broker_order_sent']
    assert not hasattr(c,'submit') and c.state()['intents'][0]['quantity'] is None


def test_sized_preview_retains_no_write_permission(tmp_path):
    c=h.Handoff(tmp_path,{'AB_V3_CONTRACT':'MNQZ2026'},sizing=lambda *a:dict(allowed=True,max_quantity=3,risk_percent=.5))
    c.refresh([record()],current(),T+11)
    x=c.state()['intents'][0]
    assert x['command_preview']['qty']==3 and x['quantity']==3
    assert not x['execution_authorized'] and not x['broker_order_sent']
    assert c.state()['risk_percent']==.5 and c.state()['target_rule']=='FIXED_2R_GROSS'


def test_half_percent_preview_uses_current_equity_costs_and_caps(monkeypatch):
    import builder_broker_sync as broker
    import directional_account_limits as account
    import execution_validation as execution
    from types import SimpleNamespace
    snapshot={'account':{'balance':{'netLiq':48721.05}}}
    sync=SimpleNamespace(enabled=True,binding='mock-account',state=lambda:{'current':True},snapshot=lambda:snapshot)
    monkeypatch.setattr(broker,'instance',lambda:sync)
    monkeypatch.setattr(execution,'policy',lambda:{'contract':'MNQZ2026'})
    monkeypatch.setattr(execution,'costs',lambda p:{'round_trip_usd':2.24,'stop_slippage_ticks':2})
    seen=[]
    def cap(s,b,o,at,cost,budget):
        seen.append((o,cost,budget))
        return dict(allowed=True,max_quantity=4,risk_per_contract_usd=63.24)
    monkeypatch.setattr(account,'capacity',cap)
    plan=h.proposal(record(),'MNQZ2026',T+11)
    plan.update(entry=30000,stop=29970,target=30060)
    v=h.sizing_preview(plan,T+11)
    assert seen[0][1]==3.24 and seen[0][2]==243.60
    assert v['max_quantity']==3 and v['total_risk_usd']==189.72
    assert seen[0][0]['stop_price']==29970 and v['preview_only']
    sync.state=lambda:{'current':False}
    assert h.sizing_preview(plan,T+11)['max_quantity']==0


def test_unknown_account_capacity_never_becomes_one(monkeypatch):
    import builder_broker_sync as broker
    monkeypatch.setattr(broker,'instance',lambda:(_ for _ in ()).throw(ValueError('private provider error')))
    r=h.sizing_preview(h.proposal(record(),'MNQZ2026',T+11),T+11)
    assert not r['allowed'] and r['max_quantity']==0 and 'private' not in str(r)
