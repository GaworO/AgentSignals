#!/usr/bin/env python3
"""Send one authenticated broker lifecycle test event to AgentSignals v32.

Use a real plan_id copied from /guard after a manual test order. This script does not place an order.
It only tests the relay/broker -> guard callback path.
"""
from __future__ import annotations
import argparse
import json
import sys
import time
import requests


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--base-url', required=True, help='e.g. https://agentsignals.example.com')
    p.add_argument('--token', required=True, help='BROKER_EVENT_TOKEN')
    p.add_argument('--plan-id', required=True)
    p.add_argument('--order-id', default='callback-smoke-test')
    p.add_argument('--status', default='accepted', choices=[
        'submitted','accepted','working','partial','filled','closed','canceled','rejected','expired'])
    p.add_argument('--realized-pnl', type=float)
    p.add_argument('--avg-fill-price', type=float)
    p.add_argument('--exit-price', type=float)
    args = p.parse_args()

    body = {
        'plan_id': args.plan_id,
        'order_id': args.order_id,
        'status': args.status,
        'event_ms': int(time.time() * 1000),
        'provider': 'manual-smoke-test',
    }
    if args.realized_pnl is not None:
        body['realized_pnl'] = args.realized_pnl
    if args.avg_fill_price is not None:
        body['avg_fill_price'] = args.avg_fill_price
    if args.exit_price is not None:
        body['exit_price'] = args.exit_price

    url = args.base_url.rstrip('/') + '/guard/broker-event'
    r = requests.post(url, params={'t': args.token}, json=body, timeout=15)
    print('HTTP', r.status_code)
    try:
        print(json.dumps(r.json(), indent=2))
    except Exception:
        print(r.text)
    return 0 if r.ok else 1


if __name__ == '__main__':
    sys.exit(main())
