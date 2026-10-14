"""Export synthetic dashboard fixtures; run from services/tanja, no network."""
import argparse
import json
from pathlib import Path
import sys
sys.path[:0]=[str(Path(__file__).resolve().parent),str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'vendor')]
from entry_sequence_fixture import fixture,decision_fixture
from entry_patterns import PROFILES

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('output')
args=parser.parse_args()
states=[]
for profile in PROFILES:
    for direction in ('long','short'):
        for mode in ('immediate_after_confirmation','retracement'):
            p,raw=fixture(profile,direction,mode);d,t,_=decision_fixture(profile,direction,mode)
            d['ai_context'].update(packet_id=p['packet_id'],ai_run_id='synthetic-review',source_cutoff=t,available_at=t+1,
                expires_at=t+180,reasons=[],explanation='Synthetic reviewed sequence',invalidation_price=d['compiled']['plan']['invalidation']['price'])
            d['revision']='synthetic';d.pop('packet')
            states.append(dict(feeds={s:dict(count=0) for s in ('ES','MNQ','NQ')},jobs=[],diagnostics=[],data_health=dict(level='ok'),
                candidates=[dict(id='trigger',packet_id=p['packet_id'],as_of=t,direction=direction,timeframe=1,
                    lower=p['evidence']['trigger']['lower'],upper=p['evidence']['trigger']['upper'])],
                automatic_selection=dict(enabled=True,configuration=dict(paper_max_contracts=40),records=[d],
                    scheduled_news=dict(state='CLEAR',reasons=[]),account_guard=d['account_guard'],
                    sizing=dict(budget_usd=250,balance=50000,balance_as_of='Synthetic fixture')),
                ai=dict(status='DISABLED',model='synthetic',calls_today=0,daily_limit=6,min_interval_minutes=15,max_output_tokens=8000,review_window='Synthetic test',
                    records=[dict(id='synthetic-review',packet_id=p['packet_id'],cutoff=t,finished=t+1,available_at=t+1,status='validated',decision=raw,review=dict(missing_plan_fields=[]))]),
                fixture_audit=dict(snapshot=dict(packet=p),decision=raw,cutoff=t,finished=t+1,status='validated',review=dict(measurements={}))))
Path(args.output).write_text(json.dumps(states))
