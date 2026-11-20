"""Regression for real provider confusing symbols with market observation IDs."""
import copy,json
from unittest.mock import patch
import pytest,jsonschema
from services.tanja.ict_agent import reasoning as r
from tests.test_ict_ai_trader import store,packet,answer,paid,response


def replace_strings(x,mapping):
    if isinstance(x,str):return mapping.get(x,x)
    if isinstance(x,list):return [replace_strings(v,mapping) for v in x]
    if isinstance(x,dict):return {replace_strings(k,mapping):replace_strings(v,mapping) for k,v in x.items()}
    return x


def test_lossless_full_packet_and_reference_domains(store):
    p=packet(store);original=copy.deepcopy(p);body=r.request_body(p,'fixture',2000)
    wire=json.loads(body['input'][1]['content']);aliases=r.wire_references(p)
    assert replace_strings(wire,{v:k for k,v in aliases.items()})==original==p
    a=answer(p);ident=next(iter(p['evidence']));alias=aliases[ident]
    a['changes']['market_ids']=[alias]
    validator=jsonschema.Draft202012Validator(body['text']['format']['schema'])
    validator.validate(a)
    for invalid in ('NQ','m9999',ident):
        a['changes']['market_ids']=[invalid]
        with pytest.raises(jsonschema.ValidationError):validator.validate(a)
    a['changes']['market_ids']=[alias];a['snapshot_id']='unknown'
    with pytest.raises(jsonschema.ValidationError):validator.validate(a)
    a['snapshot_id']=p['snapshot']['id'];a['as_of']=0
    with pytest.raises(jsonschema.ValidationError):validator.validate(a)


def test_raw_aliases_persist_and_prices_remain_exactly_validated(store):
    p=packet(store);a=answer(p);ident,e=next((i,e) for i,e in p['evidence'].items() if 'high' in e)
    alias=r.wire_references(p)[ident];a['changes']['market_ids']=[alias]
    a['assessments'][0]['supporting_market_ids']=[alias]
    a['claims']=[dict(text='Observed high',facts=[dict(evidence_id=alias,field='high',value=e['high'])],source_rule_ids=['fixture-rule'])]
    with patch.object(r,'prepare',return_value=p):
        out=r.review(store,p['snapshot']['id'],paid(store),transport=lambda _:response(a),clock=lambda:p['snapshot']['as_of'])
    assert out['status']=='VALIDATED_ADVISORY' and out['orders_sent']==0
    assert out['answer']['claims'][0]['facts'][0]['evidence_id']==ident
    with store.readonly() as db:row=json.loads(db.execute('SELECT payload FROM reasoning_reviews').fetchone()[0])
    raw=json.loads(row['response']['output'][0]['content'][0]['text'])
    assert raw['changes']['market_ids']==[alias] and row['packet']==p
    a['claims'][0]['facts'][0]['value']+=.25
    with pytest.raises(ValueError,match='NUMERICAL_FACT_MISMATCH'):r.validate(r.canonical_answer(a,p),p,p['snapshot']['as_of'])
    a['changes']['market_ids']=['m9999']
    with pytest.raises(ValueError,match='INVENTED_CHANGE_EVIDENCE'):r.validate(r.canonical_answer(a,p),p,p['snapshot']['as_of'])


def test_norag_sources_empty_and_all_hypotheses_required(store):
    p=r.prepare(store,store.snapshot()['id'],variant='B_LLM_NO_RAG');a=answer(p)
    validator=jsonschema.Draft202012Validator(r.wire_schema(p));validator.validate(a)
    a['selected_rule_ids']=['made-up-rule']
    with pytest.raises(jsonschema.ValidationError):validator.validate(a)
    a['selected_rule_ids']=[];a['assessments'].pop()
    with pytest.raises(jsonschema.ValidationError):validator.validate(a)


def test_large_reference_domain_never_truncates_and_limit_fails_before_call(store):
    p=packet(store);example=next(iter(p['evidence'].values()))
    p['evidence']={f'long-canonical-market-observation-{i:024d}':dict(example,id=f'long-canonical-market-observation-{i:024d}') for i in range(750)}
    wire=r.wire_packet(p);schema=r.wire_schema(p)
    assert len(wire['evidence'])==len(schema['$defs']['MarketID']['enum'])==750
    assert sum(map(len,schema['$defs']['MarketID']['enum']))<15000
    p['evidence'].update({f'extra-{i}':example for i in range(300)})
    with patch.object(r,'prepare',return_value=p),pytest.raises(ValueError,match='REFERENCE_SCHEMA_ENUM_LIMIT'):
        r.review(store,p['snapshot']['id'],paid(store),transport=lambda _:pytest.fail('must fail before spending'),clock=lambda:p['snapshot']['as_of'])
    with store.readonly() as db:assert db.execute('SELECT count(*) FROM reasoning_budget').fetchone()[0]==0


def test_alias_collision_is_never_silently_reinterpreted(store):
    p=packet(store);p['audit_notes'].append({'text':'m0000'})
    with pytest.raises(ValueError,match='WIRE_REFERENCE_COLLISION'):r.wire_packet(p)
