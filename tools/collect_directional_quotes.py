"""GET-only quote study. Does not load a broker transport or alter execution flags.

python tools/collect_directional_quotes.py --samples 30 --interval 2 --output /data/quote-study.json
Operational sampling bounds are not approved trading thresholds.
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from directional_market_data import QuoteClient


def summary(samples):
    valid=[r for r in samples if r.get('quote')]
    def stats(key):
        values=sorted(r['quote'][key] for r in valid)
        return None if not values else dict(n=len(values),min=values[0],median=statistics.median(values),
              p95=values[min(len(values)-1,int(.95*len(values)))],max=values[-1])
    latencies=[r['request_latency_ms'] for r in samples]
    movement=[]
    for a in valid:
        b=next((b for b in valid if 50_000<=b['observed_ms']-a['observed_ms']<=55_000),None)
        if b:movement.append(dict(elapsed_ms=b['observed_ms']-a['observed_ms'],
            ask_change_points=b['quote']['ask']-a['quote']['ask'],bid_change_points=b['quote']['bid']-a['quote']['bid']))
    return dict(samples=len(samples),valid_observations=len(valid),
        spread_ticks=stats('spread_ticks'),reported_timestamp_age_ms=stats('quote_age_ms'),
        request_latency_ms=None if not latencies else dict(min=min(latencies),median=statistics.median(latencies),max=max(latencies)),
        movement_over_50_seconds=movement,source_verified=bool(valid) and all(r['quote']['execution_eligible'] for r in valid),
        execution_parameters_validated=False,slippage_estimated=False,
        limitations=['Observation sample, not executable-source certification or session-wide validation.',
                    'Price change is not slippage; no fills or queue-depth evidence.',
                    '50-second windows are timing scenarios, not actual AI review/fill pairs.'])


def main():
    p=argparse.ArgumentParser();p.add_argument('--contract',default='MNQZ2026')
    p.add_argument('--samples',type=int,default=30);p.add_argument('--interval',type=float,default=2)
    p.add_argument('--output',required=True);a=p.parse_args()
    if not 1<=a.samples<=60 or not 2<=a.interval<=60:p.error('samples 1..60, interval 2..60')
    c=QuoteClient();rows=[]
    for i in range(a.samples):
        rows.append(c.fetch(a.contract))
        if i+1<a.samples:time.sleep(a.interval)
    result=dict(contract=a.contract,summary=summary(rows),samples=rows)
    Path(a.output).write_text(json.dumps(result,indent=2,allow_nan=False))
    print(json.dumps(result['summary']))

if __name__=='__main__':main()
