"""Builder's account-bound native order journal and broker reconciliation.

An accepted OSO is not a fill. Mutations are reserved durably before HTTP and
never blindly retried. Only explicit broker IDs or a matching New/clOrdId
establish ownership. Broker flat alone never cancels an unknown submission.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener

import builder_broker_sync as account_sync

TERMINAL = {'Filled','Completed','Canceled','Cancelled','Rejected','Expired'}
ACTIVE = {'Working','Suspended','Accepted','PendingNew','PendingReplace','PendingCancel','PartFilled','PartiallyFilled'}
_API = None
_TRANSPORT = None
_LOCK = threading.RLock()


def provider():
    value = os.environ.get('BUILDER_ORDER_PROVIDER','traderspost').strip().lower()
    return value if value in ('traderspost','crosstrade') else 'invalid'


def selected():
    return provider() == 'crosstrade'


def numeric(value):
    return type(value) is int and value > 0


def tick(value):
    return type(value) in (int,float) and math.isfinite(value) and value > 0 and value*4 == round(value*4)


def contract_aliases(contract):
    month={'H':'03','M':'06','U':'09','Z':'12'}[contract[3]]
    return {contract,contract[:-4]+contract[-1],f'MNQ {month}-{contract[-2:]}'}


def oco_linked(a,b):
    # Tradovate represents an OCO using order links; accept a common anchor or
    # reciprocal sibling IDs, in addition to a shared positive group ID.
    left,right=a.get('ocoId'),b.get('ocoId')
    return bool((numeric(left) and left==right) or
                (left==b['id'] and right==a['id']) or
                ((left or a['id'])==(right or b['id']) and (numeric(left) or numeric(right))))


def stamp(value):
    parsed = datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if parsed.tzinfo is None:raise ValueError('UTC_BROKER_TIMESTAMP_REQUIRED')
    return int(parsed.timestamp()*1000)


def encode(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def stable_version(life):
    changed=[c for c in life['commands'] if c.get('commandType')=='Modify']
    if not changed:return True
    version_id=life['version'].get('id')
    matching=[c for c in changed if numeric(version_id) and c.get('id')==version_id]
    if len(matching)!=1:return False
    reports=[r for r in life['reports'] if r.get('commandId')==version_id and numeric(r.get('id'))]
    if not reports:return False
    latest=max(reports,key=lambda r:r['id'])
    return (matching[0].get('commandStatus')=='Replaced' and latest.get('commandStatus')=='Replaced'
            and latest.get('rejectReason') in (None,'Success') and latest.get('ordStatus')=='Working')


class TransportError(ValueError):
    pass


class Transport:
    def __init__(self, api, sync, *, request=None, clock=time.time):
        self.api, self.sync, self.clock = api, sync, clock
        self.binding = sync.binding
        self.prefix = '/accounts/'+quote(sync.name,safe='')
        self.request = request or self._request
        self.contract_id = None
        self.poll_lock = threading.Lock()
        with api.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS native_orders(
                    binding TEXT,order_id TEXT,client_id TEXT,created_ms INTEGER,
                    expiry_ms INTEGER,state TEXT,command_json TEXT,receipt_json TEXT,
                    observation_json TEXT,error TEXT,updated_ms INTEGER,
                    PRIMARY KEY(binding,order_id),UNIQUE(binding,client_id));
                CREATE TABLE IF NOT EXISTS native_actions(
                    binding TEXT,action_id TEXT,order_id TEXT,operation TEXT,state TEXT,
                    payload_json TEXT,response_json TEXT,updated_ms INTEGER,
                    PRIMARY KEY(binding,action_id));
                CREATE UNIQUE INDEX IF NOT EXISTS native_single_close
                    ON native_actions(binding,order_id) WHERE operation='close';
                CREATE TABLE IF NOT EXISTS native_audit(
                    id INTEGER PRIMARY KEY,binding TEXT,order_id TEXT,at_ms INTEGER,
                    kind TEXT,payload_json TEXT);
                CREATE TABLE IF NOT EXISTS native_lifecycles(
                    binding TEXT,broker_id INTEGER,observed_ms INTEGER,payload_json TEXT,
                    PRIMARY KEY(binding,broker_id));
                CREATE TABLE IF NOT EXISTS native_exit_requests(
                    binding TEXT,order_id TEXT,reason TEXT,created_ms INTEGER,
                    PRIMARY KEY(binding,order_id));
            ''')

    def _request(self, method, path, body=None):
        allowed_read = (path == '/orders' or path.startswith('/fills/history?') or
                        path.startswith('/contracts/find?') or
                        re.fullmatch(re.escape(self.prefix)+r'/orders/[1-9][0-9]*(?:/lifecycle)?',path))
        allowed_write = (path == self.prefix+'/orders/place' or path == self.prefix+'/positions/close'
                         or re.fullmatch(re.escape(self.prefix)+r'/orders/[1-9][0-9]*/cancel',path))
        if not ((method=='GET' and allowed_read) or (method=='POST' and allowed_write)):
            raise TransportError('BROKER_ROUTE_NOT_ALLOWED')
        if method=='POST' and not selected():raise TransportError('CROSSTRADE_ROUTE_NOT_SELECTED')
        request = Request(account_sync.BASE+path,method=method,
            data=None if body is None else encode(body).encode(),
            headers={'Authorization':'Bearer '+self.sync.token,'Accept':'application/json','Content-Type':'application/json'})
        try:
            with build_opener(account_sync.NoRedirect()).open(request,timeout=20 if method=='GET' else 10) as response:
                raw=response.read(8_000_001)
                if len(raw)>8_000_000:raise TransportError('BROKER_RESPONSE_TOO_LARGE')
                result=json.loads(raw)
        except HTTPError as exc:
            raise TransportError('BROKER_HTTP_'+str(exc.code)) from None
        except (OSError,URLError,TimeoutError):
            raise TransportError('BROKER_REQUEST_OUTCOME_UNKNOWN') from None
        except (ValueError,UnicodeError):
            raise TransportError('BROKER_RESPONSE_INVALID') from None
        if not isinstance(result,dict):raise TransportError('BROKER_RESPONSE_OBJECT_REQUIRED')
        return result

    def read(self,path):
        result=self.request('GET',path)
        if result.get('success') is not True or result.get('partial') or result.get('unavailable'):
            raise TransportError('COMPLETE_BROKER_READ_REQUIRED')
        return result.get('data')

    def audit(self,c,oid,kind,payload):
        c.execute('INSERT INTO native_audit(binding,order_id,at_ms,kind,payload_json) VALUES(?,?,?,?,?)',
                  (self.binding,oid,int(self.clock()*1000),kind,encode(payload)))

    def row(self,oid):
        with self.api.connect() as c:
            r=c.execute('SELECT * FROM native_orders WHERE binding=? AND order_id=?',(self.binding,oid)).fetchone()
        return dict(r) if r else None

    def resolve_contract(self):
        contract=self.api.contract()
        if not re.fullmatch(r'MNQ[HMUZ]20\d{2}',contract):raise TransportError('DATED_MNQ_CONTRACT_REQUIRED')
        if self.contract_id is None:
            native=contract[:-4]+contract[-1]
            found=self.read('/contracts/find?'+urlencode({'name':native}))
            if not isinstance(found,dict) or found.get('name') not in (native,contract) or not numeric(found.get('id')):
                raise TransportError('EXACT_BROKER_CONTRACT_REQUIRED')
            self.contract_id=found['id']
        return self.contract_id

    def submit(self,payload,oid):
        now=int(self.clock()*1000)
        if not self.sync.enabled or not selected():raise TransportError('SELECTED_VERIFIED_NATIVE_ACCOUNT_REQUIRED')
        with self.api.connect() as c:
            r=c.execute('SELECT * FROM orders WHERE order_id=?',(oid,)).fetchone()
        if not r or r['state']!='PREPARED':raise TransportError('PREPARED_V3_ENTRY_REQUIRED')
        order=json.loads(r['order_json'])
        if (order['route_id']!=self.api.route() or order['contract']!=self.api.contract()
                or order['account_label']!=os.environ.get('ACCOUNT_LABEL','account')
                or not order['activation_ms']<=now<order['expiry_ms']):
            raise TransportError('PREPARED_ENTRY_IDENTITY_OR_EXPIRY_MISMATCH')
        quantity=payload.get('quantity')
        entry=payload.get('limitPrice');stop=(payload.get('stopLoss') or {}).get('stopPrice')
        target=(payload.get('takeProfit') or {}).get('limitPrice')
        action=payload.get('action');sign=1 if order['direction']=='LONG' else -1
        if (not numeric(quantity) or quantity!=order.get('quantity') or quantity>40
                or payload.get('orderType')!='limit' or payload.get('ticker')!=order['contract']
                or action!=('buy' if sign==1 else 'sell') or any(not tick(v) for v in (entry,stop,target))
                or entry!=order['entry_price'] or stop!=order['stop_price'] or target!=order['target_price']
                or sign*(entry-stop)<=0 or sign*(target-entry)<=0):
            raise TransportError('EXACT_PREPARED_NATIVE_BRACKET_REQUIRED')
        self.resolve_contract()
        if self.sync.entry_blocker(refresh=True):raise TransportError('FRESH_FLAT_ACCOUNT_REQUIRED')
        snapshot=self.sync.snapshot();now=int(self.clock()*1000)
        expiry=min(order['expiry_ms'],now+int(payload.get('cancelAfter',60))*1000)
        if expiry<=now or now>=order['expiry_ms']:raise TransportError('ENTRY_EXPIRED')
        client='b50-'+hashlib.sha256((self.binding+'|'+oid).encode()).hexdigest()[:56]
        body=dict(instrument=order['contract'][:-4]+order['contract'][-1],action=action,qty=quantity,
            orderType='limit',limitPrice=entry,takeProfit=target,stopLoss=stop,tif='gtd',
            expireTime=datetime.fromtimestamp(expiry/1000,timezone.utc).isoformat().replace('+00:00','Z'),
            orderId=client,requireMarketPosition='flat',maxPositions=1)
        command=dict(order=order,body=body,contract_id=self.contract_id,
            before_cash=snapshot['account']['balance']['totalCashValue'],
            before_fill_ids=sorted(f['id'] for f in snapshot['fills']))
        with self.api.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior=c.execute('SELECT state FROM native_orders WHERE binding=? AND order_id=?',(self.binding,oid)).fetchone()
            if prior:return dict(sent=False,submission_unknown=True,provider='crosstrade',reason='native_intent_already_reserved',qty=quantity,status=None)
            if c.execute("SELECT 1 FROM native_orders WHERE binding=? AND state NOT IN ('NO_FILL','CLOSED')",(self.binding,)).fetchone():
                raise TransportError('NATIVE_ORDER_RECONCILIATION_REQUIRED')
            c.execute('INSERT INTO native_orders VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (self.binding,oid,client,now,expiry,'SENDING',encode(command),None,None,None,now))
            self.audit(c,oid,'ENTRY_RESERVED',body)
        state='UNKNOWN';receipt=None;error='SUBMISSION_OUTCOME_UNKNOWN_NO_RETRY'
        try:
            reply=self.request('POST',self.prefix+'/orders/place',body)
            ids=reply.get('response',{})
            if (reply.get('success') is True and reply.get('destination')=='tradovate'
                    and reply.get('account')==self.sync.name and reply.get('instrument')==body['instrument']
                    and reply.get('api')=='place_with_brackets' and not reply.get('warning')
                    and isinstance(ids,dict) and all(numeric(ids.get(k)) for k in ('orderId','oso1Id','oso2Id'))
                    and len({ids[k] for k in ('orderId','oso1Id','oso2Id')})==3
                    and ids.get('osoChildIds')==[ids['oso1Id'],ids['oso2Id']]):
                receipt={k:ids[k] for k in ('orderId','oso1Id','oso2Id')}
                state='WAITING_BROKER_FILL';error=None
        except Exception:pass
        with self.api.connect() as c:
            c.execute("UPDATE native_orders SET state=?,receipt_json=?,error=?,updated_ms=? WHERE binding=? AND order_id=? AND state='SENDING'",
                (state,encode(receipt) if receipt else None,error,int(self.clock()*1000),self.binding,oid))
            self.audit(c,oid,state,receipt or dict(error=error))
        return dict(sent=state=='WAITING_BROKER_FILL',submission_unknown=state=='UNKNOWN',
            accepted_any=receipt is not None,status=200 if receipt else None,provider='crosstrade',
            reason=error or 'native_oso_accepted_not_filled',broker_ids=receipt,client_id=client,
            qty=quantity,route_id=self.api.route())

    def lifecycle(self,oid):
        if not numeric(oid):raise TransportError('NUMERIC_BROKER_ORDER_ID_REQUIRED')
        self.resolve_contract()
        with self.api.connect() as c:
            cached=c.execute('SELECT payload_json FROM native_lifecycles WHERE binding=? AND broker_id=?',(self.binding,oid)).fetchone()
        if cached:
            prior=json.loads(cached[0]);current=self.read(self.prefix+'/orders/'+str(oid))
            keys=('id','accountId','contractId','action','ordStatus','parentId','ocoId','linkedId')
            if isinstance(current,dict) and current.get('contractId')==self.contract_id and all(current.get(k)==prior['order'].get(k) for k in keys):
                return prior
            with self.api.connect() as c:
                c.execute('DELETE FROM native_lifecycles WHERE binding=? AND broker_id=?',(self.binding,oid))
        data=self.read(self.prefix+'/orders/'+str(oid)+'/lifecycle')
        if (not isinstance(data,dict) or any(not isinstance(data.get(k),dict) for k in ('order','version'))
                or any(not isinstance(data.get(k),list) for k in ('commands','reports'))):
            raise TransportError('COMPLETE_ORDER_LIFECYCLE_REQUIRED')
        order,version=data['order'],data['version']
        if (order.get('id')!=oid or str(order.get('accountId'))!=self.sync.account
                or order.get('contractId')!=self.resolve_contract() or version.get('orderId')!=oid
                or not numeric(version.get('orderQty'))):raise TransportError('BROKER_ORDER_BINDING_MISMATCH')
        if (any(not isinstance(c,dict) for c in data['commands']) or any(not isinstance(r,dict) for r in data['reports'])
                or any(c.get('commandType') not in ('New','Cancel','Modify') for c in data['commands'])):
            raise TransportError('INVALID_NATIVE_COMMAND_TRAIL')
        if order.get('ordStatus') in TERMINAL:
            with self.api.connect() as c:
                c.execute('INSERT OR REPLACE INTO native_lifecycles VALUES(?,?,?,?)',
                          (self.binding,oid,int(self.clock()*1000),encode(data)))
        return data

    def recover_receipt(self,row):
        envelopes=self.read('/orders')
        if not isinstance(envelopes,list):raise TransportError('BROKER_ORDER_LIST_REQUIRED')
        candidates=[]
        for identity in envelopes:
            if not isinstance(identity,dict) or identity.get('environment')!=self.sync.environment:continue
            if not isinstance(identity.get('data'),list):raise TransportError('COMPLETE_IDENTITY_ORDERS_REQUIRED')
            candidates.extend(r for r in identity['data'] if isinstance(r,dict) and str(r.get('accountId'))==self.sync.account)
        candidates=[r for r in candidates if r.get('contractId')==self.resolve_contract()]
        if len(candidates)>20:raise TransportError('UNKNOWN_SUBMISSION_RECOVERY_LIMIT_REQUIRES_REVIEW')
        matches=[]
        for order in candidates:
            if not numeric(order.get('id')):continue
            life=self.lifecycle(order['id'])
            if any(c.get('commandType')=='New' and c.get('clOrdId')==row['client_id'] for c in life['commands']):
                matches.append(order)
        if len(matches)!=1:raise TransportError('UNKNOWN_SUBMISSION_REQUIRES_EXACT_CLIENT_ID')
        entry=matches[0];anchors=[r for r in candidates if r.get('parentId')==entry['id']]
        anchor_ids={r['id'] for r in anchors};peer_ids={r.get('ocoId') for r in anchors}
        children=[r for r in candidates if r['id'] in anchor_ids or r['id'] in peer_ids or r.get('ocoId') in anchor_ids or (numeric(r.get('ocoId')) and r.get('ocoId') in peer_ids)]
        legs={}
        for child in children:
            life=self.lifecycle(child['id']);kind=str(life['version'].get('orderType','')).lower()
            if kind in legs:raise TransportError('AMBIGUOUS_NATIVE_CHILDREN')
            legs[kind]=child
        if (set(legs)!={'limit','stop'} or not oco_linked(legs['limit'],legs['stop'])):
            raise TransportError('NATIVE_BRACKET_IDS_NOT_RECOVERED')
        return dict(orderId=entry['id'],oso1Id=legs['limit']['id'],oso2Id=legs['stop']['id'])

    def fills(self, ids, snapshot, since):
        # The account synchronizer retains session fills across broker resets.
        with self.sync.connect() as c:
            stored=[json.loads(r[0]) for r in c.execute('SELECT payload FROM broker_fills WHERE binding=?',(self.binding,))]
        rows={f['id']:f for f in stored+snapshot['fills'] if f.get('orderId') in ids}
        for f in rows.values():
            if (f.get('contractId')!=self.resolve_contract() or f.get('accountId') is not None and str(f['accountId'])!=self.sync.account
                    or not numeric(f.get('id')) or not numeric(f.get('qty')) or not tick(f.get('price'))
                    or f.get('action') not in ('Buy','Sell') or not since<=stamp(f.get('timestamp'))<=int(snapshot['observed_at']*1000)+1000):
                raise TransportError('OWNED_FILL_ACCOUNT_CONTRACT_OR_TIME_MISMATCH')
        return sorted(rows.values(),key=lambda f:(stamp(f['timestamp']),f['id']))

    def store_resolution(self,row,state,observation,receipt,error=None):
        with self.api.connect() as c:
            c.execute('UPDATE native_orders SET state=?,receipt_json=?,observation_json=?,error=?,updated_ms=? WHERE binding=? AND order_id=?',
                (state,encode(receipt),encode(observation),error,int(self.clock()*1000),self.binding,row['order_id']))
            self.audit(c,row['order_id'],state,observation)

    def reconcile_one(self,row,snapshot):
        command=json.loads(row['command_json']);order=command['order'];receipt=json.loads(row['receipt_json'] or 'null')
        if receipt is None:receipt=self.recover_receipt(row)
        ids=set(receipt.values())
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs={oid:pool.submit(self.lifecycle,oid) for oid in ids}
            lives={oid:job.result() for oid,job in jobs.items()}
        entry=lives[receipt['orderId']]
        if not any(c.get('commandType')=='New' and c.get('clOrdId')==row['client_id'] for c in entry['commands']):
            raise TransportError('ENTRY_CLIENT_ID_NOT_RECONCILED')
        if str(entry['order'].get('action','')).lower()!=command['body']['action']:
            raise TransportError('BROKER_ENTRY_DIRECTION_MISMATCH')
        entry_geometry=(stable_version(entry) and entry['version'].get('orderQty')==command['body']['qty']
                and str(entry['version'].get('orderType','')).lower()=='limit'
                and entry['version'].get('price')==order['entry_price'])
        legs=[lives[receipt[k]] for k in ('oso1Id','oso2Id')]
        parents=[leg['order'].get('parentId') for leg in legs]
        if receipt['orderId'] not in parents or any(parent not in (None,receipt['orderId']) for parent in parents):
            raise TransportError('NATIVE_BRACKET_PARENT_CHAIN_MISMATCH')
        bracket_geometry=True
        for life,kind,price in [(legs[0],'limit',order['target_price']),(legs[1],'stop',order['stop_price'])]:
            if (not oco_linked(legs[0]['order'],legs[1]['order'])
                    or str(life['order'].get('action','')).lower() not in ('buy','sell')
                    or str(life['order'].get('action','')).lower()==command['body']['action']):
                raise TransportError('NATIVE_BRACKET_OWNERSHIP_MISMATCH')
            bracket_geometry=bracket_geometry and stable_version(life) and str(life['version'].get('orderType','')).lower()==kind and life['version'].get('price' if kind=='limit' else 'stopPrice')==price
        close_ids=[]
        with self.api.connect() as c:
            for action in c.execute("SELECT response_json FROM native_actions WHERE binding=? AND order_id=? AND operation='close'",(self.binding,row['order_id'])):
                reply=json.loads(action[0] or '{}')
                value=reply.get('response',{}).get('orderId')
                if numeric(value):close_ids.append(value)
        for close_id in close_ids:
            lives[close_id]=self.lifecycle(close_id)
        ids.update(close_ids)
        fills=self.fills(ids,snapshot,row['created_ms'])
        entry_fills=[f for f in fills if f['orderId']==receipt['orderId']]
        exit_fills=[f for f in fills if f['orderId']!=receipt['orderId']]
        eq=sum(f['qty'] for f in entry_fills);xq=sum(f['qty'] for f in exit_fills)
        entry_status=entry['order'].get('ordStatus')
        native_active=[life['order']['id'] for life in lives.values() if life['order'].get('ordStatus') not in TERMINAL]
        at=int(snapshot['observed_at']*1000)
        with self.api.connect() as c:
            if entry_status in TERMINAL:
                c.execute("UPDATE native_actions SET state='ACKNOWLEDGED',updated_ms=? WHERE binding=? AND order_id=? AND operation='cancel'",(at,self.binding,row['order_id']))
            for child in (receipt['oso1Id'],receipt['oso2Id']):
                if lives[child]['order'].get('ordStatus') in TERMINAL:
                    c.execute("UPDATE native_actions SET state='ACKNOWLEDGED',updated_ms=? WHERE binding=? AND action_id=?",(at,self.binding,'child-cancel-'+str(child)))
        base=dict(order_id=row['order_id'],route_id=order['route_id'],contract=order['contract'],account_label=order['account_label'],
            event_ms=at,source='crosstrade-native-reconciliation',broker_ids=receipt)
        observation=dict(observed_at=snapshot['observed_at'],entry_quantity=eq,exit_quantity=xq,
            entry_status=entry_status,owned_fill_ids=[f['id'] for f in fills],native_active=native_active)
        if not eq:
            # A terminal entry plus no observed fill is insufficient if a late
            # Fill report exists or child/position evidence contradicts it.
            reported=any(str(r.get('reportType','')).lower() in ('fill','partialfill') or r.get('ordStatus') in ('Filled','Completed','PartFilled','PartiallyFilled') for r in entry['reports'])
            if entry_status in ('Canceled','Cancelled','Rejected','Expired') and not reported and not snapshot['positions']:
                if native_active:
                    # Suspended children may survive a rejected/expired parent.
                    # Only its exact children may be cancelled, never a live bracket.
                    for child in native_active:
                        if child not in (receipt['oso1Id'],receipt['oso2Id']):
                            raise TransportError('UNEXPECTED_ACTIVE_NATIVE_ORDER')
                        self.cancel_owned_child(row,child)
                    self.store_resolution(row,'WAITING_CHILD_CANCEL',observation,receipt)
                    return
                previous=json.loads(row['observation_json'] or '{}')
                since=previous.get('no_fill_verified_since_ms',at)
                observation['no_fill_verified_since_ms']=since
                if at-since<15000:
                    self.store_resolution(row,'WAITING_NO_FILL_EVIDENCE',observation,receipt)
                    return
                with self.api.connect() as c:
                    local=c.execute('SELECT state FROM orders WHERE order_id=?',(row['order_id'],)).fetchone()
                if local and local['state']!='NO_FILL':
                    body=dict(base,event='NO_FILL',quantity=0,event_id='ct-nofill-'+row['client_id']+'-'+str(at))
                    self.api.broker_event(body)
                self.store_resolution(row,'NO_FILL',observation,receipt)
            else:self.store_resolution(row,'WAITING_BROKER_FILL',observation,receipt)
            return
        if (eq>command['body']['qty'] or xq>eq or any(f['action'].lower()!=command['body']['action'] for f in entry_fills)
                or any(f['action'].lower()==command['body']['action'] for f in exit_fills)):
            raise TransportError('OWNED_FILL_QUANTITY_OR_DIRECTION_MISMATCH')
        average=sum(Decimal(str(f['price']))*f['qty'] for f in entry_fills)/eq
        remaining=eq-xq
        if remaining:
            own_positions=[p for p in snapshot['positions'] if p.get('contractId')==self.contract_id]
            expected=remaining*(1 if order['direction']=='LONG' else -1)
            if len(own_positions)!=1 or own_positions[0].get('netPos')!=expected or len(snapshot['positions'])!=1:
                raise TransportError('BROKER_POSITION_DOES_NOT_MATCH_OWNED_FILLS')
            if any(f.get('orderId') not in ids and stamp(f.get('timestamp'))>=row['created_ms']
                   and f.get('contractId')==self.contract_id for f in snapshot['fills']):
                raise TransportError('FOREIGN_SAME_CONTRACT_FILL')
            protected=entry_geometry and bracket_geometry and all(life['order'].get('ordStatus')=='Working' and life['version']['orderQty']==remaining for life in legs)
            body=dict(base,event='POSITION',quantity=remaining,entry_price=float(average),fill_ms=min(stamp(f['timestamp']) for f in entry_fills),
                direction=order['direction'],position_id='ct:'+str(receipt['orderId']),exclusive=protected and eq==command['body']['qty'],
                stop_price=order['stop_price'],target_price=order['target_price'],event_id='ct-pos-'+row['client_id']+'-'+str(at),
                protection_verified=protected,assumed=False)
            # Partial fills remain visible in the native journal. Do not turn
            # them into a strategy-owned full position or fabricate an average.
            if eq==command['body']['qty']:
                with self.api.connect() as c:
                    local=c.execute('SELECT state,position_json FROM orders WHERE order_id=?',(row['order_id'],)).fetchone()
                previous=json.loads(local['position_json'] or '{}') if local else {}
                if previous.get('event_ms',-1)<at:
                    self.api.broker_event(body)
            state='OPEN' if protected and eq==command['body']['qty'] and float(average)==order['entry_price'] else 'RECONCILIATION_REQUIRED'
            self.store_resolution(row,state,observation,receipt)
            return
        if snapshot['positions'] or any(o.get('id') in ids for o in snapshot['orders']) or native_active:
            raise TransportError('FLAT_AND_TERMINAL_NATIVE_ORDERS_REQUIRED')
        exit_average=sum(Decimal(str(f['price']))*f['qty'] for f in exit_fills)/xq
        gross=(exit_average-average)*(1 if order['direction']=='LONG' else -1)*eq*2
        observation.update(gross_usd=float(gross),entry_price=float(average),exit_price=float(exit_average),
                           exit_fill_ms=max(stamp(f['timestamp']) for f in exit_fills),quantity=eq)
        # Confirm closure from actual, complete owned fills; net accounting is
        # a separate state until captured costs agree with broker cash.
        with self.api.connect() as c:
            local=c.execute('SELECT state,position_json FROM orders WHERE order_id=?',(row['order_id'],)).fetchone()
        if local and local['state']!='CLOSED':
            prior=json.loads(local['position_json'] or '{}')
            if not prior:
                # A round trip may finish between polls. Record the actual
                # fills atomically; never create a temporary assumed position.
                with self.api.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    closed=dict(base,event='CLOSED',quantity=0,position_id='ct:'+str(receipt['orderId']),assumed=False,
                        entry_price=float(average),fill_ms=min(stamp(f['timestamp']) for f in entry_fills),
                        exit_price=float(exit_average),exit_fill_ms=observation['exit_fill_ms'])
                    c.execute("UPDATE orders SET state='CLOSED',position_json=?,updated_ms=? WHERE order_id=?",(encode(closed),at,row['order_id']))
                    c.execute('INSERT OR IGNORE INTO exit_trades(trade_id,exit_mode,status,updated_ms) VALUES(?,?,?,?)',(row['order_id'],self.api.exit_mode(),'CLOSED',at))
                    c.execute('UPDATE exit_trades SET fill_time=?,fill_price=?,updated_ms=? WHERE trade_id=?',
                              (observation['exit_fill_ms'],float(exit_average),at,row['order_id']))
                    self.api._exit_transition(c,row['order_id'],'CLOSED',at,'broker_owned_round_trip_between_polls')
                    event_id='ct-roundtrip-'+row['client_id']
                    c.execute('INSERT OR IGNORE INTO broker_events VALUES(?,?,?,?,?)',
                              (event_id,hashlib.sha256(encode(closed).encode()).hexdigest(),row['order_id'],at,at))
            else:
                self.api.broker_event(dict(base,event='CLOSED',quantity=0,position_id=prior['position_id'],
                    exit_price=float(exit_average),exit_fill_ms=observation['exit_fill_ms'],broker_order_id=str(close_ids[-1]) if close_ids else None,
                    event_id='ct-close-'+row['client_id']+'-'+str(at)))
        self.store_resolution(row,'CLOSED_PENDING_COSTS',observation,receipt)

    def schedule_reconcile(self,snapshot):
        # Slow broker audit trails must not stall the independent 15s account
        # reader. The reconciliation lock skips overlapping background passes.
        threading.Thread(target=self.reconcile,args=(snapshot,),daemon=True,
                         name='builder-native-reconciliation').start()

    def reconcile(self,snapshot):
        if not self.poll_lock.acquire(blocking=False):return
        try:
            if not 0<=self.clock()-snapshot['observed_at']<=3 or not 0<=snapshot['observed_at']-snapshot['read_started_at']<=12:return
            with self.api.connect() as c:
                rows=[dict(r) for r in c.execute("SELECT * FROM native_orders WHERE binding=? AND state NOT IN ('NO_FILL','CLOSED') ORDER BY created_ms",(self.binding,))]
            for row in rows:
                if row['state']=='CLOSED_PENDING_COSTS':continue
                try:self.reconcile_one(row,snapshot)
                except Exception as exc:
                    error=str(exc) if isinstance(exc,TransportError) else 'BROKER_RECONCILIATION_FAILED'
                    with self.api.connect() as c:
                        c.execute('UPDATE native_orders SET error=?,updated_ms=? WHERE binding=? AND order_id=?',
                            (error,int(self.clock()*1000),self.binding,row['order_id']))
            self.reconcile_costs(snapshot)
            self.drain_exits(snapshot)
        finally:self.poll_lock.release()

    def reconcile_costs(self,snapshot):
        with self.api.connect() as c:
            rows=[dict(r) for r in c.execute("SELECT * FROM native_orders WHERE binding=? AND state IN ('CLOSED_PENDING_COSTS','CLOSED') ORDER BY created_ms",(self.binding,))]
        pending=[r for r in rows if r['state']=='CLOSED_PENDING_COSTS']
        # Revisit the latest closed trade as fees can be revised after capture.
        latest=rows[-1:]
        pending=pending+[r for r in latest if r['state']=='CLOSED']
        if not pending:return
        # Revisions and capture are debounced; they are not a real-time fill feed.
        if self.clock()-getattr(self,'last_cost_poll',0)<60:return
        self.last_cost_poll=self.clock()
        start=min(r['created_ms'] for r in pending)/1000
        params=dict(account=self.sync.name,environment=self.sync.environment,limit=1000,
                    **{'from':datetime.fromtimestamp(start,timezone.utc).isoformat(),'to':datetime.fromtimestamp(self.clock(),timezone.utc).isoformat()})
        history=[];seen=set()
        try:
            for _ in range(5):
                reply=self.request('GET','/fills/history?'+urlencode(params))
                if reply.get('success') is not True or reply.get('partial') or reply.get('unavailable') or not isinstance(reply.get('data'),list) or 'nextCursor' not in reply:
                    return
                history.extend(reply['data']);cursor=reply['nextCursor']
                if cursor is None:break
                if not isinstance(cursor,str) or not cursor or cursor in seen:return
                seen.add(cursor);params['cursor']=cursor
            else:return
        except Exception:return
        by_id={}
        for f in history:
            if not isinstance(f,dict) or str(f.get('accountId'))!=self.sync.account or f.get('accountName')!=self.sync.name or f.get('environment')!=self.sync.environment:return
            if not numeric(f.get('fillId')):return
            if f['fillId'] in by_id and by_id[f['fillId']]!=f:return
            by_id[f['fillId']]=f
        for row in pending:
            observation=json.loads(row['observation_json']);command=json.loads(row['command_json'])
            ids=observation['owned_fill_ids']
            if any(i not in by_id for i in ids):continue
            actual={f['id']:f for f in self.fills(set(json.loads(row['receipt_json']).values())|
                {by_id[i]['orderId'] for i in ids},snapshot,row['created_ms'])}
            costs=[]
            for i in ids:
                f=by_id[i];original=actual.get(i)
                if (not original or f.get('orderId')!=original['orderId'] or f.get('qty')!=original['qty']
                    or f.get('price')!=original['price'] or f.get('action')!=original['action']
                    or f.get('instrument') not in contract_aliases(command['order']['contract'])
                    or stamp(f.get('timestamp'))!=stamp(original['timestamp'])):break
                if any(type(f.get(k)) not in (int,float) or not math.isfinite(f[k]) or f[k]<0 for k in ('commission','fees')):break
                cost=Decimal(str(f['commission']))+Decimal(str(f['fees']))
                if cost<=0:break  # Missing FillFee may appear as zero; do not certify it.
                costs.append(cost)
            if len(costs)!=len(ids):continue
            net=Decimal(str(observation['gross_usd']))-sum(costs)
            unrelated=[f for f in snapshot['fills'] if f['id'] not in set(command['before_fill_ids'])|set(ids)]
            cash=Decimal(str(snapshot['account']['balance']['totalCashValue']))
            if unrelated or snapshot['positions']:continue
            if abs(cash-Decimal(str(command['before_cash']))-net)>Decimal('.01'):
                if row['state']=='CLOSED':self.store_resolution(row,'CLOSED_PENDING_COSTS',observation,json.loads(row['receipt_json']),'BROKER_CASH_OR_COST_REVISION_PENDING')
                continue
            observation.update(net_usd=float(net),captured_cost_usd=float(sum(costs)),costs_reconciled=True,fees_final=False)
            self.store_resolution(row,'CLOSED',observation,json.loads(row['receipt_json']))
            with self.api.connect() as c:
                c.execute('UPDATE exit_trades SET exit_pnl=?,fill_price=?,fill_time=?,updated_ms=? WHERE trade_id=?',
                    (float(net),observation['exit_price'],observation['exit_fill_ms'],int(self.clock()*1000),row['order_id']))

    def resolution(self,oid):
        row=self.row(oid)
        if not row:return None
        observation=json.loads(row['observation_json'] or '{}')
        if row['state']=='NO_FILL':return dict(outcome='no_fill',net=0,R=None,broker_reconciled=True)
        if row['state']=='CLOSED':
            net=observation['net_usd']
            return dict(outcome='win' if net>0 else 'loss' if net<0 else 'timeout',net=net,R=None,broker_reconciled=True)
        if row['state']=='CLOSED_PENDING_COSTS':return dict(outcome='closed_pnl_pending',net=None,R=None,broker_reconciled=False)
        return dict(outcome='open',net=None,R=None,broker_reconciled=False)

    def entry_blocker(self):
        with self.api.connect() as c:
            if c.execute("SELECT 1 FROM native_actions WHERE binding=? AND state IN ('SENDING','UNKNOWN')",(self.binding,)).fetchone():
                return 'broker:native_action_outcome_unknown'
            if c.execute("SELECT 1 FROM native_orders WHERE binding=? AND state NOT IN ('NO_FILL','CLOSED')",(self.binding,)).fetchone():
                return 'broker:native_order_or_cost_reconciliation_pending'
        snapshot=self.sync.snapshot()
        if snapshot:
            with self.api.connect() as c:
                native=list(c.execute('SELECT receipt_json,command_json,observation_json,state FROM native_orders WHERE binding=? ORDER BY created_ms',(self.binding,)))
                close_replies=list(c.execute("SELECT response_json FROM native_actions WHERE binding=? AND operation='close'",(self.binding,)))
            known=set()
            for row in native: known.update(json.loads(row['receipt_json'] or '{}').values())
            for row in close_replies:
                value=json.loads(row[0] or '{}').get('response',{}).get('orderId')
                if numeric(value):known.add(value)
            if any(f.get('orderId') not in known for f in snapshot['fills']):
                return 'broker:unattributed_account_fill_requires_reconciliation'
            if native and native[-1]['state']=='CLOSED':
                last=native[-1];obs=json.loads(last['observation_json']);cmd=json.loads(last['command_json'])
                delta=Decimal(str(snapshot['account']['balance']['totalCashValue']))-Decimal(str(cmd['before_cash']))
                if abs(delta-Decimal(str(obs['net_usd'])))>Decimal('.01'):
                    return 'broker:cash_change_or_fee_revision_requires_reconciliation'
        return None

    def action(self,oid,operation,action_id,*,snapshot=None):
        row=self.row(oid)
        if not row or not row['receipt_json']:raise TransportError('EXACT_OWNED_BROKER_IDS_REQUIRED')
        if snapshot is None:
            if not self.sync.poll(wait=True):raise TransportError('FRESH_BROKER_READ_REQUIRED')
            snapshot=self.sync.snapshot()
        if not 0<=self.clock()-snapshot['observed_at']<=3:raise TransportError('FRESH_BROKER_READ_REQUIRED')
        row=self.row(oid)
        previous=json.loads(row['observation_json'] or '{}')
        if row['error'] or previous.get('observed_at')!=snapshot['observed_at']:
            self.reconcile_one(row,snapshot)
        row=self.row(oid);observation=json.loads(row['observation_json'] or '{}')
        receipt=json.loads(row['receipt_json']);command=json.loads(row['command_json'])
        if operation=='cancel':
            if observation.get('entry_status') in TERMINAL:return dict(success=True,already_terminal=True)
            path=self.prefix+'/orders/'+str(receipt['orderId'])+'/cancel';body={}
        elif operation=='close':
            if not observation.get('entry_quantity') or observation.get('entry_status') not in TERMINAL:
                raise TransportError('TERMINAL_ENTRY_AND_OWNED_POSITION_REQUIRED')
            if observation['entry_quantity']==observation.get('exit_quantity'):
                return dict(success=True,already_flat=True)
            if any(p.get('contractId')!=self.contract_id for p in snapshot['positions']):
                raise TransportError('FOREIGN_POSITION_PRESENT')
            path=self.prefix+'/positions/close';body=dict(instrument=command['body']['instrument'])
        else:raise TransportError('NATIVE_ACTION_NOT_ALLOWED')
        with self.api.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT state FROM native_actions WHERE binding=? AND action_id=?',(self.binding,action_id)).fetchone()
            if old:return dict(success=old['state']=='ACKNOWLEDGED',already_reserved=True)
            if operation=='close':
                if c.execute("SELECT 1 FROM native_actions WHERE binding=? AND order_id=? AND operation='close'",(self.binding,oid)).fetchone():
                    return dict(success=False,already_reserved=True)
                local=c.execute('SELECT state FROM orders WHERE order_id=?',(oid,)).fetchone()
                if not local or local['state'] in ('CLOSED','NO_FILL','CANCELLED','EXPIRED'):
                    raise TransportError('LOCAL_ORDER_NO_LONGER_OPEN')
            c.execute('INSERT INTO native_actions VALUES(?,?,?,?,?,?,?,?)',
                (self.binding,action_id,oid,operation,'SENDING',encode(body),None,int(self.clock()*1000)))
        state='UNKNOWN';reply={}
        try:
            reply=self.request('POST',path,body)
            if (reply.get('success') is True and reply.get('destination')=='tradovate' and reply.get('account')==self.sync.name
                    and reply.get('api')==('cancel_order' if operation=='cancel' else 'liquidate_position') and not reply.get('warning')
                    and (operation=='cancel' or numeric(reply.get('response',{}).get('orderId')))):
                state='ACKNOWLEDGED'
        except Exception:pass
        with self.api.connect() as c:
            c.execute('UPDATE native_actions SET state=?,response_json=?,updated_ms=? WHERE binding=? AND action_id=?',
                (state,encode(reply),int(self.clock()*1000),self.binding,action_id))
            self.audit(c,oid,'ACTION_'+state,dict(action=operation))
        return dict(success=state=='ACKNOWLEDGED',response=reply.get('response',{}))


    def cancel_owned_child(self,row,child):
        aid='child-cancel-'+str(child)
        with self.api.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT 1 FROM native_actions WHERE binding=? AND action_id=?',(self.binding,aid)).fetchone():return
            c.execute('INSERT INTO native_actions VALUES(?,?,?,?,?,?,?,?)',
                      (self.binding,aid,row['order_id'],'child_cancel','SENDING','{}',None,int(self.clock()*1000)))
        reply={};state='UNKNOWN'
        try:
            reply=self.request('POST',self.prefix+'/orders/'+str(child)+'/cancel',{})
            if (reply.get('success') is True and reply.get('account')==self.sync.name
                and reply.get('destination')=='tradovate' and reply.get('api')=='cancel_order' and not reply.get('warning')):
                state='ACKNOWLEDGED'
        except Exception:pass
        with self.api.connect() as c:
            c.execute('UPDATE native_actions SET state=?,response_json=?,updated_ms=? WHERE binding=? AND action_id=?',
                      (state,encode(reply),int(self.clock()*1000),self.binding,aid))

    def queue_exit(self,oid,reason):
        with self.api.connect() as c:
            c.execute('INSERT OR IGNORE INTO native_exit_requests VALUES(?,?,?,?)',
                      (self.binding,oid,reason,int(self.clock()*1000)))

    def drain_exits(self,snapshot):
        with self.api.connect() as c:
            pending=list(c.execute('SELECT order_id FROM native_exit_requests WHERE binding=?',(self.binding,)))
        for request in pending:
            oid=request['order_id'];row=self.row(oid)
            if row and row['state'] in ('NO_FILL','CLOSED','CLOSED_PENDING_COSTS'):
                with self.api.connect() as c:
                    c.execute('DELETE FROM native_exit_requests WHERE binding=? AND order_id=?',(self.binding,oid))
                continue
            try:
                self.action(oid,'cancel','guard-cancel-'+oid,snapshot=snapshot)
                self.action(oid,'close','guard-close-'+oid,snapshot=snapshot)
            except Exception:pass  # Durable request is resumed after the next complete read.


def configure(api):
    global _API,_TRANSPORT
    _API=api
    if selected():
        sync=account_sync.instance()
        if not sync.enabled:raise TransportError('BROKER_ACCOUNT_SYNC_REQUIRED')
        _TRANSPORT=Transport(api,sync)
        sync.after_poll=_TRANSPORT.schedule_reconcile


def route_id():
    return hashlib.sha256(('builder-v3-crosstrade|'+account_sync.instance().binding).encode()).hexdigest()[:16]


def submit(payload,oid):
    if not _TRANSPORT:raise TransportError('NATIVE_TRANSPORT_NOT_CONFIGURED')
    return _TRANSPORT.submit(payload,oid)


def entry_blocker():
    if provider()=='invalid':return 'broker:invalid_order_provider'
    return _TRANSPORT.entry_blocker() if selected() and _TRANSPORT else 'broker:native_transport_not_configured' if selected() else None


def resolution(oid):
    return _TRANSPORT.resolution(oid) if selected() and _TRANSPORT else None


def relay(action,reason='guard'):
    if not _TRANSPORT:return dict(ok=False,error='NATIVE_TRANSPORT_NOT_CONFIGURED')
    with _API.connect() as c:
        rows=c.execute("SELECT order_id FROM native_orders WHERE binding=? AND state NOT IN ('NO_FILL','CLOSED','CLOSED_PENDING_COSTS')",(_TRANSPORT.binding,)).fetchall()
    results=[]
    for row in rows:
        try:
            ident='guard-'+hashlib.sha256((row['order_id']+'|'+action+'|'+reason).encode()).hexdigest()[:40]
            if action=='exit':
                _TRANSPORT.queue_exit(row['order_id'],reason)
                results.append(True)
            else:
                result=_TRANSPORT.action(row['order_id'],'cancel',ident)
                results.append(bool(result.get('success')))
        except Exception:results.append(False)
    return dict(ok=all(results),action=action,status=200 if all(results) else None)


def exit_payload(payload):
    if not _TRANSPORT:raise TransportError('NATIVE_TRANSPORT_NOT_CONFIGURED')
    extras=payload.get('extras',{})
    oid=extras.get('v3OrderId');action_id=extras.get('v3ActionId')
    if not oid or not action_id:raise TransportError('EXACT_LOCAL_EXIT_IDENTITY_REQUIRED')
    reply=_TRANSPORT.action(oid,'close',action_id)
    class Reply:
        status_code=200 if reply.get('success') else 503
        def json(self):return reply
    return Reply()


def status():
    if not selected():return dict(provider=provider(),enabled=False)
    if not _TRANSPORT:return dict(provider='crosstrade',enabled=False,error='NOT_CONFIGURED')
    with _API.connect() as c:
        counts={r[0]:r[1] for r in c.execute('SELECT state,count(*) FROM native_orders WHERE binding=? GROUP BY state',(_TRANSPORT.binding,))}
        pending=[dict(r) for r in c.execute("SELECT order_id,state,error,updated_ms FROM native_orders WHERE binding=? AND state NOT IN ('NO_FILL','CLOSED') ORDER BY created_ms LIMIT 20",(_TRANSPORT.binding,))]
    return dict(provider='crosstrade',enabled=True,route_id=route_id(),orders_by_state=counts,pending=pending,entry_blocker=_TRANSPORT.entry_blocker(),
        note='Accepted native order IDs are not fills. Unknown submissions are reconciled and never resent.')


def request_flatten(reason):
    result=relay('exit',reason)
    # A durable intent is not a broker closure. The next complete poll cancels
    # the owned entry, waits for terminal status, then closes owned fills.
    return result.get('ok',False)

