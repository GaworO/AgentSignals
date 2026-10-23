"""Official NT8 REST contract; mock inputs only, never external market/order IO."""
from datetime import datetime, timezone
import copy
import pytest
import directional_market_data as q
from test_directional_completion import quote
from test_directional_quotes import client,collective
from test_execution_readiness import evidence

REMOVED=dict(source='drafted',pricingBasis='cluster_mean_mid',confidence='low',pricingPrice=999,
             ts=-999,eventTs=9999999999999,marketOpen=False)

@pytest.mark.parametrize('fields',[
 {'epoch':1000}, {'quoteTimeUtc':'1970-01-01 00:16:40'},
 {'quoteTimeUtc':'1970-01-01T00:16:40Z'},
 {'quoteTime':'1970-01-01T01:16:40+01:00'},
 {'epoch':1000,'quoteTimeUtc':'1970-01-01 00:16:40','quoteTime':'01/01/1970 01:16:40 AM'},
])
def test_documented_timestamps_parse_consistently(fields):
 raw=quote();raw.pop('epoch');raw.update(fields)
 a=evidence();got=q.normalize(raw,a[0],a[5],a[-1])
 assert got['as_of_ms']==1000000 and got['source']=='crosstrade_nt8'
 assert got['execution_eligible']

@pytest.mark.parametrize('fields,reason',[
 ({'epoch':1000000},'quote_stale_or_future'),  # milliseconds are not REST epoch seconds
 ({'quoteTimeUtc':'bad'},'quote_provider_timestamp_invalid'),
 ({'quoteTimeUtc':'1970-01-01'},'quote_provider_timestamp_invalid'),
 ({'quoteTimeUtc':'1970-01-01T01:16:40+01:00'},'quote_utc_timestamp_offset_invalid'),
 ({'epoch':1000,'quoteTimeUtc':'1970-01-01 00:16:38'},'quote_provider_timestamps_conflict'),
 ({'quoteTime':'11/01/2026 01:30:00 AM'},'quote_provider_timestamp_required'),
 ({'quoteTime':'2026-11-01 01:30:00'},'quote_provider_timestamp_required'),
 ({'epoch':float('nan')},'quote_provider_timestamp_required'),
 ({'epoch':True},'quote_provider_timestamp_required'),
 ({'quoteTimeUtc':'1970-01-01T00:16:41Z'},'quote_stale_or_future'),
 ({'quoteTimeUtc':'1970-01-01T00:16:38Z'},'quote_stale_or_future'),
])
def test_unknown_ambiguous_conflicting_or_stale_time_blocks(fields,reason):
 raw=quote();raw.pop('epoch');raw.update(fields)
 with pytest.raises(ValueError,match=reason):q.inspect_quote(raw,'MNQZ2026',1000000,1000)

@pytest.mark.parametrize('source',['collective','drafted','nt8',None])
def test_removed_fields_are_irrelevant_to_valid_documented_feed(source):
 raw=quote();a=evidence()
 expected=q.normalize(raw,a[0],a[5],a[-1])
 raw.update(REMOVED,source=source)
 assert q.normalize(raw,a[0],a[5],a[-1])==expected


def test_internal_market_open_cannot_replace_documented_field():
 raw=quote();raw.pop('isMarketOpen');raw['marketOpen']=True
 assert not q.inspect_quote(raw,'MNQZ2026',1000000,1000)['market_open']


def test_schema_alone_is_not_runtime_entitlement_or_provenance():
 raw=quote();raw.update(REMOVED)
 observed=q.inspect_quote(raw,'MNQZ2026',1000000,1000)
 assert observed['feed_candidate'] and not observed['execution_eligible']
 assert not q.attest_quote(observed,{})['execution_eligible']
 raw['dataSource']={'status':'Delayed'}
 a=evidence()
 with pytest.raises(ValueError,match='quote_source_suitability_unverified'):q.normalize(raw,a[0],a[5],a[-1])


def test_internal_field_change_does_not_signal_connection_change(tmp_path):
 c=client(tmp_path,dict(quote(),source='collective'));c.fetch('MNQZ2026')
 c.clock=lambda:1002;c.reader=lambda _:dict(quote(),epoch=1002,**REMOVED)
 r=c.fetch('MNQZ2026')
 assert not r['data_source_changed'] and 'source_changed' not in r
 assert r['quote']['source']=='crosstrade_nt8' and 'pricing_basis' not in r['quote']


def test_timestamp_precision_uses_oldest_supported_value():
 raw=quote();raw.update(epoch=1000.9,quoteTimeUtc='1970-01-01 00:16:40')
 got=q.inspect_quote(raw,'MNQZ2026',1000900,1000)
 assert got['quote_age_ms']==900


def test_calibration_ignores_legacy_timestamp_and_source_fields():
 from tools.quote_observation_report import summarize
 raw=quote();raw.update(epoch=1792762200,**REMOVED)
 good=q.inspect_quote(raw,'MNQZ2026',1792762200000,1000)
 rows=[dict(observed_ms=1792762200000,raw=raw,quote=good,request_latency_ms=20),
       dict(observed_ms=1792762200001,raw=collective(),reason='quote_provider_timestamp_required')]
 group=summarize(rows)['groups']['2026-10-23 NYAM']
 assert group['reported_quote_age_ms']['n']==1
 assert group['unsupported_timestamp_count']==1
 assert 'drafted' not in group['sources']
