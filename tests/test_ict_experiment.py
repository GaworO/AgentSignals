import copy,json
from pathlib import Path
import pytest
from unittest.mock import patch
from services.tanja.ict_agent import experiment as e,reasoning as r
from services.tanja.ict_agent.common import digest,read_json,save,read_bytes
from services.tanja.ict_agent.store import Store
from tests.test_ict_ai_trader import store,paid,answer,response


def test_pilot_plan_and_approval_binding(tmp_path):
 plan=read_json(Path(__file__).parents[1]/'docs/ict-ai-trader/reports/pilot-experiment.json')
 # Public report redacts the private source path; approvals bind private plan only.
 assert len(plan['cases'])==15 and len({x['id'] for x in plan['cases']})==15
 assert {x['category'] for x in plan['cases']}=={'AMBIGUOUS_EXECUTION','RESEARCH_CANDIDATE_NOT_SOURCE_VALIDATED','INCOMPLETE','INVALIDATED','NO_TRADE'}
 assert plan['request_count']==30 and plan['cost']['paid_calls_made']==0
 assert plan['cost']['suggested_total_cap_usd']>=plan['cost']['reserved_upper_usd']>plan['cost']['heuristic_usd']
 assert all(c['requests']['B_LLM_NO_RAG']['sources']==0 and c['requests']['C_LLM_RAG']['sources']>0 for c in plan['cases'])
 plan['id']=digest({k:v for k,v in plan.items() if k!='id'})[:20]
 a=dict(approved=True,experiment_id=plan['id'],plan_sha256=digest(plan),approval_id='TEST_ONLY',model=plan['model'],expires_at=200,max_calls=30,max_usd=plan['cost']['suggested_total_cap_usd'])
 assert e.approved_config(plan,a,100)['ICT_AI_DAILY_CALLS']=='30'
 for field,value in [('approved',False),('max_calls',31),('max_usd',100),('plan_sha256','wrong'),('expires_at',90)]:
  with pytest.raises(ValueError):e.approved_config(plan,dict(a,**{field:value}),100)
 with pytest.raises(ValueError,match='PLAN_ID_HASH'):e.approved_config(dict(plan,model='changed'),a,100)
 save(tmp_path/'plan.json',plan);save(tmp_path/'approval.json',a)
 with patch.object(e.time,'time',return_value=100),pytest.raises(ValueError,match='EXPLICIT_SHARED_EXPERIMENT_BUDGET'):
  e.run_experiment(tmp_path/'plan.json',tmp_path/'approval.json',None)


def test_paid_review_uses_separate_ledger_without_mutating_replay(store,tmp_path):
 snap=store.snapshot();p=r.prepare(store,snap['id'],include_prior=False);a=answer(p)
 before=read_bytes(store.path)
 ledger=Store(tmp_path/'ledger.sqlite3',dict(test='isolated-advisory'))
 c=dict(paid(store),ICT_AI_HISTORICAL_APPROVED='true')
 result=r.review(store,snap['id'],c,historical=True,ledger_store=ledger,include_prior=False,
  expected_packet_hash=digest(p),transport=lambda _:response(a),clock=lambda:snap['as_of'])
 assert result['status']=='VALIDATED_ADVISORY' and result['origin']=='INJECTED_TEST_TRANSPORT'
 assert read_bytes(store.path)==before
 assert ledger.connect().execute('SELECT count(*) FROM reasoning_transitions').fetchone()[0]==len(a['assessments'])
 plan=dict(id='fixture',cases=[dict(id=snap['id'],decision='NO_TRADE',baseline=dict(decision=snap['decision']))])
 metrics=e.export_blind(plan,ledger,tmp_path)
 items=read_json(tmp_path/'blind-rating.json')['items'];key=read_json(tmp_path/'private-arm-key.json')
 assert len(items)==2 and {x['variant'] for x in key.values()}=={'A_DETERMINISTIC','C_LLM_RAG'}
 assert metrics['baseline_cases']==1 and metrics['attempted']==1
 ratings=[dict(blind_id=x['blind_id'],rationale='UNIT_TEST_NOT_A_MARKET_RATING',unsupported_claims=None,**{k:None for k in e.RATING_CRITERIA}) for x in items]
 save(tmp_path/'ratings.json',ratings)
 assert e.score_ratings(tmp_path,tmp_path/'ratings.json')['paired_differences']['market_interpretation']==dict(pairs=0,mean_C_minus_B=None)
 save(tmp_path/'ratings.json',ratings[:1])
 with pytest.raises(ValueError,match='RATE_ALL'):e.score_ratings(tmp_path,tmp_path/'ratings.json')
