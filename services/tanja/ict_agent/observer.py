"""One observation transaction: data → context → hypotheses → RAG → decision."""
import json
import time
from . import VERSION
from .common import digest,encode
from .market import validate_bar,bar_id,fresh_state,advance,context,clock,intermarket
from .scenarios import update_setups,decide,TERMINAL

class Observer:
    def __init__(self,store,knowledge):
        self.store=store;self.knowledge=knowledge
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS retrievals(id TEXT PRIMARY KEY,payload TEXT)')
            self.manifest_hash=digest(json.loads(db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()[0]))

    def push(self,bars,*,emit=True):
        started=time.perf_counter()
        if not bars:raise ValueError('EMPTY_BATCH')
        bars=sorted(bars,key=lambda b:b['symbol'])
        for b in bars:validate_bar(b)
        if len({b['symbol'] for b in bars})!=len(bars) or len({b['ts'] for b in bars})!=1:
            raise ValueError('ONE_SHARED_MINUTE_PER_BATCH_REQUIRED')
        as_of=max(b['received_at'] for b in bars)
        fingerprint=digest(bars);batch_id=digest(['bars',[(b['symbol'],b['contract'],b['ts']) for b in bars]])[:24]
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT fingerprint,result FROM batches WHERE id=?',(batch_id,)).fetchone()
            if prior:
                if prior[0]!=fingerprint:raise ValueError('CONFLICTING_DUPLICATE_BATCH')
                return dict(json.loads(prior[1]),duplicate=True)
            row=db.execute('SELECT payload FROM memory WHERE id=1').fetchone()
            memory=json.loads(row[0]) if row else dict(cursor=-1,markets={},setups={},retrieval_ids={},snapshot_at=-1,revision=0)
            if as_of<=memory['cursor']:raise ValueError('OUT_OF_ORDER_OBSERVATION')
            events=[]
            for b in bars:
                symbol=b['symbol'];current=memory['markets'].get(symbol)
                if current and current['contract']!=b['contract']:
                    # Contract roll is explicit; no price stitching or copied zones.
                    e=dict(kind='CONTRACT_CHANGE',symbol=symbol,contract=b['contract'],timeframe=1,ts=b['ts'],available_at=as_of,
                        previous=current['contract'],evidence_ids=[bar_id(b)],status='CONFIRMED_IDENTIFIER_CHANGE')
                    e['id']=digest(e)[:24];events.append(e);current=None
                if current is None:current=fresh_state(symbol,b['contract']);memory['markets'][symbol]=current
                fs,es=advance(current,b);events.extend(es)
                old=db.execute('SELECT payload FROM bars WHERE id=?',(bar_id(b),)).fetchone()
                if old and old[0]!=encode(b):raise ValueError('BAR_IMMUTABILITY_CONFLICT')
                db.execute('INSERT OR IGNORE INTO bars VALUES(?,?,?,?,?,?)',(bar_id(b),symbol,b['contract'],b['ts'],b['received_at'],encode(b)))
                for f in fs:
                    db.execute('INSERT INTO frames VALUES(?,?,?,?,?,?,?)',(f['id'],symbol,b['contract'],f['timeframe'],f['ts'],f['available_at'],encode(f)))
            for e in events:
                if e['available_at']>as_of:raise ValueError('FUTURE_EVENT')
                db.execute('INSERT INTO events VALUES(?,?,?,?,?)',(e['id'],e['available_at'],e['symbol'],e['kind'],encode(e)))
            ctx=context(memory['markets'],as_of)
            relationships=intermarket(memory['markets'],as_of)
            transitions,retrievals=update_setups(memory['setups'],events,ctx,as_of,self.knowledge)
            for ident,retrieval in retrievals.items():
                key=digest(retrieval)
                db.execute('INSERT OR IGNORE INTO retrievals VALUES(?,?)',(key,encode(retrieval)))
                memory['retrieval_ids'][ident]=key
            for t in transitions:
                db.execute('INSERT INTO transitions VALUES(?,?,?,?)',(t['id'],t['setup_id'],t['at'],encode(t)))
            for s in memory['setups'].values():
                db.execute('INSERT INTO setups VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',(s['id'],encode(s)))
            meaningful=bool(transitions) or any(e['kind'] in ('CONTRACT_CHANGE','DATA_GAP') or
                (e['timeframe']>=5 and e['kind'] not in ('PIVOT',)) for e in events)
            snapshot_id=None
            if emit and (meaningful or as_of-memory['snapshot_at']>=300):
                decision=decide(memory['setups'],events,ctx,as_of)
                from .risk import check
                decision['risk_reviews']={i:check(memory['setups'][i]['plan'],None,{}, {},as_of)
                                          for i in decision['candidate_ids']}
                important=[e for e in events if e['symbol']=='NQ' and e['kind'] in ('SWEEP','FVG','IFVG','DISPLACEMENT','STRUCTURE_BREAK')]
                query=' '.join(sorted({e['kind'] for e in important})) or 'market context waiting no trade uncertainty'
                retrieved=self.knowledge.contextual(ctx,events) if hasattr(self.knowledge,'contextual') else self.knowledge.retrieve(query+' market context invalidation liquidity',event_sequence=tuple(e['kind'] for e in important[-4:]))
                rid=digest(retrieved)
                db.execute('INSERT OR IGNORE INTO retrievals VALUES(?,?)',(rid,encode(retrieved)))
                changed={t['setup_id'] for t in transitions}
                shown=[s for s in memory['setups'].values() if s['state'] not in TERMINAL or s['id'] in changed]
                # Snapshot is as-of immutable; later transitions/outcomes live elsewhere.
                snap=dict(schema_version=1,as_of=as_of,as_of_ny=clock(as_of).isoformat(),input_batch_id=batch_id,
                    code_version=VERSION,run_manifest_hash=self.manifest_hash,
                    knowledge_version=self.knowledge.version,index_version=self.knowledge.index_version,
                    revision=memory['revision']+1,context=ctx,intermarket=relationships,decision=decision,events=events,
                    event_kinds=sorted({e['kind'] for e in events}),setups=shown,transitions=transitions,
                    retrieval_ids=sorted({rid}|{memory['retrieval_ids'][s['id']] for s in shown if s['id'] in memory['retrieval_ids']}),
                    missing=['calibrated model comparison','verified HTF range selection','timestamped news','contract definition mapping','measured receipts and executable ticks'],
                    observed_symbols=[b['symbol'] for b in bars],orders_sent=0)
                snap['id']=digest(snap)[:24];snapshot_id=snap['id']
                db.execute('INSERT INTO snapshots VALUES(?,?,?,?)',(snapshot_id,as_of,decision['action'],encode(snap)))
                memory['snapshot_at']=as_of
            memory['cursor']=as_of;memory['revision']+=1
            # Terminal setups stay in the journal; only a small recent tail stays in hot memory.
            terminal=sorted([s for s in memory['setups'].values() if s['state'] in TERMINAL],key=lambda s:s['updated_at'])
            for s in terminal[:-8]:
                memory['setups'].pop(s['id']);memory['retrieval_ids'].pop(s['id'],None)
            db.execute('INSERT INTO memory VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',(encode(memory),))
            result=dict(batch_id=batch_id,as_of=as_of,events=len(events),transitions=len(transitions),snapshot_id=snapshot_id)
            db.execute('INSERT INTO batches VALUES(?,?,?,?)',(batch_id,fingerprint,as_of,encode(result)))
            db.execute('INSERT INTO measurements VALUES(?,?)',(batch_id,encode(dict(
                processing_ms=round((time.perf_counter()-started)*1000,3),
                data_latency_seconds={b['symbol']:b['received_at']-(b['ts']+60) for b in bars},
                latency_semantics='SOURCE_RECEIPTS' if all(b.get('source')=='TANJA_READ_ONLY_RECEIPT_TIME' for b in bars) else 'Historical close availability assumed; not measured provider latency.',
                llm_latency_ms=None,execution_latency_ms=None))))
        return result
