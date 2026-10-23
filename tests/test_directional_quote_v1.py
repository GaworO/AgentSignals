"""Offline regressions for the LIVE V1 quote/entry boundary; no provider IO."""
import copy
import json
import pytest
import directional_market_data as q
import execution_validation as gate
from test_execution_readiness import evidence
from test_directional_completion import quote
from test_directional_quotes import client, collective

@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_market_side_and_original_limit_preserved(side):
    a=evidence()
    if side=='SHORT':
        a[0].update(direction=side,stop_price=110.,target_price=80.)
        a[1].update(bid=100.,ask=100.25)
        a[4]['setup']=copy.deepcopy(a[0])
    original=copy.deepcopy(a[0])
    assert gate.evaluate(*a) is None
    result=gate.price_assessment(a[0],a[1],a[5],a[-1])
    assert result['price_side']==('ask' if side=='LONG' else 'bid')
    a[1].update(bid=100 if side=='LONG' else 99.75,ask=100.25 if side=='LONG' else 100)
    assert gate.evaluate(*a)=='original_limit_exceeded'
    assert a[0]==original

@pytest.mark.parametrize('source',['collective','drafted'])
def test_normalized_consensus_cannot_be_forged_by_boolean_flags(source):
    a=evidence();a[1]['source']=a[5]['quote_source']=source
    assert gate.evaluate(*a)=='quote_source_unverified'

@pytest.mark.parametrize('key,value', [('max_quote_age_ms',float('nan')),('max_spread_ticks',float('inf')),
    ('entry_zone_points',0),('max_quote_latency_ms',-1),('max_quote_latency_ms',True)])
def test_invalid_numeric_policy_is_not_a_bypass(key,value):
    a=evidence();a[5][key]=value
    assert gate.evaluate(*a) is not None

@pytest.mark.parametrize('change,reason', [
    ({'contract_last_trade_ms':1000000},'contract_expired'),
    ({'contract':'NQZ2026'},'revalidation_evidence_incomplete'),
    ({'contract_last_trade_ms':None},'revalidation_evidence_incomplete'),
])
def test_contract_expiry_or_missing_proof_blocks(change,reason):
    a=evidence();a[5].update(change)
    assert gate.evaluate(*a)==reason

@pytest.mark.parametrize('latency',[None,-1,float('nan'),1001,True])
def test_final_gate_rechecks_request_latency(latency):
    a=evidence();a[1]['request_latency_ms']=latency
    assert gate.evaluate(*a)=='quote_request_latency_exceeded_or_unknown'


def test_source_proof_expiration_and_exact_setup_id():
    a=evidence();a[1]['provenance_expires_at_ms']=a[-1]
    assert gate.evaluate(*a)=='quote_verification_expired'
    a=evidence();a[4]['setup']['order_id']='different-order'
    assert gate.evaluate(*a)=='setup_review_geometry_mismatch'
    a=evidence();a[0]['order_type']='MARKET'
    assert gate.evaluate(*a)=='exact_directional_limit_setup_required'

@pytest.mark.parametrize('change',[{'verified_at_ms':1000001},{'expires_at_ms':1000000},
    {'verified_at_ms':None},{'expires_at_ms':float('inf')}])
def test_nt8_requires_current_provider_proof(change):
    a=evidence();a[5]['quote_verification'].update(change)
    with pytest.raises(ValueError,match='quote_source_suitability_unverified'):
        q.normalize(quote(),a[0],a[5],a[-1])


def test_source_switch_survives_missing_quote_and_restart(tmp_path):
    c=client(tmp_path,collective());c.fetch('MNQZ2026')
    c.clock=lambda:1002
    c.reader=lambda _: (_ for _ in ()).throw(TimeoutError())
    c.fetch('MNQZ2026')
    c=client(tmp_path,dict(collective(),source='drafted',pricingBasis='drafted_consensus',ts=1010,eventTs=1010))
    c.clock=lambda:1010
    result=c.fetch('MNQZ2026')
    assert result['source_changed'] and result['previous_source']=='collective'
    assert not result['allowed']
    assert result['quote']['pricing_basis']=='drafted_consensus'
    assert not result['quote']['timestamp_semantics_verified']


def test_observational_price_is_not_reported_as_executable():
    a=evidence();a[1]['execution_eligible']=False
    r=gate.price_assessment(a[0],a[1],a[5],a[-1])
    assert r['observed_side_price']==100 and r['executable_price'] is None
    assert r['observational_only']


def test_calibration_uses_ny_windows_and_counts_invalid_quotes():
    from tools.quote_observation_report import summarize,session,review_movements
    # 2026-10-23 13:30 UTC = 09:30 New York, not Berlin time.
    now=1792762200000
    assert session(now)=='2026-10-23 NYAM'
    assert session(now-1)=='2026-10-23 OUTSIDE'
    rows=[dict(observed_ms=now,raw=dict(collective(),ts=now/1000,eventTs=now/1000-2,bid=100.125,ask=100.5),reason='quote_bid_ask_invalid',request_latency_ms=10)]
    r=summarize(rows)['groups']['2026-10-23 NYAM']
    assert r['off_tick_count']==1 and r['verified_executable_count']==0
    pair=review_movements([dict(order_id='one',review_id='r',created=now/1000)],[dict(id='r',requested_at=now/1000,response_received_at=now/1000+10)],rows)
    assert pair[0]['status']=='UNKNOWN'
