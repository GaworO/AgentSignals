"""Frozen matched-cohort protocol. Paid arms are NOT evaluated by preparation."""
import json
from pathlib import Path
from .common import digest,save
from .store import Store
from .reasoning import prepare

def build(run_folder,destination):
    folder=Path(run_folder);s=object.__new__(Store);s.path=folder/'observer.sqlite3'
    with s.readonly() as db:
        cohort=[dict(id=r[0],as_of=r[1],deterministic_decision=r[2]) for r in db.execute('SELECT id,as_of,decision FROM snapshots ORDER BY as_of')]
    report=dict(version='matched-ict-ablation-v1',run_id=folder.name,cohort_hash=digest(cohort),cohort=cohort,
        selection='ALL_SNAPSHOTS_IN_CHRONOLOGICAL_ORDER_INCLUDING_WAIT_AND_NO_TRADE',
        arms=dict(A_DETERMINISTIC='RECORDED',B_LLM_NO_RAG='NOT_RUN_NO_PAID_AUTHORIZATION',C_LLM_RAG='NOT_RUN_NO_PAID_AUTHORIZATION'),
        split=json.loads((folder/'split-plan.json').read_text()),
        invariant='Identical snapshot, numerical market evidence, execution assumptions and cost schedule in all arms; B removes all source records/audit notes.',
        model='EXPLICIT_MODEL_REQUIRED',budget='EXPLICIT_DATED_AUTHORIZATION_REQUIRED',orders_sent=0,
        measures={k:None for k in ('valid_observations','scenario_consistency','invalidation_detection','rejected_trades','candidate_quality','source_grounding_accuracy','win_rate','profit_factor','net_expectancy','max_drawdown','slippage','execution_ambiguity','latency','by_session','by_model_family')},
        empirical_comparison_complete=False,untouched_holdout_available=False,
        semantic_judgments='Require independently labelled as-of cases; schema validity is not a semantic score.',
        performance='Missing fees, ticks, quote latency and unresolved fills block financial conclusions; not filled with zeros.')
    save(destination,report);return report

def matched_packets(store,snapshot_id):
    b=prepare(store,snapshot_id,variant='B_LLM_NO_RAG');c=prepare(store,snapshot_id,variant='C_LLM_RAG')
    if digest(b['evidence'])!=digest(c['evidence']):raise ValueError('UNMATCHED_MARKET_EVIDENCE')
    return b,c
