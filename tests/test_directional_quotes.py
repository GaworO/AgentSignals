"""Production quote adapter with injected HTTP; never sends an order."""
import json
from urllib.error import HTTPError
from unittest.mock import patch

import pytest
import directional_market_data as q
import execution_validation as gate
from test_execution_readiness import evidence
from test_directional_completion import quote, allowance_fixture
import directional_account_limits as limits


def collective():
    return dict(instrument='MNQ 12-26',tickSize=.25,pointValue=2,
                source='collective',confidence='high',pricingBasis='cluster_mean_mid',
                bid=29906.5,ask=29907.,ts=999.8,eventTs=999.5,marketOpen=True)


def client(tmp_path,raw=None,reader=None):
    return q.QuoteClient(clock=lambda:1000,monotonic=lambda:10,
                         reader=reader or (lambda c:raw),path=tmp_path/'quotes.db')


def test_collective_observed_logged_but_never_authorizes(tmp_path,monkeypatch):
    c=client(tmp_path,collective());r=c.fetch('MNQZ2026')
    assert r['quote']['spread_ticks']==2 and r['quote']['quote_age_ms']==500
    assert r['raw']==collective() and json.loads(r['original_response_text'])==collective()
    assert not r['allowed'] and not r['quote']['execution_eligible']
    # Restart shares pacing; later request must STILL reject collective even configured as NT8.
    c=client(tmp_path,collective());c.clock=lambda:1002
    monkeypatch.setenv('DIRECTIONAL_QUOTE_SOURCE','crosstrade_nt8')
    with pytest.raises(ValueError,match='quote_source_suitability_unverified'):
        c.fetch('MNQZ2026',execution=True,parameters=dict(max_quote_age_ms=10000))
    assert not c.latest()['allowed']


def test_drafted_consensus_timestamp_observation_is_not_bbo_attestation(tmp_path):
    # Shape observed from the production provider on 2026-10-22, prices/time
    # shifted for a deterministic offline fixture. It cannot certify a venue.
    raw=dict(collective(),source='drafted',pricingBasis='drafted_consensus')
    r=client(tmp_path,raw).fetch('MNQZ2026')
    assert r['quote']['quote_age_ms']==500 and r['quote']['spread_ticks']==2
    assert r['quote']['market_open'] and r['quote']['source']=='drafted'
    assert r['reason']=='quote_source_suitability_unverified'
    fake_proof=dict(quote_verification=dict(verified=True,source='drafted',contract='MNQZ2026',
        reference='UNIT_TEST_ONLY',timestamp_semantics='bid_ask_event_time',venue_bbo=True))
    assert not q.attest_quote(r['quote'],fake_proof)['execution_eligible']


@pytest.mark.parametrize('change,reason',[
    ({'instrument':'MNQ 03-27'},'quote_contract_metadata_mismatch'),
    ({'tickSize':.5},'quote_contract_metadata_mismatch'),
    ({'bid':29908},'quote_bid_ask_invalid'),({'ask':None},'quote_bid_ask_invalid'),
    ({'bid':0},'quote_bid_ask_invalid'),({'bid':True},'quote_bid_ask_invalid'),
    ({'bid':float('nan')},'quote_bid_ask_invalid'),({'ask':29907.1},'quote_bid_ask_invalid'),
    ({'eventTs':980},'quote_stale_or_future'),({'ts':1001},'quote_stale_or_future'),
    ({'eventTs':None},'quote_provider_timestamp_required'),({'stale':True},'quote_unavailable'),
])
def test_invalid_observations_persist_without_prices_fallback(tmp_path,change,reason):
    c=client(tmp_path,dict(collective(),**change));r=c.fetch('MNQZ2026')
    assert not r['allowed'] and r['reason']==reason
    assert c.latest()['original_response_text']


def test_pacing_and_retry_after_persist_across_restart(tmp_path):
    calls=[]
    def read(c):
        calls.append(c)
        raise HTTPError('https://example.invalid',429,'limit',{'Retry-After':'60'},None)
    c=client(tmp_path,reader=read);r=c.fetch('MNQZ2026')
    assert r['http_status']==429
    c=client(tmp_path,reader=read);c.clock=lambda:1059
    assert c.fetch('MNQZ2026')['reason']=='quote_rate_limited_locally'
    assert len(calls)==1
    c.clock=lambda:1060;c.fetch('MNQZ2026');assert len(calls)==2


@pytest.mark.parametrize('error',[TimeoutError(),OSError(),ValueError('secret arbitrary text')])
def test_errors_fail_closed_no_retry_loop(tmp_path,error):
    def read(c):raise error
    c=client(tmp_path,reader=read)
    with pytest.raises(ValueError):c.fetch('MNQZ2026',execution=True,parameters=dict(max_quote_age_ms=1000,quote_verification=evidence()[5]['quote_verification']))
    assert 'secret arbitrary text' not in json.dumps(c.latest())


def test_verified_nt8_and_latency_then_final_gate_reages_quote(tmp_path,monkeypatch):
    c=client(tmp_path,quote());monkeypatch.setenv('DIRECTIONAL_QUOTE_SOURCE','crosstrade_nt8')
    r=c.fetch('MNQZ2026',execution=True,parameters=dict(max_quote_age_ms=1000,quote_verification=evidence()[5]['quote_verification']))
    assert r['execution_eligible'] and r['spread_ticks']==1
    args=evidence();args[1].update(r);args[5]['quote_source']='crosstrade_nt8'
    assert gate.evaluate(*args) is None
    args[-1]+=1001
    assert gate.evaluate(*args)=='quote_stale_or_future'
    c.clock=lambda:1002;c.reader=lambda c:dict(quote(),epoch=1002)
    times=iter([10,12,12]);c.monotonic=lambda:next(times)
    with pytest.raises(ValueError,match='quote_request_latency_exceeded'):
        c.fetch('MNQZ2026',execution=True,parameters=dict(max_quote_age_ms=1000,quote_verification=evidence()[5]['quote_verification']))


def test_even_matching_policy_cannot_accept_explicit_unverified_source():
    args=evidence();args[1]['execution_eligible']=False
    assert gate.evaluate(*args)=='quote_source_unverified'


def test_original_raw_quote_has_no_token_in_durable_journal(tmp_path,monkeypatch):
    monkeypatch.setenv('BUILDER_BROKER_API_TOKEN','test-private-token')
    c=client(tmp_path,dict(collective(),echo='test-private-token'))
    c.fetch('MNQZ2026')
    assert 'test-private-token' not in json.dumps(c.latest())


def test_real_tradovate_balance_aliases_and_conflicts(tmp_path,monkeypatch):
    args,proof,path=allowance_fixture(tmp_path,monkeypatch)
    b=args[0]['account']['balance']
    for a,z in [('cashSOD','totalCashValueSOD'),('fundsInTransit','fundsInTransitUSD'),
                ('fundsInTransitAppliedToCash','fundsInTransitAppliedToCashUSD')]:b[z]=b.pop(a)
    assert limits.check(*args) is None
    b['cashSODUSD']=b['totalCashValueSOD']+10
    assert limits.check(*args)=='account_wide_pnl_unreconciled'
