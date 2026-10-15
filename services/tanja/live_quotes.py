"""Independent, authenticated quote bridge. No candles or send-time heartbeats.

The producer must supply actual exchange/component timestamps and dated
contracts. This module neither subscribes to paid data nor sends orders.
"""
import hashlib
import json
import re
from broker_feedback import encode, identity, tick, number


class LiveQuotes:
    def __init__(self, store, config):
        self.store = store
        self.token = config.get('TANJA_QUOTE_FEED_TOKEN', '')
        self.source = config.get('TANJA_QUOTE_SOURCE', '')
        self.selected = bool(self.token or self.source or config.get('TANJA_QUOTE_CONTRACTS'))
        self.contracts = json.loads(config.get('TANJA_QUOTE_CONTRACTS', '{}'))
        self.configured = bool(re.fullmatch(r'[A-Za-z0-9_-]{32,128}', self.token)
            and identity(self.source) and isinstance(self.contracts, dict)
            and set(self.contracts) == {'ES', 'NQ', 'MNQ'}
            and all(re.fullmatch(s+r'[HMUZ]20\d{2}', str(c)) for s,c in self.contracts.items()))
        if self.configured:
            self.configured = (self.contracts['NQ'][2:] == self.contracts['MNQ'][3:]
                and self.contracts['MNQ'] == config.get('TANJA_EXECUTION_CONTRACT'))
        self.binding = hashlib.sha256(encode([self.source,self.contracts]).encode()).hexdigest()
        with store.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS live_quote_streams(
                binding TEXT PRIMARY KEY,generation TEXT,sequence INTEGER,connected INTEGER,payload TEXT);
                CREATE TABLE IF NOT EXISTS live_quote_generations(
                binding TEXT,generation TEXT,PRIMARY KEY(binding,generation));
                CREATE TABLE IF NOT EXISTS live_quote_latest(
                binding TEXT,symbol TEXT,payload TEXT,PRIMARY KEY(binding,symbol));''')

    def ingest(self, event, now):
        if not self.configured: raise ValueError('QUOTE_SOURCE_NOT_CONFIGURED')
        required={'source','generation','sequence','kind','sent_at','data'}
        if not isinstance(event,dict) or set(event)!=required: raise ValueError('QUOTE_ENVELOPE_REQUIRED')
        if event['source']!=self.source or not identity(event['generation']): raise ValueError('QUOTE_SOURCE_MISMATCH')
        if type(event['sequence']) is not int or event['sequence']<1: raise ValueError('QUOTE_SEQUENCE_REQUIRED')
        if not number(event['sent_at']) or not 0<=now-event['sent_at']<=2: raise ValueError('QUOTE_TRANSPORT_STALE')
        kind=event['kind'];data=event['data']
        if kind in ('connected','disconnected'):
            if data!={}: raise ValueError('QUOTE_CONNECTION_SCHEMA_REQUIRED')
        elif kind=='quote':
            if not isinstance(data,dict) or set(data)!={'symbol','contract','bid','ask','last','bid_at','ask_at','last_at'}:
                raise ValueError('QUOTE_COMPONENT_CLOCKS_REQUIRED')
            if self.contracts.get(data['symbol'])!=data['contract']: raise ValueError('QUOTE_CONTRACT_MISMATCH')
            if any(not tick(data[k]) for k in ('bid','ask','last')) or data['bid']>data['ask']:
                raise ValueError('QUOTE_PRICES_INVALID')
            if any(not number(data[k]) or not 0<=now-data[k]<=2 or data[k]>event['sent_at'] for k in ('bid_at','ask_at','last_at')):
                raise ValueError('QUOTE_COMPONENT_STALE')
        else: raise ValueError('QUOTE_KIND_UNSUPPORTED')
        raw=encode(event)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM live_quote_streams WHERE binding=?',(self.binding,)).fetchone()
            if kind=='connected':
                if row and row['generation']==event['generation'] and row['payload']==raw: return {'status':'duplicate'}
                if event['sequence']!=1 or db.execute('SELECT 1 FROM live_quote_generations WHERE binding=? AND generation=?',(self.binding,event['generation'])).fetchone():
                    raise ValueError('NEW_QUOTE_GENERATION_REQUIRED')
                db.execute('INSERT INTO live_quote_generations VALUES(?,?)',(self.binding,event['generation']))
                db.execute('DELETE FROM live_quote_latest WHERE binding=?',(self.binding,))
            else:
                if not row or row['generation']!=event['generation']: raise ValueError('QUOTE_GENERATION_MISMATCH')
                if row['sequence']==event['sequence'] and row['payload']==raw: return {'status':'duplicate'}
                if not row['connected']: raise ValueError('QUOTE_RECONNECT_REQUIRED')
                if event['sequence']!=row['sequence']+1:
                    # Commit the invalidation even though the caller gets a rejection.
                    db.execute('UPDATE live_quote_streams SET connected=0 WHERE binding=?',(self.binding,))
                    db.execute('DELETE FROM live_quote_latest WHERE binding=?',(self.binding,))
                    db.commit()
                    raise ValueError('QUOTE_SEQUENCE_GAP_RECONNECT_REQUIRED')
                if kind=='quote':
                    prior=db.execute('SELECT payload FROM live_quote_latest WHERE binding=? AND symbol=?',(self.binding,data['symbol'])).fetchone()
                    if prior and any(data[k]<json.loads(prior[0])['data'][k] for k in ('bid_at','ask_at','last_at')):
                        raise ValueError('QUOTE_COMPONENT_CLOCK_REGRESSION')
            if kind=='quote':
                normalized=dict(event_id=self.binding+':'+event['generation']+':'+str(event['sequence']),
                    observed_at=min(data[k] for k in ('bid_at','ask_at','last_at')),data=data,
                    source=self.source,generation=event['generation'],received_at=now)
                db.execute('INSERT OR REPLACE INTO live_quote_latest VALUES(?,?,?)',(self.binding,data['symbol'],encode(normalized)))
            if kind=='disconnected': db.execute('DELETE FROM live_quote_latest WHERE binding=?',(self.binding,))
            db.execute('INSERT OR REPLACE INTO live_quote_streams VALUES(?,?,?,?,?)',
                (self.binding,event['generation'],event['sequence'],int(kind!='disconnected'),raw))
        return {'status':'stored'}

    def state(self, now):
        with self.store.connect() as db:
            stream=db.execute('SELECT connected FROM live_quote_streams WHERE binding=?',(self.binding,)).fetchone()
            rows=db.execute('SELECT symbol,payload FROM live_quote_latest WHERE binding=?',(self.binding,)).fetchall()
        found={r['symbol']:json.loads(r['payload']) for r in rows}
        quotes={s:found.get(s) for s in ('ES','NQ','MNQ')}
        current=bool(self.configured and stream and stream[0] and all(q and 0<=now-q['observed_at']<=2
            and 0<=now-q['received_at']<=2 for q in quotes.values()))
        return dict(selected=self.selected,configured=self.configured,source=self.source,quotes=quotes,
            quotes_current=current,status='LIVE_QUOTES_CURRENT' if current else 'LIVE_QUOTES_REQUIRED')
