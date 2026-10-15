"""Authenticated broker-bridge intake, not a TradersPost read API.

Snapshots must be complete for the entire configured account. A signal receipt
is never a fill, and a paper result cannot be submitted as broker feedback.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
import math
import re

VERSION='broker-feedback-v1'
NY=ZoneInfo('America/New_York')


def encode(value):return json.dumps(value,sort_keys=True,allow_nan=False)
def number(value):return type(value) in (int,float) and math.isfinite(value)
def tick(value):return number(value) and value>0 and value*4==round(value*4)
def identity(value):return isinstance(value,str) and bool(re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',value))


class BrokerFeedback:
    def __init__(self,store,config):
        self.store=store
        self.token=config.get('TANJA_BROKER_FEEDBACK_TOKEN','')
        self.account=config.get('TANJA_BROKER_ACCOUNT_ID','')
        self.contract=config.get('TANJA_EXECUTION_CONTRACT','')
        self.source=config.get('TANJA_BROKER_FEEDBACK_SOURCE','')
        from live_quotes import LiveQuotes
        from execution_ledger import ExecutionLedger
        self.quote_feed=LiveQuotes(store,config)
        self.accounting=ExecutionLedger(store,config)
        self.configured=bool(re.fullmatch(r'[A-Za-z0-9_-]{32,128}',self.token) and identity(self.account)
            and identity(self.source) and re.fullmatch(r'MNQ[HMUZ]20[0-9]{2}',self.contract))
        self.binding=hashlib.sha256(encode(dict(account=self.account,contract=self.contract,source=self.source)).encode()).hexdigest()
        with store.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS broker_feedback(
                binding TEXT,event_id TEXT,sequence INTEGER,kind TEXT,observed_at INTEGER,received_at REAL,payload TEXT,
                PRIMARY KEY(binding,event_id),UNIQUE(binding,sequence));
                CREATE TABLE IF NOT EXISTS broker_closed_trades(
                binding TEXT,trade_id TEXT,closed_at INTEGER,payload TEXT,PRIMARY KEY(binding,trade_id));''')

    def ingest(self,event,now):
        if not self.configured:raise ValueError('BROKER_FEEDBACK_NOT_CONFIGURED')
        fields={'schema_version','event_id','sequence','account_id','source','kind','observed_at','data'}
        if not isinstance(event,dict) or set(event)!=fields or type(event['schema_version']) is not int or event['schema_version']!=1:raise ValueError('BROKER_EVENT_SCHEMA_REQUIRED')
        if event['account_id']!=self.account or event['source']!=self.source:raise ValueError('BROKER_ACCOUNT_OR_SOURCE_MISMATCH')
        if not identity(event['event_id']) or type(event['sequence']) is not int or event['sequence']<1:raise ValueError('BROKER_EVENT_ID_AND_SEQUENCE_REQUIRED')
        at=event['observed_at']
        if type(at) is not int or not 0<=now-at<=10:raise ValueError('FRESH_BROKER_EVENT_REQUIRED')
        data=event['data'];kind=event['kind']
        if kind=='quote':
            if not isinstance(data,dict) or set(data)!={'symbol','contract','bid','ask','last'}:raise ValueError('QUOTE_SCHEMA_REQUIRED')
            if data['symbol'] not in ('ES','NQ','MNQ') or not identity(data['contract']):raise ValueError('QUOTE_INSTRUMENT_REQUIRED')
            if data['symbol']=='MNQ' and data['contract']!=self.contract:raise ValueError('EXECUTION_CONTRACT_MISMATCH')
            if any(not tick(data[k]) for k in ('bid','ask','last')) or data['bid']>data['ask']:raise ValueError('VALID_LIVE_QUOTE_REQUIRED')
            if now-at>2:raise ValueError('LIVE_QUOTE_TOO_OLD')
        elif kind=='snapshot':self.validate_snapshot(data,at)
        else:raise ValueError('BROKER_EVENT_KIND_UNSUPPORTED')
        raw=encode(event)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT payload FROM broker_feedback WHERE binding=? AND event_id=?',(self.binding,event['event_id'])).fetchone()
            if old:
                if old[0]!=raw:raise ValueError('BROKER_EVENT_CONFLICT')
                return dict(status='duplicate')
            last=db.execute('SELECT MAX(sequence) FROM broker_feedback WHERE binding=?',(self.binding,)).fetchone()[0]
            if last is not None and event['sequence']<=last:raise ValueError('BROKER_SEQUENCE_MUST_ADVANCE')
            if kind=='snapshot':
                for trade in data['closed_trades']:
                    old=db.execute('SELECT payload FROM broker_closed_trades WHERE binding=? AND trade_id=?',(self.binding,trade['trade_id'])).fetchone()
                    text=encode(trade)
                    if old and old[0]!=text:raise ValueError('BROKER_TRADE_CONFLICT')
                    db.execute('INSERT OR IGNORE INTO broker_closed_trades VALUES(?,?,?,?)',(self.binding,trade['trade_id'],trade['closed_at'],text))
            db.execute('INSERT INTO broker_feedback VALUES(?,?,?,?,?,?,?)',(self.binding,event['event_id'],event['sequence'],kind,at,now,raw))
        return dict(status='stored')

    @staticmethod
    def validate_snapshot(data,at):
        required={'complete','scope','history_start','history_end','account','positions','orders','fills','closed_trades'}
        if not isinstance(data,dict) or set(data)!=required or data['complete'] is not True or data['scope']!='entire_broker_account':raise ValueError('COMPLETE_ACCOUNT_WIDE_SNAPSHOT_REQUIRED')
        midnight=int(datetime.fromtimestamp(at,NY).replace(hour=0,minute=0,second=0,microsecond=0).timestamp())
        if type(data['history_start']) is not int or type(data['history_end']) is not int or not 0<=data['history_start']<=midnight<=data['history_end']<=at:raise ValueError('COMPLETE_NEW_YORK_DAY_HISTORY_REQUIRED')
        if at-data['history_end']>2:raise ValueError('BROKER_HISTORY_NOT_CURRENT')
        account=data['account']
        values=('balance','minimum_balance','target_balance','daily_loss_remaining','committed_risk_usd','committed_micro_contracts')
        if not isinstance(account,dict) or set(account)!=set(values) or any(not number(account[k]) or account[k]<0 for k in values):raise ValueError('CURRENT_ACCOUNT_LIMITS_REQUIRED')
        if type(account['committed_micro_contracts']) is not int or not 0<account['minimum_balance']<account['target_balance'] or account['balance']<=0:raise ValueError('INVALID_ACCOUNT_LIMITS')
        for name in ('positions','orders','fills','closed_trades'):
            if not isinstance(data[name],list):raise ValueError('BROKER_LIST_REQUIRED')
        seen=set()
        for trade in data['closed_trades']:
            if not isinstance(trade,dict) or set(trade)!={'trade_id','contract','opened_at','closed_at','gross_usd','fees_usd'}:raise ValueError('CLOSED_TRADE_SCHEMA_REQUIRED')
            if not identity(trade['trade_id']) or trade['trade_id'] in seen or not identity(trade['contract']):raise ValueError('UNIQUE_ACCOUNT_TRADE_ID_REQUIRED')
            seen.add(trade['trade_id'])
            if any(type(trade[k]) is not int for k in ('opened_at','closed_at')) or not 0<=trade['opened_at']<=trade['closed_at']<=at:raise ValueError('TRADE_CLOCK_INVALID')
            if not number(trade['gross_usd']) or not number(trade['fees_usd']) or trade['fees_usd']<0:raise ValueError('TRADE_NET_PNL_REQUIRED')
        for item in data['positions']:
            if not isinstance(item,dict) or not identity(item.get('contract')) or type(item.get('quantity')) is not int or item['quantity']<1 or item.get('direction') not in ('long','short') or not number(item.get('average_price')) or item['average_price']<=0:raise ValueError('BROKER_POSITION_INVALID')
        for item in data['orders']:
            if not isinstance(item,dict) or not identity(item.get('order_id')) or not identity(item.get('contract')) or item.get('state') not in ('working','filled','cancelled','rejected') or type(item.get('quantity')) is not int or item['quantity']<1:raise ValueError('BROKER_ORDER_INVALID')
        seen=set()
        for item in data['fills']:
            if not isinstance(item,dict) or not identity(item.get('fill_id')) or item['fill_id'] in seen or not identity(item.get('order_id')) or not identity(item.get('contract')) or type(item.get('quantity')) is not int or item['quantity']<1 or not tick(item.get('price')) or type(item.get('at')) is not int or not 0<=item['at']<=at:raise ValueError('BROKER_FILL_INVALID')
            seen.add(item['fill_id'])

    def latest(self,kind,symbol=None):
        with self.store.connect() as db:
            rows=db.execute('SELECT payload FROM broker_feedback WHERE binding=? AND kind=? ORDER BY sequence DESC LIMIT 100',(self.binding,kind)).fetchall()
        for row in rows:
            event=json.loads(row[0])
            if symbol is None or event['data'].get('symbol')==symbol:return event

    def state(self,now):
        snapshot=self.latest('snapshot');quotes={s:self.latest('quote',s) for s in ('ES','NQ','MNQ')}
        fresh=bool(snapshot and 0<=now-snapshot['observed_at']<=5)
        day=datetime.fromtimestamp(now,NY).date()
        with self.store.connect() as db:
            trades=[json.loads(r[0]) for r in db.execute('SELECT payload FROM broker_closed_trades WHERE binding=?',(self.binding,))]
        losses=sum(datetime.fromtimestamp(t['closed_at'],NY).date()==day and t['gross_usd']-t['fees_usd'] < -1e-8 for t in trades)
        result=dict(version=VERSION,configured=self.configured,status='ACCOUNT_FEEDBACK_CURRENT' if fresh else 'BROKER_FEEDBACK_REQUIRED',
            account_verified=fresh,loss_count=losses if fresh else None,loss_scope='entire_broker_account',
            snapshot=snapshot,quotes=quotes,quotes_current=all(q and 0<=now-q['observed_at']<=2 for q in quotes.values()),
            note='An external broker bridge must supply actual quotes, orders, fills and complete account history. TradersPost has no public read API for this feed.')
        if self.quote_feed.selected:
            q=self.quote_feed.state(now)
            result.update(quotes=q['quotes'],quotes_current=q['quotes_current'],quote_feed=q)
        if self.accounting.selected:
            a=self.accounting.state(now)
            result.update(accounting=a,loss_count=a['loss_count'],account_verified=fresh and a['accounting_ready'],
                entry_accounting_required=True)
            if not a['accounting_ready']:result['status']=a['status']
        return result
