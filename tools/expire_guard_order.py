#!/usr/bin/env python3
"""Mark one verified stale AgentSignals plan as expired.

Run only after checking the broker has no open position and no working order.
This script does not place or cancel a broker order; it reconciles guard state.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import requests


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True, help="AgentSignals URL, e.g. https://agent.up.railway.app")
    parser.add_argument("--token", required=True, help="BROKER_EVENT_TOKEN")
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--signal-key")
    parser.add_argument("--reason", default="Operator verified no broker position or working order")
    args = parser.parse_args()

    body = {
        "plan_id": args.plan_id,
        "signal_key": args.signal_key,
        "status": "expired",
        "filled_quantity": 0,
        "remaining_quantity": 0,
        "provider": "manual-reconciliation",
        "reason": args.reason,
        "event_ms": int(time.time() * 1000),
    }
    url = args.base_url.rstrip("/") + "/guard/broker-event"
    response = requests.post(url, params={"t": args.token}, json=body, timeout=15)
    print("HTTP", response.status_code)
    try:
        print(json.dumps(response.json(), indent=2, ensure_ascii=False))
    except Exception:
        print(response.text)
    return 0 if response.ok else 1


if __name__ == "__main__":
    sys.exit(main())
