"""Closed-bar feature observations, reusing the audited v3 research engine."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'vendor'))
from context_v3 import build_v3_packet
from tanya_replay import Bar


def process_one(store, now=None):
    job = store.claim_job()
    if not job:
        return False
    now = time.time() if now is None else now
    try:
        histories = {s:store.history(s, job['cutoff'], job['frozen_at']) for s in ('ES','MNQ')}
        markets = {s:[Bar(b['bar_open_ms']//1000, b['open'], b['high'], b['low'], b['close']) for b in rows]
                   for s,rows in histories.items()}
        packet = build_v3_packet(markets, job['cutoff'])
        gaps = {}
        for s,rows in histories.items():
            gaps[s] = [{'after':a['bar_close_ms']//1000,'before':b['bar_open_ms']//1000}
                       for a,b in zip(rows,rows[1:]) if a['bar_close_ms'] != b['bar_open_ms']][-30:]
        readiness = {s:packet['coverage'][s]['retained_bars_per_timeframe'] for s in markets}
        context = dict(packet=packet, frozen_at=job['frozen_at'], processed_at=now,
                       processing_delay_seconds=round(now-job['cutoff'],1),
                       gaps=gaps, coverage=readiness, ai_status='NOT_CONNECTED',
                       warmup='PARTIAL_HISTORY' if any(readiness[s]['240']<3 for s in markets) else 'HTF_BARS_PRESENT_NOT_STRATEGY_APPROVAL',
                       history_limit_per_market=12000,
                       note='Gaps include exchange closures. No candles are filled or interpolated. No automatic SMT anchor selection.')
        # Observations only: IFVG formations are NOT context-approved trades.
        candidates=[]
        for key,e in packet['evidence'].items():
            if e['kind']=='IFVG' and e['available_at']==job['cutoff'] and e['symbol']=='MNQ':
                candidates.append(dict(id=key, observed_at=job['frozen_at'], as_of=job['cutoff'],
                    direction=e['direction'], timeframe=e['timeframe'], lower=e['lower'], upper=e['upper'],
                    trigger_close=e['close'], packet_id=packet['packet_id'],
                    status='FEATURE_ONLY_NEEDS_CONTEXT', executable=False,
                    reason='Closed-bar inversion detected. Bias, POI, SMT selection, stop and management remain unapproved.'))
        store.finish(job, context, candidates, now)
    except Exception as e:
        # Error class only: never leak configuration or request contents to UI/logs.
        store.fail(job['cutoff'], type(e).__name__)
    return True


def run_worker(store, stop):
    store.recover()
    while not stop.is_set():
        try:
            if not process_one(store):
                stop.wait(1)
        except Exception:
            stop.wait(2)
