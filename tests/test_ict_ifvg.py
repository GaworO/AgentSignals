import copy,json
import pytest
import ict_shadow_research as r
from ict_shadow_cloud import Cloud
from test_ict_shadow_research import sample,T

def setup(short=False):
    packet,base=sample()
    # Bullish FVG 104-106 -> later close below104 -> SHORT retest 105.
    packet.update(as_of=T+60,cloud_shadow=True,session_end=T+1800,review_families=['ICT2022','IFVG'])
    packet['evidence']['inv']=dict(kind='bar',symbol='MNQ',timeframe=1,time=T,available_at=T+60,open=107,high=108,low=101,close=102)
    packet['evidence']['target']['price']=94
    plan=base['plan'];plan.update(direction='SHORT',entry_price=105,stop_anchor=dict(evidence_id='inv',field='high',price=108),stop_price=108.25,target_anchor=dict(evidence_id='target',field='price',price=94),target_price=94,inversion_bar_id='inv',valid_until=T+600)
    child=dict(base,decision='PROPOSE_SHORT',packet_id=packet['packet_id'])
    if not short:
        # Mirror all price fields and preserve original bearish formation.
        for b in packet['evidence'].values():
            if b['kind']=='bar':b.update(open=220-b['open'],high=220-b['low'],low=220-b['high'],close=220-b['close'])
            else:b['price']=220-b['price']
        plan.update(direction='LONG',entry_price=115,stop_anchor=dict(evidence_id='inv',field='low',price=112),stop_price=111.75,target_anchor=dict(evidence_id='target',field='price',price=126),target_price=126)
        child['decision']='PROPOSE_LONG'
    parent=dict(packet_id=packet['packet_id'],execution_authorized=False,decision='WAIT',plan=None,assessments={},ifvg_review=child)
    return packet,parent

@pytest.mark.parametrize('short',[False,True])
def test_valid_ifvg_and_original_wait(short):
    p,d=setup(short);v=r.validate(p,d,T+70)
    assert v['state']=='WAIT'
    assert v['ifvg_validation']['state']=='VALIDATED_SHADOW_PROPOSAL'
    assert v['ifvg_validation']['route']=='SHADOW_ONLY'
    assert v['ifvg_validation']['gross_rr']==2
    assert abs(v['ifvg_validation']['target']-v['ifvg_validation']['entry'])==2*abs(v['ifvg_validation']['entry']-v['ifvg_validation']['stop'])
    assert v['ifvg_validation']['execution_authorized'] is False

@pytest.mark.parametrize('mutation',['wick','future','wrong_tf','wrong_symbol','before_gap','wrong_direction','inverted_again','gap','expired','entry','sl','tp','binding','permission','unknown_id'])
def test_bad_ifvg_cannot_authorize_or_damage_original(mutation):
    p,d=setup(True);c=d['ifvg_review'];plan=c['plan'];inv=p['evidence']['inv'];at=T+70
    if mutation=='wick':inv['close']=104
    if mutation=='future':inv['available_at']=T+120
    if mutation=='wrong_tf':inv['timeframe']=5
    if mutation=='wrong_symbol':inv['symbol']='NQ'
    if mutation=='before_gap':inv['time']=T-60;inv['available_at']=T
    if mutation=='wrong_direction':plan['direction']='LONG';c['decision']='PROPOSE_LONG'
    if mutation=='inverted_again':
        p['as_of']=T+120;at=T+130
        p['evidence']['later']=dict(inv,time=T+60,available_at=T+120,close=107)
    if mutation=='gap':p['as_of']=T+120;at=T+130
    if mutation=='expired':plan['valid_until']=T+61
    if mutation=='entry':plan['entry_price']=107
    if mutation=='sl':plan['stop_price']=108
    if mutation=='tp':plan['target_price']=103
    if mutation=='binding':c['packet_id']='other'
    if mutation=='permission':c['execution_authorized']=True
    if mutation=='unknown_id':plan['inversion_bar_id']='missing'
    v=r.validate(p,d,at)
    assert v['state']=='WAIT'
    assert v['ifvg_validation']['state']=='BLOCKED'

@pytest.mark.parametrize('decision',['WAIT','NO_TRADE'])
def test_companion_wait(decision):
    p,d=setup();d['ifvg_review'].update(decision=decision,plan=None)
    assert r.validate(p,d,T+70)['ifvg_validation']['state']==decision

def test_original_long_and_short_unchanged_with_invalid_companion():
    for short in (False,True):
        p,d=sample(short);before=r.validate(p,d,T+10)
        p['review_families']=['ICT2022','IFVG']
        after=r.validate(p,d,T+10)
        assert {k:v for k,v in after.items() if k!='ifvg_validation'}==before
        assert after['ifvg_validation']['state']=='BLOCKED'

def test_schema_one_request_two_bound_families():
    s=r.schema('bound','contract',True)
    assert 'ifvg_review' in s['required']
    a=s['properties']['ifvg_review']
    assert a['properties']['packet_id']['enum']==['bound']
    assert 'inversion_bar_id' in a['properties']['plan']['anyOf'][1]['required']
    assert 'invalidation_mode' not in a['properties']['plan']['anyOf'][1]['required']
    assert 'ifvg_review' not in r.schema('legacy')['required']

def test_ifvg_never_handed_to_broker_even_if_enabled(monkeypatch):
    from ict_order_handoff import proposal
    monkeypatch.setenv('ICT_EXECUTION_ENABLED','true')
    for row in [dict(family='IFVG'),dict(parent_review_id='x'),dict(id='x:IFVG')]:
        with pytest.raises(ValueError,match='IFVG_REVIEW_ONLY'):proposal(row,'MNQZ2026',T)

def test_dashboard_two_assessments_one_call_and_parent_detail(tmp_path,monkeypatch):
    c=Cloud({'DATA_DIR':str(tmp_path)});p,d=setup();v=r.validate(p,d,T+70)
    record=dict(id='review',requested_at=T+65,packet=p,decision=d,validation=v)
    monkeypatch.setattr(c,'records',lambda:[record]);monkeypatch.setattr(c,'outcome',lambda r:{'state':'SIMULATED_WAITING'})
    s=c.state();assert len(s['reviews'])==2
    child=s['reviews'][1];assert child['parent_review_id']=='review' and child['family']=='IFVG'
    assert child['execution_route']=='SHADOW_ONLY' and s['calls_today']==0
    assert c.records()==[record] # broker sees parent records only

def test_inversion_event_requires_close(tmp_path):
    c=Cloud({'DATA_DIR':str(tmp_path)})
    p,_=setup(True)
    ev=[e for e in c.entry_events(p) if e['kind']=='OBSERVED_IFVG_INVERSION']
    assert len(ev)==1 and ev[0]['direction']=='SHORT' and ev[0]['available_at']==T+60
    p['evidence']['inv']['close']=104
    assert not [e for e in c.entry_events(p) if e['kind']=='OBSERVED_IFVG_INVERSION']
