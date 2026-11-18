"""Midpoint guidance and retained geometry checks. No paid calls."""
import copy
import pytest
import ict_shadow_research as r
from test_ict_shadow_research import sample,T

def test_midpoint_guidance_applies_to_both_families():
 assert r.VERSION=='ict-2022-ifvg-cloud-v6-entry-timing-liquidity'
 assert r.MIDPOINT_GUIDANCE in r.PROMPT
 for requirement in ('BOTH ICT2022 and IFVG','entry_fraction=0','entry_fraction=0.5','entry_fraction=1','No location has a default preference or priority','must not veto a supported LOW or HIGH entry','In entry_reason record gap bounds','ONE final plan per family','Existing pending orders retain their exact approved geometry','return WAIT/NO_TRADE'):
  assert requirement in r.MIDPOINT_GUIDANCE
 assert 'Prefer entry_fraction=0.5' not in r.PROMPT

@pytest.mark.parametrize('short',[False,True])
def test_midpoint_half_tick_uses_existing_directional_rounding(short):
 p,d=sample(short)
 if short:
  p['evidence']['0']['low']=116.25
  d['plan']['entry_price']=115.25
 else:
  p['evidence']['0']['high']=104.25
  d['plan']['entry_price']=105.
 assert r.validate(p,d,T+10)['state']=='VALIDATED_SHADOW_PROPOSAL'
 d['plan']['entry_price']+=-.25 if short else .25
 with pytest.raises(AssertionError,match='ENTRY_NOT_DERIVED'):r.validate(p,d,T+10)

@pytest.mark.parametrize('short',[False,True])
def test_midpoint_cannot_invent_liquidity_or_change_structural_stop(short):
 p,d=sample(short)
 price=110 if not short else 110
 p['evidence']['target']['price']=price
 d['plan']['target_anchor']['price']=price;d['plan']['target_price']=price
 with pytest.raises(AssertionError):r.validate(p,d,T+10)
 p,d=sample(short);d['plan']['stop_price']+=-.25 if short else .25
 with pytest.raises(AssertionError):r.validate(p,d,T+10)

def test_edge_remains_compatible_with_saved_approvals():
 p,d=sample();d['plan'].update(entry_fraction=0,entry_price=104)
 assert r.validate(p,d,T+10)['state']=='VALIDATED_SHADOW_PROPOSAL'

@pytest.mark.parametrize('short',[False,True])
def test_supported_edge_is_not_blocked_when_midpoint_lacks_2r_room(short):
 p,d=sample(short)
 target=106 if short else 114
 p['evidence']['target']['price']=target
 d['plan']['target_anchor']['price']=target;d['plan']['target_price']=target
 original_stop=d['plan']['stop_price']
 with pytest.raises(AssertionError):r.validate(p,d,T+10)
 d['plan'].update(entry_fraction=1 if short else 0,entry_price=116 if short else 104)
 assert r.validate(p,d,T+10)['state']=='VALIDATED_SHADOW_PROPOSAL'
 assert d['plan']['stop_price']==original_stop
