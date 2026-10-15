"""Inspect/capture CrossTrade or reconcile a broker export. Contains no order client."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from store import Store
from execution_ledger import ExecutionLedger
from crosstrade_capture import CrossTradeCapture


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('check','capture-once','report','import-fills','reconcile'))
    parser.add_argument('--statement',type=Path,help='Private normalized JSON: fills and reconciliation fields')
    parser.add_argument('--evidence',type=Path,help='Actual broker statement/export checked by the operator')
    parser.add_argument('--from-time',help='UTC window start for a report fingerprint')
    parser.add_argument('--through',help='UTC window end for a report fingerprint')
    args=parser.parse_args()
    config=dict(os.environ)
    ledger=ExecutionLedger(Store(config.get('DATA_DIR','/data')),config)
    now=time.time()
    if args.command=='check':
        print(json.dumps(dict(configured=ledger.configured,provider_selected=ledger.selected,
            capture_enabled=config.get('TANJA_CROSSTRADE_CAPTURE_ENABLED','false')=='true',
            api_token_present=bool(config.get('TANJA_CROSSTRADE_API_TOKEN')),orders_enabled=False)))
    elif args.command=='capture-once':
        capture=CrossTradeCapture(ledger,config)
        if not capture.enabled:raise ValueError('READ_ONLY_CAPTURE_NOT_ENABLED')
        ok=capture.poll(now)
        print(json.dumps(dict(captured=ok,accounting=ledger.state(now),orders_enabled=False)))
        return 0 if ok else 1
    elif args.command=='report':
        from execution_ledger import timestamp
        rows=ledger.records()
        if args.from_time:rows=[r for r in rows if r['at']>=timestamp(args.from_time)]
        if args.through:rows=[r for r in rows if r['at']<=timestamp(args.through)]
        print(json.dumps(dict(state=ledger.state(now),fills=rows,window_fingerprint=ledger.fingerprint(rows),cost_history=ledger.cost_history()),indent=2))
    elif args.command=='import-fills':
        if not args.statement:raise ValueError('NORMALIZED_STATEMENT_REQUIRED')
        document=json.loads(args.statement.read_text())
        ledger.ingest(document['fills'],now)
        print(json.dumps(dict(imported=True,accounting_still_provisional=True,orders_enabled=False)))
    else:
        if not args.statement or not args.evidence:raise ValueError('STATEMENT_AND_BROKER_EVIDENCE_REQUIRED')
        document=json.loads(args.statement.read_text())
        if not isinstance(document,dict) or set(document)!={'fills','reconciliation'}:raise ValueError('STATEMENT_IMPORT_SCHEMA_INVALID')
        digest=hashlib.sha256(args.evidence.read_bytes()).hexdigest()
        if document['reconciliation'].get('evidence_sha256')!=digest:raise ValueError('BROKER_EVIDENCE_HASH_MISMATCH')
        ledger.ingest(document['fills'],now)
        ledger.reconcile(document['reconciliation'],now)
        print(json.dumps(dict(reconciled_historical_window=True,orders_enabled=False,
            note='An offline statement does not establish continuing live account coverage.')))
    return 0


if __name__=='__main__':
    try:sys.exit(main())
    except Exception:
        # Never echo provider payloads, private paths, credentials or account IDs.
        print('Read-only capture/reconciliation failed; verify private configuration and broker evidence.',file=sys.stderr)
        sys.exit(1)
