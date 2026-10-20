"""GET-only adapter for documented CrossTrade NT8 executable quote snapshots.

The TV account API has no quote feed. Collective/indicative pricing is rejected.
No price is ever synthesized from bars. Bars below measure volatility only.
"""
import json
import math
import os
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener

import ab_v3_live
from builder_broker_sync import NoRedirect


def normalize(raw, order, parameters, now_ms):
    contract = order['contract']
    if not re.fullmatch(r'MNQ[HMUZ]20\d{2}', contract):
        raise ValueError('quote_explicit_contract_required')
    month = {'H':'03','M':'06','U':'09','Z':'12'}[contract[3]]
    expected = 'MNQ '+month+'-'+contract[-2:]
    if raw.get('source') == 'collective' or raw.get('pricingBasis'):
        raise ValueError('indicative_collective_quote_not_executable')
    if (raw.get('instrument') != expected or raw.get('tickSize') != .25
            or raw.get('pointValue') != 2 or raw.get('type') != 'Future'):
        raise ValueError('quote_contract_metadata_mismatch')
    if (raw.get('dataSource') or {}).get('status') != 'Live':
        raise ValueError('verified_live_quote_source_required')
    at = raw.get('epoch')
    if type(at) not in (int,float) or not math.isfinite(at):
        raise ValueError('quote_provider_timestamp_required')
    at = int(at*1000)
    if not 0 <= now_ms-at <= parameters['max_quote_age_ms']:
        raise ValueError('quote_stale_or_future')
    bid, ask = raw.get('bid'), raw.get('ask')
    if any(type(v) not in (int,float) or not math.isfinite(v) or v <= 0 or v/.25 != round(v/.25) for v in (bid,ask)) or bid > ask:
        raise ValueError('quote_bid_ask_invalid')
    return dict(contract=contract,source='crosstrade_nt8',bid=bid,ask=ask,
                as_of_ms=at,market_open=raw.get('isMarketOpen') is True)


class MarketSource:
    def __init__(self, *, clock=time.time, reader=None):
        self.clock=clock
        self.reader=reader or self.read

    def read(self, contract):
        token=os.environ.get('DIRECTIONAL_QUOTE_API_TOKEN') or os.environ.get('BUILDER_BROKER_API_TOKEN')
        if not token or os.environ.get('DIRECTIONAL_QUOTE_SOURCE') != 'crosstrade_nt8':
            raise ValueError('executable_quote_access_not_configured')
        if not re.fullmatch(r'MNQ[HMUZ]20\d{2}',contract):
            raise ValueError('quote_explicit_contract_required')
        instrument='MNQ '+{'H':'03','M':'06','U':'09','Z':'12'}[contract[3]]+'-'+contract[-2:]
        url='https://app.crosstrade.io/v1/api/market/quote?'+urlencode(dict(instrument=instrument))
        try:
            with build_opener(NoRedirect()).open(Request(url,headers={'Authorization':'Bearer '+token}),timeout=5) as r:
                raw=r.read(65537)
                if len(raw)>65536:raise ValueError()
                return json.loads(raw)
        except Exception:
            raise ValueError('executable_quote_read_failed') from None

    def __call__(self, order):
        import execution_validation
        p=execution_validation.policy()
        if p.get('quote_source') != 'crosstrade_nt8':
            raise ValueError('quote_provider_not_supported')
        n=p.get('volatility_window_bars')
        if p.get('volatility_definition') != 'closed_m1_max_true_range' or type(n) is not int or not 1<=n<=1440:
            raise ValueError('validated_volatility_definition_required')
        now=int(self.clock()*1000);end=now//60000*60000
        with ab_v3_live.connect() as db:
            rows=db.execute("SELECT ts_ms,bar_json FROM bars WHERE tf='M1' AND contract=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms",
                            (order['contract'],end-(n+1)*60000,end)).fetchall()
        if [r['ts_ms'] for r in rows] != list(range(end-(n+1)*60000,end,60000)):
            raise ValueError('volatility_reference_candles_missing')
        bars=[json.loads(r['bar_json']) for r in rows]
        ranges=[max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close'])) for a,b in zip(bars,bars[1:])]
        result=normalize(self.reader(order['contract']),order,p,int(self.clock()*1000))
        if int(self.clock()*1000)//60000*60000 != end:
            raise ValueError('volatility_reference_rolled_during_read')
        return dict(result,volatility_points=max(ranges),volatility_definition=p['volatility_definition'])
