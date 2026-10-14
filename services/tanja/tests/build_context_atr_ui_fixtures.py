"""Synthetic saved-context/ATR dashboard fixtures, no network or market claims."""
import argparse,json
from pathlib import Path
import sys
sys.path[:0]=[str(Path(__file__).resolve().parent),str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'vendor')]
from test_context_watch import watch_fixture
from entry_patterns import PROFILES
from context_watch import evaluate

parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output');args=parser.parse_args();states=[]
for family in PROFILES:
    for direction in ('long','short'):
        for mode in ('immediate_after_confirmation','retracement'):
            row,m,t,atr=watch_fixture(family,direction,mode)
            atr.update(state='READY',range_to_atr=.4,reached=False,convention='5-session SMA true range; 18:00–17:00 New York')
            r=evaluate(row,m,cutoff=t,now=t+1,budget=250,cap=40,daily_atr=atr)
            r.update(revision='synthetic',scheduled_news=dict(state='CLEAR'),news_entry_reasons=[],
                account_guard=dict(state='REPORTED_LIMITS_CLEAR',budget_usd=250,max_contracts=40))
            states.append(dict(feeds={s:dict(count=0) for s in ('ES','MNQ','NQ')},jobs=[],diagnostics=[],data_health=dict(level='ok'),
                candidates=[dict(id=c['candidate_id'],as_of=c['cutoff'],packet_id=c['packet_id'],direction=c['direction'],
                    timeframe=c['timeframe'],lower=c['lower'],upper=c['upper']) for c in r['candidate_evaluations']],
                automatic_selection=dict(enabled=True,configuration=dict(paper_max_contracts=40),records=[r],daily_atr=atr,
                    scheduled_news=dict(state='CLEAR',reasons=[]),account_guard=r['account_guard'],sizing=dict(budget_usd=250,balance=50000,balance_as_of='synthetic')),
                ai=dict(status='DISABLED',records=[],model='synthetic',calls_today=0,daily_limit=6,min_interval_minutes=15,max_output_tokens=8000,review_window='synthetic')))
Path(args.output).write_text(json.dumps(states))
