#!/usr/bin/env python3
"""
fx_feeder.py — the live bar feed for the forex services.

Polls OANDA v20 for the latest COMPLETED 1-minute (bid) candle of each pair and POSTs it to that pair's
service /bars. One feeder feeds all three pairs. Free: an OANDA *practice* (demo) account gives an API
token in minutes with no funding.

DEPLOY as its OWN Railway service (a worker — no volume, no web port):
    Root Directory : repo root
    Start Command  : python3 forex/fx_feeder.py
    (needs `requests`, already in requirements.txt)

ENV (set on the feeder service only):
    OANDA_TOKEN    your token  (OANDA site > Manage API Access > Generate)
    OANDA_ENV      practice   (default) | live
    EUR_BARS_URL   https://forex-eur-...up.railway.app/bars     (omit a pair to skip it)
    GBP_BARS_URL   https://forex-gbp-...up.railway.app/bars
    JPY_BARS_URL   https://forex-jpy-...up.railway.app/bars
    POLL_SEC       seconds between polls (default 20)

Notes: forex is closed on weekends -> no new candles -> feeder simply posts nothing until Monday.
Sends RAW prices (~1.07 / 1.27 / 155); det_forex scales internally. /bars needs no secret.
"""
import os, time, datetime as dt
import requests

TOKEN = os.environ['OANDA_TOKEN']
BASE  = 'https://api-fxpractice.oanda.com' if os.environ.get('OANDA_ENV', 'practice') != 'live' \
        else 'https://api-fxtrade.oanda.com'
POLL  = int(os.environ.get('POLL_SEC', '20'))
PAIRS = [('EUR_USD', os.environ.get('EUR_BARS_URL', '')),
         ('GBP_USD', os.environ.get('GBP_BARS_URL', '')),
         ('USD_JPY', os.environ.get('JPY_BARS_URL', ''))]
PAIRS = [(inst, url) for inst, url in PAIRS if url]
HDR   = {'Authorization': 'Bearer ' + TOKEN}
_last = {}

def latest_complete(inst):
    r = requests.get(f"{BASE}/v3/instruments/{inst}/candles",
                     params={'granularity': 'M1', 'count': '3', 'price': 'B'}, headers=HDR, timeout=15)
    r.raise_for_status()
    done = [c for c in r.json().get('candles', []) if c.get('complete')]
    if not done:
        return None
    c = done[-1]; b = c['bid']
    ts = c['time'][:19].replace('T', ' ') + '+00:00'          # RFC3339 -> "YYYY-MM-DD HH:MM:SS+00:00"
    return dict(ts_event=ts, open=float(b['o']), high=float(b['h']),
                low=float(b['l']), close=float(b['c']), volume=int(c.get('volume', 0)))

def main():
    if not PAIRS:
        raise SystemExit("set at least one of EUR_BARS_URL / GBP_BARS_URL / JPY_BARS_URL")
    print(f"[fx_feeder] {BASE}  pairs={[i for i,_ in PAIRS]}  poll={POLL}s", flush=True)
    while True:
        for inst, url in PAIRS:
            try:
                bar = latest_complete(inst)
                if bar and bar['ts_event'] != _last.get(inst):
                    resp = requests.post(url, json=bar, timeout=15)
                    print(f"[fx_feeder] {inst} {bar['ts_event']} c={bar['close']} -> {resp.status_code}", flush=True)
                    _last[inst] = bar['ts_event']
            except Exception as e:
                print(f"[fx_feeder] {inst} ERR {e}", flush=True)
        time.sleep(POLL)

if __name__ == '__main__':
    main()
