"""Persist readiness of completed AI reviews. Observation only; no transport.

Reuses frozen evidence validation; never promotes valid JSON to strategy approval.
No additional AI calls, position simulation, broker signals or configuration keys.
"""
import json
import math
import time
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'vendor'))
from entry_review import validate_decision

STRATEGY_GAPS = [
    'Current service defaults to ES/NQ analysis and separate MNQ execution prices; retained historical MNQ reviews remain legacy records.',
    'Both user-approved entry families and optional retracement are implemented; discretionary quality still needs comparison with reviewed examples.',
    'Model-selected context, stop and target evidence are checked mechanically; matching Tanja’s choices is not established.',
]
AUTOMATION_GAPS = [
    'Current reported account limits and scheduled news must pass when the saved entry selection becomes a research plan.',
    'ATR continuation checks require a fresh measurement with an explicit calculation convention.',
    'Paper fills consume supplied quotes; broker positions, fills and stop amendments remain unconnected.',
]


def assess(row):
    base=dict(ai_run_id=row['id'],packet_id=row['packet_id'],market_cutoff=row['cutoff'],
              available_at=row['finished'],selected_trigger_id=None,state='AI_NOT_VALIDATED',
              reasons=[],measurements={},missing_fields=[],executable=False,
              semantic_fidelity_verified=False,simulated_entry=False,broker_orders_sent=0)
    if row['status']!='validated':
        base['reasons']=['The AI review failed, was rejected or was interrupted. It cannot supply a plan.']
        return base
    try:
        snapshot=json.loads(row['snapshot']);decision=json.loads(row['decision']);p=snapshot['packet']
        dates=[row['cutoff'],row['started'],row['finished'],snapshot['frozen_at'],snapshot['processed_at']]
        if not all(type(x) in (int,float) and math.isfinite(x) for x in dates):raise ValueError('Invalid time')
        if not row['cutoff']<=snapshot['frozen_at']<=snapshot['processed_at']<=row['started']<=row['finished']:
            raise ValueError('Noncausal review time')
        if p['packet_id']!=row['packet_id'] or p['as_of']!=row['cutoff']:raise ValueError('Snapshot identity mismatch')
        review=validate_decision(p,decision)
        context=decision['context']
        base['selected_trigger_id']=context['selected_trigger_id']
        base['measurements']=review['measurements']
        base['missing_fields']=review['missing_plan_fields']
        base['response_delay_seconds']=round(row['finished']-row['cutoff'],3)
        base['context_review_state']=review['context_review_state']
        if context['decision']=='abstain':
            base.update(state='ABSTAINED',reasons=['AI abstained; no entry proposed.'])
        elif review.get('context_watch_verified'):
            base.update(state='CONTEXT_WATCH_ARMED',direction=context['bias'],setup_profile=decision['entry_watch']['setup_profile'],
                reasons=['Saved context is armed. Each new inversion is checked against its explicit sequence, price and entry rules.'],
                expires_at=row['cutoff']+decision['entry_watch']['valid_for_seconds'])
        elif context['decision']!='candidate':
            base.update(state='CONTEXT_NOT_READY',reasons=['Context review did not propose an entry candidate.'])
        elif context['bias'] not in ('long','short') or decision['execution_symbol']!='MNQ':
            base.update(state='UNSUPPORTED_VARIANT',reasons=['A directional MNQ selection is required.'])
        elif decision['entry_mode']=='retracement' and not review.get('entry_setup_verified'):
            base.update(state='PLAN_INCOMPLETE',reasons=['This review does not supply a verified retracement price, lifetime and entry sequence.'])
        elif review['context_review_state']!='CONDITIONS_MET':
            base.update(state='CONTEXT_NOT_READY',reasons=['Selected context conditions are incomplete or not met.'])
        elif review['missing_plan_fields']:
            base.update(state='PLAN_INCOMPLETE',reasons=['AI proposal is missing required plan details.'])
        elif review.get('entry_setup_verified'):
            base.update(state='ENTRY_SELECTION_VERIFIED',reasons=['Entry sequence and selected prices passed. Current account, news, expiry and intervening-price checks still apply.'],
                direction=context['bias'],entry_mode=decision['entry_mode'],setup_profile=decision['entry_setup']['setup_profile'])
        else:
            base.update(state='RULE_REVIEW_REQUIRED',reasons=STRATEGY_GAPS+AUTOMATION_GAPS)
    except (ValueError,KeyError,TypeError,IndexError):
        base.update(state='AUDIT_REJECTED',reasons=['Stored review failed frozen-evidence or timestamp verification.'])
    return base


class PlanObserver:
    def __init__(self,store):
        self.store=store
        from transcript_ingestion import TranscriptJournal
        self.transcripts=TranscriptJournal(store)
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS plan_observations(
                ai_run_id TEXT PRIMARY KEY, available_at REAL NOT NULL,
                recorded_at REAL NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL)''')

    def process_one(self,now=None):
        now=time.time() if now is None else now
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('''SELECT a.* FROM ai_runs a
                LEFT JOIN plan_observations o ON o.ai_run_id=a.id
                WHERE o.ai_run_id IS NULL AND a.status IN ('validated','failed','rejected','interrupted')
                AND COALESCE(a.finished,a.started)<=?
                ORDER BY COALESCE(a.finished,a.started),a.id LIMIT 1''',(now,)).fetchone()
            if not row:return False
            row=dict(row)
            # Interrupted requests have no finished answer. Record only nondecision status.
            available=row['finished'] if row['finished'] is not None else now
            if row['status']=='validated' and row['finished'] is None:
                result=dict(ai_run_id=row['id'],packet_id=row['packet_id'],market_cutoff=row['cutoff'],
                    available_at=None,state='AUDIT_REJECTED',reasons=['Validated review has no completion timestamp.'],
                    executable=False,simulated_entry=False,broker_orders_sent=0,selected_trigger_id=None,missing_fields=[])
            else:result=assess(row)
            result['recorded_at']=now
            db.execute('INSERT INTO plan_observations VALUES(?,?,?,?,?)',
                (row['id'],available,now,result['state'],json.dumps(result,allow_nan=False)))
        return True

    def state(self):
        with self.store.connect() as db:
            records=[json.loads(r[0]) for r in db.execute('SELECT payload FROM plan_observations ORDER BY available_at DESC,ai_run_id LIMIT 100')]
        return dict(mode='OBSERVE_ONLY',records=records,executable=False,orders_enabled=False,
                    transcript_observations=self.transcripts.shadow(),
                    automatic_paper_entries=False,extra_ai_calls=0,strategy_gaps=STRATEGY_GAPS,automation_gaps=AUTOMATION_GAPS)

    def run(self,stop):
        while not stop.is_set():
            try:
                if self.process_one():continue
            except Exception:
                self.store.note('plan_observer','Plan observation could not be recorded; inspect service health.')
            stop.wait(10)
