from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import tempfile
import unittest
from store import Store
from market_readiness import contract_map,MarketReadiness
from test_daily_atr import history

TICKERS={s:'CME_MINI:'+s+'1!' for s in ('ES','NQ','MNQ')}


def mapping(now):
    return dict(TANJA_EXECUTION_CONTRACT='MNQZ2026',TANJA_FEED_CONTRACT_MAP=json.dumps({
        s:dict(ticker=TICKERS[s],contract=s+'Z2026',valid_from=now-86400*30,
            valid_until=now+86400,back_adjusted=False,source='Synthetic operator attestation')
        for s in TICKERS}))


class MarketReadinessTests(unittest.TestCase):
    def test_continuous_chart_does_not_prove_broker_contract(self):
        r=contract_map({},TICKERS,1791466860)
        self.assertFalse(r['ready']);self.assertFalse(r['exchange_verified'])
        self.assertIn('MNQ_CONTRACT_MAPPING_REQUIRED',r['reasons'])

    def test_roll_expiry_adjustment_and_explicit_contract_conflicts_block(self):
        now=1791466860;config=mapping(now)
        self.assertTrue(contract_map(config,TICKERS,now)['ready'])
        for cause in ('expiry','quarter','adjusted','explicit','execution'):
            c=deepcopy(config);m=json.loads(c['TANJA_FEED_CONTRACT_MAP']);tickers=dict(TICKERS)
            if cause=='expiry':m['NQ']['valid_until']=now
            elif cause=='quarter':m['NQ']['contract']='NQH2027'
            elif cause=='adjusted':m['MNQ']['back_adjusted']=True
            elif cause=='explicit':tickers['MNQ']=m['MNQ']['ticker']='CME_MINI:MNQH2027'
            else:c['TANJA_EXECUTION_CONTRACT']='MNQH2027'
            c['TANJA_FEED_CONTRACT_MAP']=json.dumps(m)
            self.assertFalse(contract_map(c,tickers,now)['ready'],cause)

    def test_fresh_complete_feeds_atr_and_contracts_allow_only_quote_check(self):
        t=int(datetime(2026,10,8,10,0,tzinfo=ZoneInfo('America/New_York')).timestamp());rows=history(t)
        with tempfile.TemporaryDirectory() as root:
            store=Store(root,analysis_symbol='NQ');store.bind_tickers(TICKERS)
            with store.connect() as db:
                for symbol in TICKERS:
                    table='collection_bars' if symbol=='NQ' else 'bars'
                    for row in rows:
                        at=row['time'];b=dict(symbol=symbol,ticker=TICKERS[symbol],bar_open_ms=at*1000,
                            bar_close_ms=(at+60)*1000,**{k:row[k] for k in ('open','high','low','close')})
                        db.execute(f'INSERT INTO {table} VALUES(?,?,?,?,?)',(symbol,at,at+60,at+60,json.dumps(b)))
            ready=MarketReadiness(store,mapping(t),TICKERS)
            r=ready.state(t+1)
            self.assertEqual(r['state'],'READY_FOR_QUOTE_CHECK',r['entry_reasons'])
            self.assertTrue(r['daily_atr']['complete']);self.assertFalse(r['orders_enabled'])
            quotes={s:dict(data=dict(contract=s+'Z2026')) for s in TICKERS}
            self.assertEqual(ready.quote_reasons(quotes,t+1),[])
            quotes['NQ']['data']['contract']='NQH2027'
            self.assertIn('NQ_QUOTE_CONTRACT_MAPPING_MISMATCH',ready.quote_reasons(quotes,t+1))
            # Delayed NQ and a missing MNQ minute are separate, visible blockers.
            with store.connect() as db:
                db.execute("DELETE FROM collection_bars WHERE symbol='NQ' AND end>?",(t-180,))
                db.execute("DELETE FROM bars WHERE symbol='MNQ' AND start=?",(t-600,))
            r=ready.state(t+4)
            self.assertIn('NQ_FEED_STALE_OR_FUTURE',r['entry_reasons'])
            self.assertIn('MNQ_RECENT_SEQUENCE_HISTORY_GAP',r['entry_reasons'])
            self.assertFalse(r['daily_atr']['complete'])


if __name__=='__main__':unittest.main()
