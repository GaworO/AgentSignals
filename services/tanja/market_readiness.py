"""Read-only checks for three distinct feeds and explicit contract provenance.

Continuous candles cannot prove which tradable contract or adjustment setting
they represent. A dated, sourced operator mapping is required for execution;
it is an attestation, not an exchange verification. No prices are translated.
"""
from copy import deepcopy
import json
import re
import time
from daily_atr import calculate,Policy,expected_minutes,trade_date
from store import bar_table

VERSION='es-nq-mnq-market-readiness-v1'


def contract_map(config,tickers,now):
    reasons=[];result={};raw=config.get('TANJA_FEED_CONTRACT_MAP','{}')
    try:
        mapping=json.loads(raw)
        if not isinstance(mapping,dict):raise ValueError('Expected mapping')
        for symbol in ('ES','NQ','MNQ'):
            item=mapping.get(symbol)
            if not isinstance(item,dict):
                reasons.append(symbol+'_CONTRACT_MAPPING_REQUIRED');continue
            if (item.get('ticker')!=tickers[symbol]
                or not re.fullmatch(symbol+r'[HMUZ]\d{4}',item.get('contract',''))
                or item.get('back_adjusted') is not False
                or not isinstance(item.get('source'),str) or not item['source'].strip()
                or type(item.get('valid_from')) is not int or type(item.get('valid_until')) is not int
                or not 0<item['valid_from']<=now<item['valid_until']):
                reasons.append(symbol+'_CONTRACT_MAPPING_INVALID_OR_EXPIRED');continue
            explicit=tickers[symbol].split(':')[-1]
            if '!' not in explicit and explicit!=item['contract']:
                reasons.append(symbol+'_EXPLICIT_TICKER_CONTRACT_CONFLICT');continue
            result[symbol]=dict(item)
        if 'NQ' in result and 'MNQ' in result and result['NQ']['contract'][2:]!=result['MNQ']['contract'][3:]:
            reasons.append('NQ_MNQ_CONTRACT_MONTH_MISMATCH')
        execution=config.get('TANJA_EXECUTION_CONTRACT','')
        if not execution or 'MNQ' not in result or result['MNQ']['contract']!=execution:
            reasons.append('MNQ_EXECUTION_CONTRACT_MAPPING_REQUIRED')
    except (ValueError,TypeError,KeyError):reasons.append('CONTRACT_MAPPING_SCHEMA_INVALID')
    return dict(state='OPERATOR_ATTESTED_CONTRACT_MAP' if not reasons else 'CONTRACT_MAP_REQUIRED',
        ready=not reasons,reasons=reasons,bindings=result,exchange_verified=False,
        note='Dated operator attestation of unadjusted chart/contract alignment; no inferred basis offset.')


class MarketReadiness:
    def __init__(self,store,config,tickers):
        self.store=store;self.config=dict(config);self.tickers=dict(tickers)
        self.atr_policy=Policy.from_config(config)
        self.cached=None

    def state(self,now=None):
        now=time.time() if now is None else now
        if self.cached is not None and self.cached['checked_at']<=now<self.cached['checked_at']+2:
            return deepcopy(self.cached)
        feeds={};histories={};bar_reasons=[];latest=[]
        with self.store.connect() as db:
            db.execute('BEGIN')
            for symbol in ('ES','NQ','MNQ'):
                rows=db.execute(f'SELECT payload,received,end FROM {bar_table(symbol)} WHERE symbol=? AND received<=? ORDER BY start DESC LIMIT 12000',(symbol,now)).fetchall()
                bars=[]
                for row in reversed(rows):
                    b=json.loads(row['payload']);b['received_at']=row['received'];bars.append(b)
                histories[symbol]=bars
                end=rows[0]['end'] if rows else None
                feeds[symbol]=dict(ticker=self.tickers[symbol],latest_close=end,
                    age_seconds=round(now-end,1) if end else None,stored_minutes=len(rows))
                if end is None:bar_reasons.append(symbol+'_FEED_MISSING')
                elif not 0<=now-end<=120:bar_reasons.append(symbol+'_FEED_STALE_OR_FUTURE')
                if end is not None:latest.append(end)
        cutoff=min(latest) if len(latest)==3 else None
        if cutoff is not None and len(set(latest))>1:bar_reasons.append('THREE_FEEDS_NOT_AT_SAME_MINUTE')
        atr=None
        if cutoff is not None:
            cutoff=int(cutoff)
            for symbol,bars in histories.items():
                times={b['bar_open_ms']//1000 for b in bars if b['bar_close_ms']//1000<=cutoff}
                # Only documented session minutes are expected; never interpolate.
                days={trade_date(t,self.atr_policy) for t in range(cutoff-10800,cutoff,60)}
                expected={t for day in days for t in expected_minutes(day,self.atr_policy,cutoff) if cutoff-10800<=t<cutoff}
                missing=sorted(expected-times)
                feeds[symbol].update(recent_missing_minutes=len(missing),first_missing=missing[0] if missing else None)
                if missing:bar_reasons.append(symbol+'_RECENT_SEQUENCE_HISTORY_GAP')
                from tanya_replay import Bar
                from context_layer import session_aggregate
                raw=[Bar(b['bar_open_ms']//1000,b['open'],b['high'],b['low'],b['close']) for b in bars if b['bar_close_ms']//1000<=cutoff]
                counts={str(tf):len(session_aggregate(raw,tf,cutoff)) for tf in (60,240)}
                feeds[symbol]['complete_htf_bars']=counts
                if symbol in ('ES','NQ') and counts['240']<3:bar_reasons.append(symbol+'_HTF_HISTORY_WARMUP_REQUIRED')
            atr=calculate(histories['MNQ'],cutoff=cutoff,now=now,policy=self.atr_policy)
        mapping=contract_map(self.config,self.tickers,now)
        if cutoff is not None:
            for symbol,item in mapping['bindings'].items():
                if cutoff-60<item['valid_from']:mapping['reasons'].append(symbol+'_LATEST_BAR_OUTSIDE_CONTRACT_MAPPING')
            mapping['ready']=not mapping['reasons']
        result=dict(version=VERSION,checked_at=now,cutoff=cutoff,analysis_inputs=['ES','NQ'],execution_symbol='MNQ',
            state='READY_FOR_QUOTE_CHECK' if not bar_reasons and mapping['ready'] else 'MARKET_DATA_BLOCKED',
            bars_ready=not bar_reasons,execution_prices_attested=mapping['ready'],
            entry_reasons=list(dict.fromkeys(bar_reasons+mapping['reasons'])),feeds=feeds,contract_map=mapping,daily_atr=atr,
            continuation_ready=bool(not bar_reasons and atr and atr['complete'] and not atr['reached']),
            orders_enabled=False,live_quote_connection_verified=False)
        self.cached=deepcopy(result)
        return result

    def quote_reasons(self,quotes,now):
        mapping=contract_map(self.config,self.tickers,now)
        reasons=list(mapping['reasons'])
        for symbol,item in mapping['bindings'].items():
            quote=quotes.get(symbol)
            if quote is None or quote.get('data',{}).get('contract')!=item['contract']:
                reasons.append(symbol+'_QUOTE_CONTRACT_MAPPING_MISMATCH')
        return list(dict.fromkeys(reasons))
