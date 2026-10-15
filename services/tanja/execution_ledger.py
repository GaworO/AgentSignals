"""Durable account-wide futures executions and revisable costs.

CrossTrade capture is evidence of rows, never proof of complete coverage or fee
finality. Only an explicit broker-statement reconciliation seals a window.
Unsealed results remain visible but cannot authorize new entries.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
import hashlib
import json
import re
from zoneinfo import ZoneInfo
from broker_feedback import encode

NY=ZoneInfo('America/New_York')


def amount(value):
    if isinstance(value,bool) or not isinstance(value,(str,int,float,Decimal)): raise ValueError('INVALID_AMOUNT')
    try: result=Decimal(str(value))
    except InvalidOperation: raise ValueError('INVALID_AMOUNT') from None
    if not result.is_finite(): raise ValueError('INVALID_AMOUNT')
    return Fraction(result)


def shown(value):
    with localcontext() as ctx:
        ctx.prec=40
        return format((Decimal(value.numerator)/Decimal(value.denominator)).quantize(Decimal('0.00000001')),'f')


def decimal_text(value):
    amount(value)
    text=format(Decimal(str(value)),'f')
    if '.' in text:text=text.rstrip('0').rstrip('.')
    return '0' if text in ('-0','') else text


def timestamp(value):
    if not isinstance(value,str): raise ValueError('UTC_FILL_TIMESTAMP_REQUIRED')
    try: dt=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError: raise ValueError('UTC_FILL_TIMESTAMP_REQUIRED') from None
    if dt.tzinfo is None or dt.utcoffset().total_seconds()!=0: raise ValueError('UTC_FILL_TIMESTAMP_REQUIRED')
    return dt.timestamp()


class ExecutionLedger:
    def __init__(self,store,config):
        self.store=store
        if config.get('TANJA_ACCOUNTING_PROVIDER','') not in ('','crosstrade'): raise ValueError('UNKNOWN_ACCOUNTING_PROVIDER')
        self.selected=config.get('TANJA_ACCOUNTING_PROVIDER','')=='crosstrade'
        self.account=str(config.get('TANJA_BROKER_ACCOUNT_ID',''))
        self.environment=config.get('TANJA_CROSSTRADE_ENVIRONMENT','')
        self.specs=json.loads(config.get('TANJA_ACCOUNTING_CONTRACT_SPECS','{}'))
        if not isinstance(self.specs,dict): raise ValueError('CONTRACT_SPECS_REQUIRED')
        for alias,spec in self.specs.items():
            if (not isinstance(alias,str) or not isinstance(spec,dict) or set(spec)!={'contract','multiplier','tick_size'}
                or not re.fullmatch(r'[A-Z0-9]+[FGHJKMNQUVXZ]20\d{2}',str(spec['contract']))
                or amount(spec['multiplier'])<=0 or amount(spec['tick_size'])<=0): raise ValueError('INVALID_CONTRACT_SPEC')
        by_contract={}
        for spec in self.specs.values():
            values=(amount(spec['multiplier']),amount(spec['tick_size']))
            if spec['contract'] in by_contract and by_contract[spec['contract']]!=values: raise ValueError('CONFLICTING_CONTRACT_SPECS')
            by_contract[spec['contract']]=values
        self.configured=bool(self.account and self.environment in ('demo','live') and self.specs)
        self.binding=hashlib.sha256(encode([self.account,self.environment,self.specs]).encode()).hexdigest()
        with store.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS execution_ledger_fills(
                binding TEXT,execution_id TEXT,at REAL,core TEXT,PRIMARY KEY(binding,execution_id));
                CREATE TABLE IF NOT EXISTS execution_ledger_costs(
                revision INTEGER PRIMARY KEY AUTOINCREMENT,binding TEXT,execution_id TEXT,observed_at REAL,cost TEXT);
                CREATE TABLE IF NOT EXISTS execution_ledger_sync(
                id INTEGER PRIMARY KEY AUTOINCREMENT,binding TEXT,at REAL,status TEXT,details TEXT);
                CREATE TABLE IF NOT EXISTS execution_ledger_seals(
                id INTEGER PRIMARY KEY AUTOINCREMENT,binding TEXT,at REAL,payload TEXT);
                CREATE TABLE IF NOT EXISTS execution_ledger_issues(
                binding TEXT,code TEXT,PRIMARY KEY(binding,code));
                CREATE TABLE IF NOT EXISTS execution_ledger_raw(
                binding TEXT,digest TEXT,at REAL,payload TEXT,PRIMARY KEY(binding,digest));
                CREATE INDEX IF NOT EXISTS execution_ledger_cost_lookup ON execution_ledger_costs(binding,execution_id,revision);
                CREATE INDEX IF NOT EXISTS execution_ledger_fill_clock ON execution_ledger_fills(binding,at);''')

    def note_sync(self,now,status,details=None):
        with self.store.connect() as db:
            db.execute('INSERT INTO execution_ledger_sync(binding,at,status,details) VALUES(?,?,?,?)',
                (self.binding,now,status,encode(details or {})))

    def ingest(self,rows,now):
        if not self.configured: raise ValueError('ACCOUNTING_NOT_CONFIGURED')
        if not isinstance(rows,list): raise ValueError('FILL_LIST_REQUIRED')
        validated=[];batch={}
        for row in rows:
            # Validate scope even when the server-side account filter was used.
            if str(row.get('accountId',''))!=self.account or row.get('environment')!=self.environment:
                raise ValueError('FILL_ACCOUNT_OWNERSHIP_UNRESOLVED')
            eid=row.get('executionId')
            if not isinstance(eid,str) or not re.fullmatch(r'tv:'+self.environment+r':[0-9]+',eid): raise ValueError('EXECUTION_ID_REQUIRED')
            if str(row.get('fillId'))!=eid.rsplit(':',1)[1]: raise ValueError('FILL_ID_CONFLICT')
            spec=self.specs.get(row.get('instrument'))
            if not spec: raise ValueError('UNKNOWN_ACCOUNT_INSTRUMENT')
            qty=row.get('qty');price=amount(row.get('price'));at=timestamp(row.get('timestamp'))
            if type(qty) is not int or qty<1 or row.get('action') not in ('Buy','Sell'):
                raise ValueError('FILL_SIDE_QUANTITY_INVALID')
            if price<=0 or price%amount(spec['tick_size']) or not 0<=at<=now: raise ValueError('FILL_PRICE_OR_CLOCK_INVALID')
            # Decimal strings retain the captured precision; calculations use rationals.
            core=dict(execution_id=eid,contract=spec['contract'],quantity=qty if row['action']=='Buy' else -qty,
                price=decimal_text(row['price']),at=at,multiplier=str(spec['multiplier']),order_id=str(row.get('orderId','')))
            costs={key:None if row.get(key) is None else decimal_text(row[key]) for key in ('commission','fees')}
            if any(value is not None and amount(value)<0 for value in costs.values()): raise ValueError('NEGATIVE_FILL_COST')
            normalized=(eid,at,encode(core),encode(costs))
            if eid in batch and batch[eid]!=normalized: raise ValueError('CONFLICTING_DUPLICATE_EXECUTION_IN_BATCH')
            if eid not in batch: validated.append(normalized)
            batch[eid]=normalized
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for eid,at,core,cost in validated:
                old=db.execute('SELECT core FROM execution_ledger_fills WHERE binding=? AND execution_id=?',(self.binding,eid)).fetchone()
                if old and old[0]!=core:
                    db.execute('INSERT OR IGNORE INTO execution_ledger_issues VALUES(?,?)',(self.binding,'EXECUTION_CORE_CONFLICT'))
                    db.commit()
                    raise ValueError('EXECUTION_CORE_CONFLICT_RECONCILIATION_REQUIRED')
            for eid,at,core,cost in validated:
                db.execute('INSERT OR IGNORE INTO execution_ledger_fills VALUES(?,?,?,?)',(self.binding,eid,at,core))
                old=db.execute('SELECT cost FROM execution_ledger_costs WHERE binding=? AND execution_id=? ORDER BY revision DESC LIMIT 1',(self.binding,eid)).fetchone()
                if not old or old[0]!=cost:
                    db.execute('INSERT INTO execution_ledger_costs(binding,execution_id,observed_at,cost) VALUES(?,?,?,?)',(self.binding,eid,now,cost))
        return len(validated)

    def capture_session(self,payload,now):
        """Preserve raw session evidence; identity-wide rows do not prove account ownership."""
        raw=encode(payload);digest=hashlib.sha256(raw.encode()).hexdigest()
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO execution_ledger_raw VALUES(?,?,?,?)',(self.binding,digest,now,raw))

    def records(self):
        with self.store.connect() as db:
            rows=db.execute('''SELECT f.core,c.cost FROM execution_ledger_fills f
                JOIN execution_ledger_costs c ON c.revision=(SELECT MAX(revision) FROM execution_ledger_costs
                WHERE binding=f.binding AND execution_id=f.execution_id)
                WHERE f.binding=? ORDER BY f.at,f.execution_id''',(self.binding,)).fetchall()
        return [dict(json.loads(r[0]),cost=json.loads(r[1])) for r in rows]

    @staticmethod
    def fingerprint(rows): return hashlib.sha256(encode(rows).encode()).hexdigest()

    def cost_history(self):
        with self.store.connect() as db:
            rows=db.execute('SELECT revision,execution_id,observed_at,cost FROM execution_ledger_costs WHERE binding=? ORDER BY revision',(self.binding,)).fetchall()
        return [dict(revision=r[0],execution_id=r[1],observed_at=r[2],cost=json.loads(r[3])) for r in rows]

    @staticmethod
    def calculate(rows):
        positions={};closed=[];pending=set();ties=set();last={}
        for f in rows:
            symbol=f['contract'];signed=f['quantity'];quantity=abs(signed);side=1 if signed>0 else -1
            if last.get(symbol)==f['at']: ties.add(symbol)
            last[symbol]=f['at']
            if any(v is None for v in f['cost'].values()): pending.add(f['execution_id'])
            fee=sum((amount(v) for v in f['cost'].values() if v is not None),Fraction())
            price=amount(f['price']);multiplier=amount(f['multiplier'])
            p=positions.get(symbol)
            closing=min(quantity,abs(p['qty'])) if p and p['qty']*signed<0 else 0
            if closing:
                p['gross']+=(price-p['average'])*(1 if p['qty']>0 else -1)*closing*multiplier
                p['fees']+=fee*closing/quantity;p['ids'].add(f['execution_id'])
                p['qty']+=side*closing
                if not p['qty']:
                    closed.append(dict(trade_id=p['id'],contract=symbol,opened_at=p['opened'],closed_at=f['at'],
                        gross_usd=shown(p['gross']),fees_usd=shown(p['fees']),net_usd=shown(p['gross']-p['fees']),
                        losing=p['gross']-p['fees']<0,fill_ids=sorted(p['ids'])))
                    del positions[symbol];p=None
            opening=quantity-closing
            if opening:
                if p is None:
                    p=dict(id=f['execution_id']+':'+symbol,opened=f['at'],qty=0,average=Fraction(),gross=Fraction(),fees=Fraction(),ids=set())
                    positions[symbol]=p
                p['average']=(p['average']*abs(p['qty'])+price*opening)/(abs(p['qty'])+opening)
                p['qty']+=side*opening;p['fees']+=fee*opening/quantity;p['ids'].add(f['execution_id'])
        return closed,{s:p['qty'] for s,p in positions.items()},pending,ties

    def reconcile(self,document,now):
        """Offline operator attestation against an actual broker statement/export.

        No HTTP endpoint accepts these attestations; CrossTrade paging never calls
        this method. The statement must include all instruments and opening flat
        inventory. A new or revised covered fill invalidates its fingerprint.
        """
        required={'account_id','environment','source','evidence_sha256','flat_at','through','fingerprint','positions','fees_confirmed'}
        if not isinstance(document,dict) or set(document)!=required: raise ValueError('STATEMENT_RECONCILIATION_SCHEMA_REQUIRED')
        if str(document['account_id'])!=self.account or document['environment']!=self.environment: raise ValueError('STATEMENT_ACCOUNT_MISMATCH')
        if document['source']!='broker_statement' or not re.fullmatch(r'[a-f0-9]{64}',str(document['evidence_sha256'])):
            raise ValueError('BROKER_STATEMENT_EVIDENCE_REQUIRED')
        start=timestamp(document['flat_at']);end=timestamp(document['through'])
        if not 0<=start<end<=now: raise ValueError('STATEMENT_WINDOW_INVALID')
        rows=[r for r in self.records() if start<=r['at']<=end]
        if self.fingerprint(rows)!=document['fingerprint']: raise ValueError('STATEMENT_EXECUTIONS_OR_COSTS_CHANGED')
        trades,positions,pending,ties=self.calculate(rows)
        if pending or ties: raise ValueError('STATEMENT_COST_OR_FILL_ORDER_UNRESOLVED')
        if (not isinstance(document['fees_confirmed'],list) or len(document['fees_confirmed'])!=len(rows)
            or set(document['fees_confirmed'])!={r['execution_id'] for r in rows}): raise ValueError('CONFIRM_EVERY_FILL_COST_INCLUDING_ZERO')
        if document['positions']!=positions or any(type(v) is not int or not v for v in document['positions'].values()):
            raise ValueError('STATEMENT_POSITION_MISMATCH')
        with self.store.connect() as db:
            # Keep a revision watermark as well as values: a correction and later
            # reversion must not silently resurrect an older confirmation.
            revision=db.execute('SELECT COALESCE(MAX(revision),0) FROM execution_ledger_costs WHERE binding=?',(self.binding,)).fetchone()[0]
            db.execute('INSERT INTO execution_ledger_seals(binding,at,payload) VALUES(?,?,?)',
                (self.binding,now,encode(dict(document,_cost_revision=revision))))

    def state(self,now):
        with self.store.connect() as db:
            seal=db.execute('SELECT payload FROM execution_ledger_seals WHERE binding=? ORDER BY id DESC LIMIT 1',(self.binding,)).fetchone()
            sync=db.execute('SELECT at,status,details FROM execution_ledger_sync WHERE binding=? ORDER BY id DESC LIMIT 1',(self.binding,)).fetchone()
            issues=[r[0] for r in db.execute('SELECT code FROM execution_ledger_issues WHERE binding=?',(self.binding,))]
            revisions=db.execute('SELECT COUNT(*) FROM execution_ledger_costs WHERE binding=?',(self.binding,)).fetchone()[0]
        rows=self.records();seal=json.loads(seal[0]) if seal else None
        reasons=[]
        if not self.configured: reasons.append('ACCOUNTING_NOT_CONFIGURED')
        if not sync or sync['status']!='CAPTURED' or not 0<=now-sync['at']<=5: reasons.append('ACCOUNT_CAPTURE_NOT_CURRENT')
        midnight=datetime.fromtimestamp(now,NY).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        start=timestamp(seal['flat_at']) if seal else 0
        relevant=[r for r in rows if r['at']>=start]
        trades,positions,pending,ties=self.calculate(relevant)
        reasons.extend(issues)
        if pending: reasons.append('FILL_COSTS_MISSING')
        if ties: reasons.append('SIMULTANEOUS_FILLS_ORDER_UNRESOLVED')
        if not seal: reasons.extend(['ACCOUNT_WIDE_COVERAGE_UNVERIFIED','FILL_FEES_NOT_FINAL'])
        else:
            end=timestamp(seal['through'])
            covered=[r for r in rows if start<=r['at']<=end]
            with self.store.connect() as db:
                changed=db.execute('''SELECT 1 FROM execution_ledger_costs c JOIN execution_ledger_fills f
                    ON f.binding=c.binding AND f.execution_id=c.execution_id
                    WHERE c.binding=? AND f.at>=? AND f.at<=? AND c.revision>? LIMIT 1''',
                    (self.binding,start,end,seal.get('_cost_revision',0))).fetchone()
            if changed or self.fingerprint(covered)!=seal['fingerprint']: reasons.append('RECONCILED_EXECUTIONS_OR_FEES_CHANGED')
            if start>midnight or not 0<=now-end<=5 or any(r['at']>end for r in relevant): reasons.append('ACCOUNT_WIDE_COVERAGE_NOT_CURRENT')
        today=datetime.fromtimestamp(now,NY).date()
        losses=sum(t['losing'] and datetime.fromtimestamp(t['closed_at'],NY).date()==today for t in trades)
        ready=not reasons
        return dict(selected=self.selected,configured=self.configured,accounting_ready=ready,
            status='ACCOUNTING_RECONCILED' if ready else 'ACCOUNTING_RECONCILIATION_REQUIRED',
            loss_scope='entire_broker_account',loss_count=losses if ready else None,
            provisional_loss_count=losses,reasons=sorted(set(reasons)),fill_count=len(rows),cost_observations=revisions,
            positions=positions,trades=trades[-100:],fingerprint=self.fingerprint(relevant),
            capture_status=sync['status'] if sync else 'NOT_CAPTURED',last_capture_at=sync['at'] if sync else None,
            capture_detail=json.loads(sync['details']) if sync else {},
            note='CrossTrade costs and history remain provisional until checked against account-wide broker evidence. A statement seal expires for live entry gating after five seconds.')
