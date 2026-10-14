"""Synthetic price sequences, not historical trades or model responses."""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
from entry_rules import fingerprint
from context_v3 import unknown_decision
from entry_review import ASSESSMENTS, compile_review


def fixture(profile='manipulation_retracement_inversion',direction='long',mode='immediate_after_confirmation'):
    t=int(datetime(2026,10,8,9,40,tzinfo=ZoneInfo('America/New_York')).timestamp())
    ev={}
    def bar(key,time,tf,o,h,l,c):
        ev[key]=dict(kind='bar',symbol='MNQ',timeframe=tf,time=time,available_at=time+tf*60,open=o,high=h,low=l,close=c)
    bar('h1-objective',t-7200,60,105,112,100,110)
    bar('m15-a',t-3600,15,102,104,97,101)
    bar('m15-b',t-2700,15,104,111,103,110)
    bar('m15-c',t-1800,15,111,113,108,112)
    bar('objective-touch',t-2760,1,102,103,99,101)
    bar('manipulation',t-3000,1,105,106,98,103)
    bar('structure',t-2940,1,103,104,97,102)
    bar('retracement',t-240,1,109,110,107,108)
    bar('a',t-180,1,110,112,109,110)
    bar('b',t-120,1,107,108,105,106)
    bar('c',t-60,1,106,107,105,106)
    bar('trigger-bar',t,1,106,113,105,112)
    ev['m15-gap']=dict(kind='FVG',symbol='MNQ',timeframe=15,direction='bull',lower=104,upper=108,
        available_at=t-900,evidence_ids=['m15-a','m15-b','m15-c'])
    ev['liquidity-level']=dict(kind='level',symbol='MNQ',price=100,available_at=t-3060)
    ev['swing']=dict(kind='pivot',symbol='MNQ',timeframe=1,price=100,side='low',available_at=t-3060)
    ev['final']=dict(kind='level',symbol='MNQ',price=125,available_at=t-3060)
    ev['local']=dict(kind='level',symbol='MNQ',price=115,available_at=t-3060)
    ev['trigger']=dict(kind='IFVG',symbol='MNQ',timeframe=1,direction='long',lower=107,upper=109,close=112,
        available_at=t+60,evidence_ids=['a','b','c','trigger-bar'])
    sequence=dict(retracement_zone_id='m15-gap',retracement_bar_id='retracement',
        manipulation=dict(level=dict(evidence_id='liquidity-level',field='price'),bar_id='manipulation',side='below'),
        structure=dict(swing_id='swing',bar_id='structure',role='liquidity_sweep',mode='wick'),
        objective=None,objective_touch_bar_id=None)
    if profile=='htf_fvg_retracement_inversion':
        sequence.update(manipulation=None,structure=None,objective=dict(evidence_id='h1-objective',field='low'),objective_touch_bar_id='objective-touch')
    adverse='low'
    if direction=='short':
        adverse='high'
        for e in ev.values():
            if e['kind']=='bar':
                e.update(open=300-e['open'],high=300-e['low'],low=300-e['high'],close=300-e['close'])
            else:
                if 'price' in e:e['price']=300-e['price']
                if 'lower' in e:e.update(lower=300-e['upper'],upper=300-e['lower'])
                if 'close' in e:e['close']=300-e['close']
                if e.get('direction')=='bull':e['direction']='bear'
                elif e.get('direction')=='long':e['direction']='short'
                if e.get('side')=='low':e['side']='high'
        if sequence['manipulation']:sequence['manipulation']['side']='above'
        if sequence['objective']:sequence['objective']['field']='high'
    p=dict(input_mode='market_only',labels_included=False,as_of=t+60,evidence=ev)
    p['packet_id']=fingerprint(p)
    d=unknown_decision(p);ctx=d['context']
    ctx.update(bias=direction,decision='candidate',setup='inversion',requires_smt=False,selected_trigger_id='trigger',missing_context=[],range_selection=None)
    for name,v in ctx['claims'].items():
        v.update(value=None if name in ('liquidity_event_complete','setup_origin_at_range_extreme') else True,
            evidence_ids=[] if name in ('liquidity_event_complete','setup_origin_at_range_extreme') else ['trigger-bar'],reason='Synthetic reviewed context')
    stop=ev['structure'][adverse]+(-.25 if direction=='long' else .25)
    d.update(variant_id='V01',analysis_symbol='MNQ',execution_symbol='MNQ',timeframe_minutes=1,
        poi_evidence_ids=['m15-gap'],selected_swing=None,require_swing_break=False,entry_mode=mode,
        invalidation=dict(evidence_id='structure',field=adverse),
        initial_stop=dict(anchor=dict(evidence_id='structure',field=adverse),buffer_ticks=1,price=stop,reason='Selected structural invalidation'),
        initial_quantity=None,missing_fields=[],rationale='Synthetic reviewed entry sequence')
    d['management'].update(policy='structural_trail',partial_policy='One-third original at selected local objective',
        targets=[dict(kind='level',anchor=dict(evidence_id='final',field='price'),extreme=None,price=ev['final']['price'],quantity=None,reason='Selected final objective')])
    d['entry_setup']=dict(setup_profile=profile,setup_sequence=sequence,
        entry_anchor=dict(evidence_id='a',field=adverse) if mode=='retracement' else None,
        entry_mode_reason='Optional further retracement' if mode=='retracement' else 'Entry after accepted inversion close',
        valid_for_seconds=180,trade_kind='reversal',
        assessments={k:dict(accepted=True,reason='Synthetic explicit assessment',evidence_ids=['trigger-bar']) for k in ASSESSMENTS},
        local_objective=dict(anchor=dict(evidence_id='local',field='price'),symbol='MNQ',reason='Selected earlier profit objective'))
    return p,d


def decision_fixture(profile='manipulation_retracement_inversion',direction='long',mode='immediate_after_confirmation'):
    p,d=fixture(profile,direction,mode);at=p['as_of']+1
    result=compile_review(p,d,selected_at=at,now=at,budget=250,cap=40)
    if not result['compiled']['plan']:raise AssertionError(result)
    result.update(frozen_at=at,ai_context=dict(state='PERMITTED',direction=direction),
        scheduled_news=dict(state='CLEAR'),news_entry_reasons=[],
        account_guard=dict(state='REPORTED_LIMITS_CLEAR',budget_usd=250,max_contracts=40))
    objective=deepcopy(result['compiled']['plan']['management_spec']['partials'][0]['objective'])
    return result,p['as_of'],objective
