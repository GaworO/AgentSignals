"""Read-only CrossTrade/Tradovate bridge for the Phase 3A manager.

Order operation plans follow documented REST schemas. Sending ANY management
operation is deliberately impossible here, even if environment flags are armed.
Tests use a separate injected in-memory adapter, never this transport's writer.
"""
import json
from urllib.parse import quote
import builder_crosstrade as native
from directional_position_manager import Manager


def modification_reports(life):
    """Only the latest owned command report can confirm a modification.

    `Success` is Tradovate's non-error rejectReason. AtExecution is used for
    initial OSO activation and is not confirmation of a requested stop change.
    An ACK or a proposed order version alone proves neither acceptance nor fill.
    """
    oid=life['order']['id'];out=[]
    for command in life['commands']:
        cid=command.get('id')
        if (command.get('commandType')!='Modify' or command.get('orderId')!=oid
                or not native.numeric(cid)):continue
        reports=[r for r in life['reports'] if r.get('commandId')==cid
                 and r.get('orderId')==oid and native.numeric(r.get('id'))]
        if not reports:continue
        r=max(reports,key=lambda item:item['id'])
        rejected=(r.get('rejectReason') not in (None,'Success')
                  or r.get('commandStatus')=='Rejected' or command.get('commandStatus')=='Rejected')
        confirmed=(not rejected and command.get('commandStatus')=='Replaced'
                   and r.get('commandStatus')=='Replaced' and r.get('ordStatus')=='Working'
                   and life['order'].get('ordStatus')=='Working'
                   and life['version'].get('id')==cid and life['version'].get('orderId')==oid
                   and native.stable_version(life))
        out.append(dict(command_id=cid,order_id=oid,report_id=r['id'],
                        rejected=rejected,confirmed=bool(confirmed)))
    return out


