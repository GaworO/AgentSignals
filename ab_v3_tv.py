"""Authenticated TV minute batches -> V3 market-data store, never M1 detector.

Requires explicit, operator-verified TV-to-broker contract mapping. This is a
data bridge, NOT the missing Tradovate position connector.
"""
from datetime import datetime, timezone
import os
import ab_v3_live as live


def on_batch(body):
    if live.mode() == 'OFF':
        return dict(v3_data_bridge='OFF')
    symbol = str(body['symbol'])
    if ('!' in symbol or not live.contract() or '!' in live.contract()
            or os.environ.get('AB_V3_TV_MAPPING_VERIFIED','0') != '1'):
        return dict(v3_data_bridge='WAITING_EXPLICIT_CONTRACT_MAPPING')
    if symbol != os.environ.get('TV_1S_SYMBOL','').strip():
        raise ValueError('TV symbol changed')

    def bar(row):
        return dict(ts_event=datetime.fromtimestamp(row['ts_ms']/1000,timezone.utc).isoformat(),
                    **{k:row[k] for k in ('open','high','low','close','volume')})

    # M1 here is a manager-only reference. Existing TV /bars remains the only
    # detector intake. No duplicate archive append, entry scan or live fanout.
    live.ingest(dict(contract=live.contract(),tf='M1',bars=[bar(body['minute'])]),notify_m1=False)
    if body['bars']:
        live.ingest(dict(contract=live.contract(),tf='1s',bars=[bar(r) for r in body['bars']]),notify_m1=False)
    import threading
    threading.Thread(target=live._tick,daemon=True).start()
    return dict(v3_data_bridge='CONNECTED',v3_contract=live.contract())
