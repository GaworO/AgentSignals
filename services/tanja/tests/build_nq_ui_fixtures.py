"""Frozen synthetic NQ/MNQ UI audits; no API/model/broker calls."""
import argparse
import json
from pathlib import Path
import sys
sys.path[:0]=[str(Path(__file__).resolve().parent),str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'vendor')]
from test_nq_analysis import nq_fixture
from entry_patterns import PROFILES
from entry_review import compile_review,validate_decision

parser=argparse.ArgumentParser();parser.add_argument('output');args=parser.parse_args();fixtures=[]
for family in PROFILES:
    for direction in ('long','short'):
        for mode in ('immediate_after_confirmation','retracement'):
            p,d=nq_fixture(family,direction,mode);t=p['as_of'];identifier=family+'-'+direction+'-'+mode
            review=validate_decision(p,d);r=compile_review(p,d,selected_at=t+1,now=t+1,budget=250,cap=40)
            r.update(revision='synthetic',scheduled_news=dict(state='CLEAR',reasons=[]),news_entry_reasons=[],
                account_guard=dict(state='REPORTED_LIMITS_CLEAR',budget_usd=250,max_contracts=40),
                ai_context=dict(state='PERMITTED',packet_id=p['packet_id'],direction=direction,invalidation_symbol='NQ'))
            record=dict(id=identifier,packet_id=p['packet_id'],cutoff=t,started=t+1,finished=t+2,status='validated',decision=d,review=review)
            audit=dict(record,snapshot=dict(packet=p,frozen_at=t,processed_at=t))
            state=dict(feeds={s:dict(count=0) for s in ('ES','MNQ','NQ')},jobs=[],diagnostics=[],data_health=dict(level='ok'),
                market_roles=dict(analysis_inputs=['ES','NQ']),
                candidates=[dict(id='NQ:trigger',as_of=t,packet_id=p['packet_id'],direction=direction,timeframe=1,lower=106.25,upper=107.25)],
                automatic_selection=dict(enabled=True,configuration=dict(paper_max_contracts=40),records=[r],
                    scheduled_news=dict(state='CLEAR',reasons=[]),account_guard=r['account_guard']),
                ai=dict(status='DISABLED',records=[record],model='synthetic',calls_today=1,daily_limit=6,min_interval_minutes=15,max_output_tokens=8000,review_window='synthetic'),
                trading_loop=dict(status='BROKER_FEEDBACK_REQUIRED',reason='Broker feedback bridge missing.',mode='off',orders_enabled=False,feedback=dict(account_verified=False,loss_count=None)))
            fixtures.append(dict(state=state,audit=audit))
Path(args.output).write_text(json.dumps(fixtures))
