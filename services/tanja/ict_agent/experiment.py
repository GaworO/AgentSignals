"""Prepare a frozen 15-case ablation. Execution is a separate explicit approval step."""
import argparse,json,math,os,sqlite3,time
from pathlib import Path
from .common import read_json,encode,digest,save
from .store import Store
from . import reasoning as r

MODEL='gpt-5.4-mini-2026-03-17'
RATES=dict(input_per_million=.75,output_per_million=4.50,
    source='https://developers.openai.com/api/docs/models/gpt-5.4-mini',checked_on='2026-11-20',
    basis='Standard USD; no cache/batch discount; excludes tax. Recheck before approval.')
ARMS=('B_LLM_NO_RAG','C_LLM_RAG')


def readonly(path):
    s=object.__new__(Store);s.path=Path(path).resolve();return s


def choose_cases(store,ambiguity):
    with store.readonly() as db:
        rows=[dict(id=x[0],as_of=x[1],decision=x[2]) for x in db.execute('SELECT id,as_of,decision FROM snapshots ORDER BY as_of,id')]
    chosen={};selected=[]
    # Retrospective category selection is kept OUTSIDE model inputs.
    times={x['plan']['available_at'] for x in ambiguity['cases']}
    groups=[('AMBIGUOUS_EXECUTION',[x for x in rows if x['as_of'] in times]),
            ('RESEARCH_CANDIDATE_NOT_SOURCE_VALIDATED',[x for x in rows if x['decision']=='TRADE_CANDIDATE' and x['as_of'] not in times]),
            ('INCOMPLETE',[x for x in rows if x['decision']=='WAIT_FOR_CONFIRMATION']),
            ('INVALIDATED',[x for x in rows if x['decision']=='CANCEL_SETUP']),
            ('NO_TRADE',[x for x in rows if x['decision']=='NO_TRADE'])]
    for category,pool in groups:
        available=[x for x in pool if x['id'] not in chosen]
        if len(available)<3:raise ValueError('INSUFFICIENT_REAL_CASES:'+category)
        for index in (0,len(available)//2,len(available)-1):
            item=dict(available[index],category=category)
            chosen[item['id']]=True;selected.append(item)
    if len(selected)!=15 or len(chosen)!=15:raise ValueError('COHORT_DUPLICATE')
    return sorted(selected,key=lambda x:(x['as_of'],x['id']))


def prepare_experiment(source,knowledge,ambiguity,destination,public_report=None):
    store=readonly(source);cases=choose_cases(store,ambiguity)
    plan=dict(version='ict-shadow-native-ablation-v1',model=MODEL,reasoning_effort='medium',
        reasoning_version=r.VERSION,prompt_sha256=digest(r.PROMPT),schema_sha256=digest(r.SCHEMA),
        knowledge_version=knowledge.version,index_version=knowledge.index_version,source_db=str(store.path),
        rates=RATES,max_output_tokens=12000,max_input_tokens_upper=400000,timeout_seconds=90,
        cases=cases,request_count=30,paid_authorized=False,orders_enabled=False,
        design='Fifteen independent retrospective snapshots; no prior model answer in either arm.',
        limitations=['Research-exposed archive, not untouched holdout.','Candidate validity needs independent source/market adjudication.',
                    'Ambiguity category uses later audit only for sampling; no later outcome enters model packets.',
                    'Partial blinding only: citations may reveal the RAG arm.','No profitability inference from this experiment.'])
    frozen={};total_upper=0;heuristic=0
    for case in cases:
        packets={arm:r.prepare(store,case['id'],variant=arm,knowledge=knowledge,include_prior=False) for arm in ARMS}
        if digest(packets[ARMS[0]]['evidence'])!=digest(packets[ARMS[1]]['evidence']):raise ValueError('UNMATCHED_MARKET_EVIDENCE')
        case['market_evidence_sha256']=digest(packets[ARMS[0]]['evidence']);case['requests']={}
        for arm,p in packets.items():
            body=r.request_body(p,MODEL,plan['max_output_tokens'],plan['reasoning_effort'])
            size=len(encode(body).encode());upper=size+4096
            if upper>plan['max_input_tokens_upper']:raise ValueError('INPUT_TOO_LARGE')
            reserve=(upper*RATES['input_per_million']+plan['max_output_tokens']*RATES['output_per_million'])/1e6
            total_upper+=reserve;heuristic+=(size/3.5*.75+4000*4.5)/1e6
            name=case['id']+'-'+arm+'.json';frozen[name]=p
            case['requests'][arm]=dict(packet=name,packet_sha256=digest(p),request_sha256=digest(body),input_upper=upper,
                reserve_usd=reserve,market_anchors=len(p['evidence']),sources=len(p['source_records']))
        snap=store.snapshot(case['id'])
        case['baseline']=dict(decision=snap['decision'],candidate_ids=snap['decision']['candidate_ids'])
        case['observable_checks']=dict(as_of=snap['as_of'],event_ids=[x['id'] for x in snap['events']],
            deterministic_missing=snap['decision']['data_missing'],source_validated_setup='NOT_ESTABLISHED')
        case['semantic_ground_truth']='AWAITING_INDEPENDENT_BLINDED_RATING'
    plan['cost']=dict(reserved_upper_usd=round(total_upper,6),suggested_total_cap_usd=math.ceil(total_upper),
        heuristic_usd=round(heuristic,2),heuristic_assumption='UTF-8 bytes / 3.5 input tokens and 4000 output tokens/call; not a tokenizer measurement.',
        paid_calls_made=0)
    plan['id']=digest(plan)[:20];root=Path(destination)/plan['id']
    if root.exists():raise ValueError('IMMUTABLE_EXPERIMENT_ALREADY_EXISTS')
    root.mkdir(parents=True)
    for name,p in frozen.items():save(root/name,p)
    save(root/'plan.json',plan)
    rubric=dict(plan_id=plan['id'],labels='Do not inspect arm mapping during qualitative scoring.',
        scale='0 incorrect, 1 mixed/uncertain, 2 correct; null if unratable.',
        criteria=['market_interpretation','missing_conditions','invalidation_detection','scenario_consistency','no_trade_quality'],
        unsupported_claims='Count after checking all prose, including numbers written as words; schema checks are insufficient.',
        grounding='Separate unblinded source audit against exact cited text; citations alone are not support.',
        ratings=[],measured_metrics=None,profitability_metrics=None)
    save(root/'rubric.json',rubric)
    save(root/'approval-template.json',dict(approved=False,experiment_id=plan['id'],plan_sha256=digest(plan),approval_id=None,model=MODEL,max_calls=30,max_usd=plan['cost']['suggested_total_cap_usd'],expires_at=None))
    if public_report:
        public=dict(plan);public['source_db']='FROZEN_LOCAL_REPLAY_71e60464703a3716'
        save(public_report,public)
    return root,plan


def approved_config(plan,approval,now):
    if digest({k:v for k,v in plan.items() if k!='id'})[:20]!=plan['id']:raise ValueError('PLAN_ID_HASH_MISMATCH')
    if (approval.get('approved') is not True or approval.get('experiment_id')!=plan['id'] or
        approval.get('plan_sha256')!=digest(plan) or not approval.get('approval_id') or
        approval.get('model')!=plan['model']):raise ValueError('EXPLICIT_MATCHING_EXPERIMENT_APPROVAL_REQUIRED')
    if approval.get('expires_at',0)<=now:raise ValueError('APPROVAL_EXPIRED')
    if approval.get('max_calls')!=30 or not plan['cost']['reserved_upper_usd']<=approval.get('max_usd',0)<=plan['cost']['suggested_total_cap_usd']:raise ValueError('APPROVAL_BUDGET_MISMATCH')
    return dict(ICT_AI_PAID_APPROVED='true',ICT_AI_HISTORICAL_APPROVED='true',ICT_AI_PROVIDER='openai',
        ICT_AI_APPROVAL_ID=approval['approval_id'],ICT_AI_APPROVAL_EXPIRES_AT=str(approval['expires_at']),ICT_AI_MODEL=plan['model'],
        ICT_AI_DAILY_CALLS='30',ICT_AI_DAILY_USD=str(approval['max_usd']),ICT_AI_INPUT_USD_PER_MILLION_CEILING=str(plan['rates']['input_per_million']),
        ICT_AI_OUTPUT_USD_PER_MILLION_CEILING=str(plan['rates']['output_per_million']),ICT_AI_MAX_OUTPUT_TOKENS=str(plan['max_output_tokens']),
        ICT_AI_MAX_INPUT_TOKENS_UPPER=str(plan['max_input_tokens_upper']),ICT_AI_TIMEOUT_SECONDS=str(plan['timeout_seconds']),
        ICT_AI_REASONING_EFFORT=plan['reasoning_effort'])


def run_experiment(plan_path,approval_path,knowledge):
    plan_path=Path(plan_path);plan=read_json(plan_path);approval=read_json(approval_path)
    cfg=approved_config(plan,approval,time.time())
    # One approval may cover today's separate journal and this historical arm.
    # Never create an independent eight-dollar allowance for this runner.
    if not approval.get('shared_budget_db') or approval.get('shared_total_usd')!=8:
        raise ValueError('EXPLICIT_SHARED_EXPERIMENT_BUDGET_REQUIRED')
    cfg.update(ICT_AI_TOTAL_BUDGET_DB=approval['shared_budget_db'],ICT_AI_TOTAL_USD=str(approval['shared_total_usd']))
    if not os.environ.get('ICT_AI_API_KEY'):raise ValueError('DEDICATED_PROVIDER_KEY_REQUIRED')
    cfg['ICT_AI_API_KEY']=os.environ['ICT_AI_API_KEY']
    if (r.VERSION,digest(r.PROMPT),digest(r.SCHEMA),knowledge.version,knowledge.index_version)!=(plan['reasoning_version'],plan['prompt_sha256'],plan['schema_sha256'],plan['knowledge_version'],plan['index_version']):raise ValueError('EXPERIMENT_VERSION_CHANGED')
    source=readonly(plan['source_db']);ledger=Store(plan_path.parent/'reasoning.sqlite3',dict(experiment=plan['id'],source=str(source.path),orders_enabled=False))
    r.initialize(ledger)
    # Verify every frozen packet against the current exact as-of source BEFORE spending.
    for case in plan['cases']:
        for arm in ARMS:
            expected=case['requests'][arm]
            if digest(read_json(plan_path.parent/expected['packet']))!=expected['packet_sha256']:raise ValueError('FROZEN_PACKET_HASH')
            current=r.prepare(source,case['id'],variant=arm,knowledge=knowledge,include_prior=False)
            if digest(current)!=expected['packet_sha256']:raise ValueError('MARKET_OR_RETRIEVAL_CHANGED')
            if digest(r.request_body(current,plan['model'],plan['max_output_tokens'],plan['reasoning_effort']))!=expected['request_sha256']:raise ValueError('REQUEST_BODY_CHANGED')
    for case in plan['cases']:
        for arm in ARMS:
            with ledger.readonly() as db:
                row=db.execute('SELECT status FROM reasoning_reviews WHERE snapshot_id=? AND variant=?',(case['id'],arm)).fetchone()
                total,count=db.execute('SELECT coalesce(sum(coalesce(actual_usd,reserved_usd)),0),count(*) FROM reasoning_budget').fetchone()
            if row:
                if row[0] in ('IN_FLIGHT','TRANSPORT_OUTCOME_UNKNOWN'):raise ValueError('RECONCILE_UNKNOWN_CALL_NO_RETRY')
                continue
            if count>=approval['max_calls'] or total+case['requests'][arm]['reserve_usd']>approval['max_usd']:raise ValueError('TOTAL_EXPERIMENT_BUDGET')
            result=r.review(source,case['id'],cfg,variant=arm,knowledge=knowledge,ledger_store=ledger,historical=True,
                include_prior=False,expected_packet_hash=case['requests'][arm]['packet_sha256'])
            if result['status']=='TRANSPORT_OUTCOME_UNKNOWN':raise ValueError('RECONCILE_UNKNOWN_CALL_NO_RETRY')
    return export_blind(plan,ledger,plan_path.parent)


def export_blind(plan,ledger,root):
    with ledger.readonly() as db:rows=[json.loads(x[0]) for x in db.execute('SELECT payload FROM reasoning_reviews ORDER BY snapshot_id,variant')]
    mapping={};items=[]
    for case in plan['cases']:
        blind=digest([plan['id'],case['id'],'A_DETERMINISTIC','rating'])[:16]
        mapping[blind]=dict(snapshot_id=case['id'],variant='A_DETERMINISTIC')
        items.append(dict(blind_id=blind,case_id=case['id'],answer=case['baseline']['decision'],status='RECORDED_BASELINE'))
    for row in rows:
        blind=digest([plan['id'],row['snapshot_id'],row['variant'],'rating'])[:16]
        mapping[blind]=dict(snapshot_id=row['snapshot_id'],variant=row['variant'])
        items.append(dict(blind_id=blind,case_id=row['snapshot_id'],answer=row.get('answer'),status=row['status']))
    save(root/'blind-rating.json',dict(items=sorted(items,key=lambda x:x['blind_id']),rubric='rubric.json',partial_blinding=True))
    save(root/'private-arm-key.json',mapping)
    metrics=dict(completed=sum(x['status']=='VALIDATED_ADVISORY' for x in rows),attempted=len(rows),
        semantic_quality='NOT_YET_RATED',profitability='NOT_MEASURED',baseline_cases=len(plan['cases']),
        baseline_decisions=[dict(case_id=c['id'],decision=c['decision'],latency_ms=None,api_usd=0) for c in plan['cases']],
        decisions=[dict(case_id=x['snapshot_id'],variant=x['variant'],decision=(x.get('answer') or {}).get('decision'),
            status=x['status'],latency_ms=x.get('elapsed_ms'),cost_ceiling_usd=x.get('estimated_usd_ceiling')) for x in rows])
    save(root/'metrics.json',metrics);return metrics


RATING_CRITERIA=('market_interpretation','missing_conditions','invalidation_detection','scenario_consistency','no_trade_quality','source_grounding')

def score_ratings(root,ratings_path):
    """Human semantic labels, separate from mechanical reference validation."""
    root=Path(root);blind=read_json(root/'blind-rating.json');mapping=read_json(root/'private-arm-key.json');ratings=read_json(ratings_path)
    ids={i['blind_id'] for i in blind['items']}
    if len(ratings)!=len(ids) or {x['blind_id'] for x in ratings}!=ids:raise ValueError('RATE_ALL_ITEMS_OR_EXPLICIT_NULL')
    grouped={};paired={}
    for rating in ratings:
        if not rating.get('rationale'):raise ValueError('RATING_RATIONALE_REQUIRED')
        for criterion in RATING_CRITERIA:
            value=rating.get(criterion)
            if value is not None and (type(value) is not int or value not in (0,1,2)):raise ValueError('RATING_SCALE')
        count=rating.get('unsupported_claims')
        if count is not None and (type(count) is not int or count<0):raise ValueError('UNSUPPORTED_COUNT')
        key=mapping[rating['blind_id']];grouped.setdefault(key['variant'],[]).append(rating)
        paired.setdefault(key['snapshot_id'],{})[key['variant']]=rating
    summary={}
    for arm,rows in grouped.items():
        summary[arm]={}
        for criterion in (*RATING_CRITERIA,'unsupported_claims'):
            values=[x[criterion] for x in rows if x.get(criterion) is not None]
            summary[arm][criterion]=dict(rated=len(values),unratable=len(rows)-len(values),mean=sum(values)/len(values) if values else None)
    differences={}
    for criterion in RATING_CRITERIA:
        values=[x[ARMS[1]][criterion]-x[ARMS[0]][criterion] for x in paired.values() if all(x.get(a,{}).get(criterion) is not None for a in ARMS)]
        differences[criterion]=dict(pairs=len(values),mean_C_minus_B=sum(values)/len(values) if values else None)
    result=dict(arms=summary,paired_differences=differences,rating_sha256=digest(ratings),
        interpretation='Human-labelled exploratory quality; no statistical superiority or profitability inference.',partial_blinding=True)
    save(root/'rated-metrics.json',result);return result


def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare','run','score']);p.add_argument('--source');p.add_argument('--assets')
    p.add_argument('--ambiguities');p.add_argument('--destination');p.add_argument('--public-report');p.add_argument('--plan');p.add_argument('--approval');p.add_argument('--ratings')
    a=p.parse_args()
    if a.command=='score':
        print(encode(score_ratings(Path(a.plan).parent,a.ratings)));return
    if not a.assets:raise ValueError('VERIFIED_ASSET_RELEASE_REQUIRED')
    from .assets import verify
    from .knowledge import Knowledge
    release=Path(a.assets).resolve();verify(release);knowledge=Knowledge(corpus=release/'corpus',atlas=release/'atlas')
    if a.command=='prepare':
        root,plan=prepare_experiment(a.source,knowledge,read_json(a.ambiguities),a.destination,a.public_report);print(encode(dict(path=str(root),id=plan['id'],cost=plan['cost'],calls=plan['request_count'],paid_calls=0)))
    else:
        if not a.approval:raise ValueError('EXPLICIT_APPROVAL_FILE_REQUIRED')
        print(encode(run_experiment(a.plan,a.approval,knowledge)))
if __name__=='__main__':main()