class CrossTradeManagementAdapter:
    def __init__(self,transport):self.transport=transport

    def require_write(self,operation):
        raise ValueError('PHASE3B_BROKER_MANAGEMENT_NOT_VERIFIED')

    def submit(self,state,intent):
        self.require_write(intent['operation'])

    def operation_plan(self,state,intent):
        """Documentation-level payload; never a permission to submit it."""
        base='/v1/api/tv/accounts/'+quote(self.transport.sync.name,safe='')
        ids=state['frozen']['broker_ids'];op=intent['operation']
        if op in ('STOP','TARGET'):
            oid=ids['oso2Id' if op=='STOP' else 'oso1Id']
            return dict(method='PUT',path=base+'/orders/'+str(oid)+'/change',
                        body={'stopPrice' if op=='STOP' else 'limitPrice':intent['price']},
                        allowed=False,reason='PHASE3B_COMMAND_AND_WORKING_ORDER_VERIFICATION_REQUIRED')
        if op=='QUANTITY':
            return dict(method='PUT',path=base+'/orders/'+str(intent['broker_order_id'])+'/change',
                        body={'qty':intent['quantity']},allowed=False,reason='NO_ATOMIC_LINKED_BRACKET_RESIZE_PROOF')
        if op in ('REDUCE','EXIT'):
            body=dict(instrument=state['contract'])
            if op=='REDUCE':body['qty']=intent['quantity']
            return dict(method='POST',path=base+'/positions/close',body=body,allowed=False,
                        reason='CLOSE_PROTECTION_HANDOFF_AND_RESIZE_UNVERIFIED')
        raise ValueError('UNSUPPORTED_MANAGEMENT_OPERATION')

    def read(self,oid,snapshot=None,*,cached=False):
        t=self.transport;row=t.row(oid)
        if not row:raise ValueError('NATIVE_ORDER_MISSING')
        cmd=json.loads(row['command_json']);order=cmd['order'];ids=json.loads(row['receipt_json'])
        if snapshot is None:
            if not t.sync.poll():raise ValueError('BROKER_REFRESH_FAILED')
            snapshot=t.sync.snapshot()
        with t.api.connect() as c:
            close_ids=[]
            for r in c.execute("SELECT response_json FROM native_actions WHERE binding=? AND order_id=? AND operation='close'",(t.binding,oid)):
                value=json.loads(r[0] or '{}').get('response',{}).get('orderId')
                if type(value) is int and value>0:close_ids.append(value)
        owned_ids=set(ids.values())|set(close_ids)
        lives={}
        with t.api.connect() as c:
            for i in owned_ids:
                if cached:
                    recent=getattr(t,'_ict_lifecycles',{}).get(oid)
                    if recent and abs(snapshot['observed_at']*1000-recent[1])<=12000:
                        lives[i]=recent[0][i];continue
                    raw=c.execute('SELECT payload_json,observed_ms FROM native_lifecycles WHERE binding=? AND broker_id=?',(t.binding,i)).fetchone()
                    if not raw:raise ValueError('BROKER_LIFECYCLE_CACHE_STALE')
                    lives[i]=json.loads(raw[0])
                    if lives[i]['order']['ordStatus'] not in native.TERMINAL:raise ValueError('BROKER_LIFECYCLE_CACHE_STALE')
                else:lives[i]=t.lifecycle(i)
        # Preserve session-reset fills from the existing durable account journal.
        with t.sync.connect() as c:
            retained=[json.loads(r[0]) for r in c.execute('SELECT payload FROM broker_fills WHERE binding=?',(t.binding,))]
        by_id={}
        for f in retained+snapshot['fills']:
            if f.get('orderId') not in owned_ids:continue
            old=by_id.get(f['id'])
            if old and any(old.get(k)!=f.get(k) for k in ('orderId','qty','price','action','timestamp')):raise ValueError('CONFLICTING_BROKER_FILL')
            by_id[f['id']]=f
        normalized=[]
        for f in by_id.values():
            normalized.append(dict(id=f['id'],order_id=f['orderId'],quantity=f['qty'],price=f['price'],
                                   side=1 if f['action'].lower()=='buy' else -1,ms=native.stamp(f['timestamp']),fees=None))
        # Costs are not inferred from balance or invented from an estimate.
        obs=json.loads(row['observation_json'] or '{}')
        positions=[p for p in snapshot['positions'] if p.get('contractId')==t.resolve_contract()]
        position=positions[0] if len(positions)==1 else {}
        legs=[];reports=[]
        for key,kind,field in [('oso1Id','TARGET','price'),('oso2Id','STOP','stopPrice')]:
            life=lives[ids[key]];o,v=life['order'],life['version']
            legs.append(dict(id=o['id'],kind=kind,parent=o.get('parentId'),oco=o.get('ocoId'),
                             quantity=v.get('orderQty'),price=v.get(field),status=o['ordStatus'],
                             side=1 if o.get('action','').lower()=='buy' else -1,confirmed=native.stable_version(life)))
            reports.extend(modification_reports(life))
        # Normalize supported reciprocal/anchor OCO representations only after
        # the original adapter's linkage predicate proves the pair.
        if native.oco_linked(lives[ids['oso1Id']]['order'],lives[ids['oso2Id']]['order']):
            for leg in legs:leg['oco']='native-pair:'+str(ids['oso1Id'])+':'+str(ids['oso2Id'])
        own_entry=lives[ids['orderId']]
        identity_ok=(all(str(l['order'].get('accountId'))==str(t.sync.account) and l['order'].get('contractId')==t.resolve_contract() for l in lives.values())
                     and all(lives[i]['order'].get('action')==('Sell' if order['direction']=='LONG' else 'Buy') for i in close_ids)
                     and any(c.get('clOrdId')==row['client_id'] and c.get('commandType')=='New' for c in own_entry['commands']))
        foreign=(not identity_ok or len(positions)>1 or len(snapshot['positions'])>len(positions)
                 or any(o.get('id') not in owned_ids for o in snapshot['orders'])
                 or any(f.get('orderId') not in owned_ids and f.get('contractId')==t.resolve_contract()
                        and native.stamp(f['timestamp'])>=row['created_ms'] for f in snapshot['fills']))
        return dict(binding=t.binding,contract=order['contract'],observed_ms=int(snapshot['observed_at']*1000),
                    complete=True,foreign_activity=foreign,net_position=position.get('netPos',0),
                    average_entry=position.get('netPrice',position.get('averagePrice',obs.get('entry_price'))),
                    entry_terminal=own_entry['order']['ordStatus'] in native.TERMINAL,
                    fills=normalized,protective_orders=legs,command_reports=reports,
                    owned_exit_order_ids=close_ids if identity_ok else [],
                    external_exit_pending=any(lives[i]['order']['ordStatus'] not in native.TERMINAL for i in close_ids),
                    accounting=dict(native_state=row['state'],observation=obs))


def observe_native(transport,oid,snapshot):
    """Called by the existing reconciler. Observation only; no rule evaluation."""
    row=transport.row(oid)
    if not row or not row['receipt_json']:return
    order=json.loads(row['command_json'])['order']
    if order.get('strategy')!='AB_DIRECTIONAL':return
    manager=Manager(transport.api.connect,transport.binding)
    manager.register(order,json.loads(row['receipt_json']))
    try:
        observed=CrossTradeManagementAdapter(transport).read(oid,snapshot,cached=True)
        return manager.reconcile(oid,observed,int(transport.clock()*1000))
    except (ValueError,KeyError,TypeError) as e:
        # Never leave a previously PROTECTED cache as permission after failure.
        with manager.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            s=manager.get(oid);s.update(state='RECONCILIATION_REQUIRED',reason=str(e),unrealized_r=None)
            manager.save(c,s)
        return s
