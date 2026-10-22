"""Recompute saved, comparable manager ledgers; no replay, fitting or AI calls.

python tools/phase3a_research.py RESEARCH_DIRECTORY OUTPUT_JSON
The source is research_v3_boundary_runner_shadow_20261006, not live fills.
"""
import csv
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys


def metrics(rows):
    pnl=[float(r['pnl_usd']) for r in rows]
    profit=sum(max(0,x) for x in pnl);loss=-sum(min(0,x) for x in pnl)
    cashflows=defaultdict(float)
    for r in rows:cashflows[int(r['exit_ms'])]+=float(r['pnl_usd'])
    equity=peak=drawdown=0.
    for t in sorted(cashflows):
        equity+=cashflows[t];peak=max(peak,equity);drawdown=max(drawdown,peak-equity)
    intervals=defaultdict(int)
    for r in rows:
        intervals[int(r['fill_ms'])]+=1;intervals[int(r['exit_ms'])]-=1
    active=maximum=0
    for t in sorted(intervals):active+=intervals[t];maximum=max(maximum,active)
    return dict(trades=len(rows),net_pf=profit/loss if loss else None,
        pf_undefined_reason='no_losses' if not loss else None,net_usd=sum(pnl),
        win_rate=sum(x>0 for x in pnl)/len(pnl),expectancy_usd=sum(pnl)/len(pnl),
        expectancy_net_r=sum(float(r['net_r']) for r in rows)/len(rows),
        closed_trade_drawdown_usd=drawdown,max_overlapping_positions=maximum,
        first_fill_utc=datetime.fromtimestamp(min(int(r['fill_ms']) for r in rows)/1000,timezone.utc).isoformat(),
        last_exit_utc=datetime.fromtimestamp(max(int(r['exit_ms']) for r in rows)/1000,timezone.utc).isoformat(),
        recorded_open_m1_gap_trades=sum(float(r.get('m1_gap_minutes_while_open') or 0)>0 for r in rows),
        recorded_same_minute_sl_tp=sum(r.get('same_minute_sl_tp')=='True' for r in rows),
        changed_from_original=sum(r.get('changed')=='True' for r in rows))


def run(root):
    root=Path(root);result=dict(kind='RECOMPUTED_ARCHIVED_RESEARCH_NOT_LIVE',sources={},periods={})
    for period in ('OLDER_580','LATER_ARCHIVE58'):
        path=root/period/'POLICY_LEDGER.csv'
        result['sources'][str(path.relative_to(root))]=hashlib.sha256(path.read_bytes()).hexdigest()
        groups=defaultdict(list)
        for r in csv.DictReader(path.open()):
            assert r['filled']=='True' and r['status']=='CLOSED'
            groups[r['variant']].append(r)
        geometry=lambda r:tuple(r[k] for k in ('order_id','direction','entry','sl','tp','qty','fill_ms'))
        cohort={geometry(r) for r in groups['ORIGINAL_V3']}
        assert len(cohort)==len(groups['ORIGINAL_V3'])
        for rows in groups.values():
            assert len(rows)==len(cohort) and {geometry(r) for r in rows}==cohort
        result['periods'][period]={name:metrics(rows) for name,rows in groups.items()}
    for name in ('PROTOCOL','TRAINING_SEPARATION','CAUSAL_CHECKS','VERIFICATION'):
        path=root/(name+'.json')
        result['sources'][path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
        result[name.lower()]=json.loads(path.read_text())
    result['not_measured']={
        'partial_take_profit':'No matched causal outcome ledger with defined integer sizing, costs and bracket resizing.',
        'structural_trailing':'No matched causal outcome ledger with a fixed confirmed-swing rule.'}
    result['limitations']=[
        'No new candle replay: metrics recomputed from saved per-trade outcomes.',
        '2019–2022 cohort was evaluated retrospectively with a model trained later; not a forward deployment test.',
        'Samples were inspected previously; no fresh untouched holdout. Later cohort contains ONE filled trade.',
        'Original historical costs: USD 3.50 round trip per contract; not current broker fee evidence.',
        'First-2R-touch selective runner is idealized at M1 resolution; target replacement latency/OCO races absent.',
        'Fixed entries and quantities can overlap when exits change; no portfolio admission/risk replay.',
        'Drawdown uses realized exit cashflows from zero, not mark-to-market, MFFU trailing drawdown or intratrade equity.',
        'The compared historical Boundary/Model0 policy is not the new research-option manager or the hybrid AI reviewer.']
    return result


if __name__=='__main__':
    output=Path(sys.argv[2]);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(run(sys.argv[1]),indent=2,allow_nan=False)+'\n')
    print(output)
