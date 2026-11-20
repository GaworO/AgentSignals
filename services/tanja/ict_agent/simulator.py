"""Durable broker protocol simulation, explicit injected quote/fill events only."""
import json
import sqlite3
from pathlib import Path
from .common import encode,digest
from .risk import check

class BrokerSimulator:
    def __init__(self,path):
        self.path=str(path);Path(path).parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS sim_orders(id TEXT PRIMARY KEY,opportunity TEXT UNIQUE,payload TEXT);
                CREATE TABLE IF NOT EXISTS sim_events(id TEXT PRIMARY KEY,fingerprint TEXT,result TEXT);
                CREATE TABLE IF NOT EXISTS sim_control(id INTEGER PRIMARY KEY,payload TEXT);
                INSERT OR IGNORE INTO sim_control VALUES(1,'{"kill":false,"reconciled":false}');''')
            db.execute('UPDATE sim_control SET payload=? WHERE id=1',(encode(dict(kill=True,reconciled=False,reason='PROCESS_START_RECONCILIATION_REQUIRED')),))

    def reconcile(self,at,positions):
        """Explicit simulated broker snapshot; equality required after restart/disconnect."""
        if type(at) is not int or not isinstance(positions,dict):raise ValueError('RECONCILIATION_SCHEMA')
        with sqlite3.connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            orders=[json.loads(r[0]) for r in db.execute('SELECT payload FROM sim_orders')]
            expected={}
            for o in orders:
                if o['filled']:
                    key=o['plan']['contract'];expected[key]=expected.get(key,0)+o['filled']*(1 if o['plan']['direction']=='LONG' else -1)
            expected={k:v for k,v in expected.items() if v}
            okay=positions==expected and all(not o['filled'] or o['protection']=='ACKNOWLEDGED_SIMULATED_STOP' for o in orders)
            value=dict(kill=not okay,reconciled=okay,as_of=at,positions=positions)
            db.execute('UPDATE sim_control SET payload=? WHERE id=1',(encode(value),))
            return value

    def submit(self, opportunity,plan,policy,account,market,now):
        if policy is None:return dict(status='REJECTED',risk=dict(approved=False,reasons=['RISK_POLICY_NOT_CONFIGURED']),orders_sent=0)
        with sqlite3.connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT payload FROM sim_orders WHERE opportunity=?',(opportunity,)).fetchone()
            if old:
                p=json.loads(old[0])
                if p['plan']!=plan:raise ValueError('CONFLICTING_DUPLICATE_ORDER')
                return dict(p,duplicate=True)
            control=json.loads(db.execute('SELECT payload FROM sim_control WHERE id=1').fetchone()[0])
            if not control['reconciled'] or not 0<=now-control.get('as_of',-1)<=policy.max_account_age_seconds:
                return dict(status='REJECTED',risk=dict(approved=False,reasons=['SIMULATOR_RECONCILIATION_REQUIRED']),orders_sent=0)
            # All outstanding reservations in THIS simulator are included atomically.
            outstanding=[json.loads(r[0]) for r in db.execute('SELECT payload FROM sim_orders')]
            reserved=sum(o['risk']['risk_per_contract_usd']*o['quantity'] for o in outstanding if o['status'] in ('PENDING','ACKNOWLEDGED','PARTIALLY_FILLED','FILLED'))
            reserved_contracts=sum(o['quantity'] for o in outstanding if o['status'] in ('PENDING','ACKNOWLEDGED','PARTIALLY_FILLED','FILLED'))
            account=dict(account,reserved_risk_usd=account.get('reserved_risk_usd',0)+reserved,
                         reserved_contracts=account.get('reserved_contracts',0)+reserved_contracts)
            verdict=check(plan,policy,account,market,now,kill=control['kill'])
            if not verdict['approved']:return dict(status='REJECTED',risk=verdict,orders_sent=0)
            order=dict(id=digest(['paper',opportunity])[:24],opportunity=opportunity,plan=plan,risk=verdict,
                quantity=verdict['quantity'],filled=0,status='PENDING',protection='NOT_YET_FILLED',at=now,
                last_event_at=now,fill_notional=0.,realized_usd=None,mode='BROKER_PROTOCOL_SIMULATION',orders_sent=0)
            db.execute('INSERT INTO sim_orders VALUES(?,?,?)',(order['id'],opportunity,encode(order)))
            return order

    def event(self,event_id,order_id,kind,at,quantity=0,price=None,protective_stop_ack=False):
        payload=dict(order_id=order_id,kind=kind,at=at,quantity=quantity,price=price,protective_stop_ack=protective_stop_ack)
        with sqlite3.connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT fingerprint,result FROM sim_events WHERE id=?',(event_id,)).fetchone()
            if old:
                if old[0]!=digest(payload):raise ValueError('CONFLICTING_BROKER_EVENT')
                return dict(json.loads(old[1]),duplicate=True)
            row=db.execute('SELECT payload FROM sim_orders WHERE id=?',(order_id,)).fetchone()
            if not row:raise ValueError('UNKNOWN_ORDER')
            o=json.loads(row[0])
            if type(at) is not int or at<o['last_event_at']:raise ValueError('BROKER_TIME_REGRESSION')
            if kind=='ACK' and o['status']=='PENDING':o['status']='ACKNOWLEDGED'
            elif kind=='REJECT' and o['status'] in ('PENDING','ACKNOWLEDGED') and not o['filled']:o['status']='REJECTED'
            elif kind=='CANCEL' and o['status'] in ('PENDING','ACKNOWLEDGED','PARTIALLY_FILLED'):
                o['status']='FILLED' if o['filled'] else 'CANCELLED';o['quantity']=o['filled']
            elif kind=='FILL' and o['status'] in ('PENDING','ACKNOWLEDGED','PARTIALLY_FILLED'):
                import math
                if type(quantity) is not int or not 0<quantity<=o['quantity']-o['filled'] or type(price) not in (int,float) or not math.isfinite(price) or price<=0 or price*4!=round(price*4):
                    raise ValueError('INVALID_FILL')
                o['filled']+=quantity;o['fill_notional']+=quantity*price
                o['status']='FILLED' if o['filled']==o['quantity'] else 'PARTIALLY_FILLED'
                o['protection']='ACKNOWLEDGED_SIMULATED_STOP' if protective_stop_ack is True else 'MISSING'
                if o['protection']=='MISSING':db.execute('UPDATE sim_control SET payload=? WHERE id=1',(encode(dict(kill=True,reconciled=False,reason='UNPROTECTED_SIMULATED_FILL')),))
            elif kind=='DISCONNECT':
                db.execute('UPDATE sim_control SET payload=? WHERE id=1',(encode(dict(kill=True,reconciled=False,reason='CONNECTION_LOST')),))
            elif kind=='PROTECTION_ACK' and o['filled'] and protective_stop_ack is True and price==o['plan']['stop']:
                o['protection']='ACKNOWLEDGED_SIMULATED_STOP'
            elif kind=='EXIT' and o['status'] in ('FILLED','PARTIALLY_FILLED'):
                import math
                if quantity!=o['filled'] or type(price) not in (int,float) or not math.isfinite(price) or price<=0 or price*4!=round(price*4):raise ValueError('INVALID_EXIT')
                from .market import SPECS
                sign=1 if o['plan']['direction']=='LONG' else -1
                o['realized_usd']=sign*(price*quantity-o['fill_notional'])*SPECS[o['plan']['symbol']]['multiplier']-o['risk']['cost_reserve_per_contract_usd']*quantity
                o['status']='CLOSED';o['filled']=0;o['remaining_entry_cancelled']=True
            else:raise ValueError('INVALID_BROKER_TRANSITION')
            o['last_event_at']=at
            db.execute('UPDATE sim_orders SET payload=? WHERE id=?',(encode(o),order_id))
            db.execute('INSERT INTO sim_events VALUES(?,?,?)',(event_id,digest(payload),encode(o)))
            return o
