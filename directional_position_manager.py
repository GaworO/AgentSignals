"""Phase 3A persistent Directional management foundation.

Broker observations are authoritative. Research decisions and simulated intents
cannot write to a real broker; Phase 3B must supply verified execution capability.
No new entry logic, AI call, or automatic production management rule lives here.
"""
import copy
import hashlib
import json
import math
import directional_ict_context

STATES=('PROTECTED','HOLD','REDUCE_PENDING','RUNNER','EXIT_PENDING','CLOSED','RECONCILIATION_REQUIRED')
TERMINAL={'Filled','Canceled','Cancelled','Rejected','Expired','Completed'}
PENDING={'REQUESTED','ACK','UNKNOWN'}


def encoded(x):return json.dumps(x,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(x):return hashlib.sha256(encoded(x).encode()).hexdigest()
def broker_fingerprint(snapshot):
    # A fresh poll of unchanged facts is not a position change. Its timestamp
    # is validated separately on every reconciliation, including duplicates.
    return digest({k:v for k,v in snapshot.items() if k!='observed_ms'})
def number(x):return type(x) in (int,float) and math.isfinite(x)
def integer(x):return type(x) is int and x>=0
def price(x):return number(x) and x>0 and abs(x/.25-round(x/.25))<1e-8
def fresh(now,at,age):return number(now) and number(at) and number(age) and age>0 and 0<=now-at<=age


class Manager:
    def __init__(self,connect,binding):
        self.connect,self.binding=connect,binding
        with connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS ict_positions(binding TEXT,setup_id TEXT,payload TEXT,
                PRIMARY KEY(binding,setup_id));
            CREATE TABLE IF NOT EXISTS ict_position_events(binding TEXT,setup_id TEXT,event_id TEXT,payload TEXT,
                PRIMARY KEY(binding,setup_id,event_id));
            CREATE TABLE IF NOT EXISTS ict_management_intents(binding TEXT,setup_id TEXT,intent_id TEXT,
                status TEXT,payload TEXT,PRIMARY KEY(binding,intent_id));
            ''')

    def get(self,oid):
        with self.connect() as c:
            row=c.execute('SELECT payload FROM ict_positions WHERE binding=? AND setup_id=?',(self.binding,oid)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self,c,s):
        assert s['state'] in STATES
        c.execute('INSERT OR REPLACE INTO ict_positions VALUES(?,?,?)',(self.binding,s['setup_id'],encoded(s)))

    def register(self,order,ids):
        if order.get('strategy')!='AB_DIRECTIONAL':raise ValueError('DIRECTIONAL_ONLY')
        z={'LONG':1,'SHORT':-1}[order['direction']]
        if (not all(price(order[k]) for k in ('entry_price','stop_price','target_price'))
                or z*(order['entry_price']-order['stop_price'])<=0
                or z*(order['target_price']-order['entry_price'])<=0
                or not integer(order['quantity']) or not order['quantity']):raise ValueError('INVALID_GEOMETRY')
        if (set(ids)!={'orderId','oso1Id','oso2Id'} or any(not integer(i) or not i for i in ids.values())
                or len(set(ids.values()))!=3):raise ValueError('BRACKET_IDS_REQUIRED')
        frozen=dict(order=order,broker_ids=ids)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT payload FROM ict_positions WHERE binding=? AND setup_id=?',(self.binding,order['order_id'])).fetchone()
            if old:
                s=json.loads(old[0])
                if s['frozen']!=frozen:raise ValueError('IMMUTABLE_POSITION_IDENTITY_CHANGED')
                return s
            for row in c.execute('SELECT payload FROM ict_positions WHERE binding=?',(self.binding,)):
                if set(json.loads(row[0])['frozen']['broker_ids'].values()) & set(ids.values()):
                    raise ValueError('BROKER_ORDER_ALREADY_OWNED_BY_ANOTHER_SETUP')
            risk=abs(order['entry_price']-order['stop_price'])
            context=directional_ict_context.frozen_context(
                dict(order,activation_ms=order.get('activation_ms',0)),{'v3_snapshot':order.get('snapshot') or {}})
            for node in context.values():
                node['source']['path']=node['source']['path'].replace('payload.v3_snapshot','native_order.snapshot')
            context['structural_invalidation']=dict(status='UNKNOWN',value=None,available_at_ms=None,
                reason='Protective SL is recorded separately; an ICT invalidation rule is not inferred from it.')
            s=dict(setup_id=order['order_id'],binding=self.binding,frozen=frozen,direction=order['direction'],
                contract=order['contract'],original_quantity=order['quantity'],remaining_quantity=None,
                initial_sl=order['stop_price'],initial_tp=order['target_price'],initial_risk_points=risk,
                initial_risk_usd=risk*2*order['quantity'],average_entry=None,unrealized_r=None,
                favorable_high=None,favorable_low=None,observed_mfe_r=None,observed_mae_r=None,
                excursion_source='CLOSED_BARS_OBSERVED_ONLY',
                ict_levels=context,fills={},fees=None,realized_gross_usd=None,
                realized_net_usd=None,protective_orders=[],observed_ms=None,broker_fingerprint=None,
                state='RECONCILIATION_REQUIRED',reason='AWAITING_BROKER',partial_done=False,runner=False,
                last_stop=order['stop_price'],last_target=order['target_price'],entry_terminal=False)
            self.save(c,s);return s

    def reconcile(self,oid,snapshot,now):
        """Normalized complete snapshot. No local position overwrite of broker."""
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT payload FROM ict_positions WHERE binding=? AND setup_id=?',(self.binding,oid)).fetchone()
            if not row:raise ValueError('UNREGISTERED_SETUP')
            s=json.loads(row[0]);event=digest(snapshot)
            if c.execute('SELECT 1 FROM ict_position_events WHERE binding=? AND setup_id=? AND event_id=?',(self.binding,oid,event)).fetchone():
                if not fresh(now,snapshot.get('observed_ms'),3000):
                    s.update(state='RECONCILIATION_REQUIRED',reason='BROKER_SNAPSHOT_STALE_OR_INCOMPLETE',unrealized_r=None)
                    self.save(c,s)
                return s
            if s['observed_ms'] is not None and number(snapshot.get('observed_ms')) and snapshot['observed_ms']<s['observed_ms']:return s
            before=copy.deepcopy(s)
            c.execute('SAVEPOINT broker_packet')
            try:
                self._reconcile(c,s,snapshot,now)
            except (ValueError,KeyError,TypeError,OverflowError) as exc:
                # A packet must validate as a whole: never retain a confirmed
                # stop/intent from a packet whose other leg failed validation.
                c.execute('ROLLBACK TO broker_packet');s=before
                s.update(state='RECONCILIATION_REQUIRED',reason=str(exc),unrealized_r=None)
            c.execute('RELEASE broker_packet')
            c.execute('INSERT INTO ict_position_events VALUES(?,?,?,?)',(self.binding,oid,event,encoded(snapshot)))
            self.save(c,s);return s

    def _reconcile(self,c,s,b,now):
        if b['binding']!=self.binding or b['contract']!=s['contract']:raise ValueError('ACCOUNT_OR_CONTRACT_MISMATCH')
        if b.get('complete') is not True or not fresh(now,b.get('observed_ms'),3000):raise ValueError('BROKER_SNAPSHOT_STALE_OR_INCOMPLETE')
        if b.get('foreign_activity'):raise ValueError('UNEXPECTED_MANUAL_INTERVENTION')
        ids=s['frozen']['broker_ids'];z=1 if s['direction']=='LONG' else -1
        intents=[json.loads(r[0]) for r in c.execute('SELECT payload FROM ict_management_intents WHERE binding=? AND setup_id=?',(self.binding,s['setup_id']))]
        pending=[i for i in intents if i['status'] in PENDING]
        extra_ids={i['broker_order_id'] for i in intents if i.get('broker_order_id')}
        # IDs from the original native close journal, identity-checked by the
        # read adapter, also own fills (existing Guard/session/Boundary exits).
        native_close_ids=b.get('owned_exit_order_ids',[])
        if any(not integer(i) or not i for i in native_close_ids):raise ValueError('INVALID_NATIVE_CLOSE_ID')
        extra_ids.update(native_close_ids)
        fills=dict(s['fills'])
        for f in b['fills']:
            if (f['order_id'] not in set(ids.values())|extra_ids or f['ms']>b['observed_ms']
                    or not integer(f['quantity']) or not f['quantity'] or not price(f['price'])):raise ValueError('UNOWNED_OR_INVALID_FILL')
            old=fills.get(str(f['id']))
            if old and any(old[k]!=f[k] for k in ('id','order_id','quantity','price','side','ms')):raise ValueError('CONFLICTING_FILL')
            fills[str(f['id'])]=f
        entries=[f for f in fills.values() if f['order_id']==ids['orderId']]
        exits=[f for f in fills.values() if f['order_id']!=ids['orderId']]
        if any(f['side']!=z for f in entries) or any(f['side']!=-z for f in exits):raise ValueError('FILL_DIRECTION_MISMATCH')
        eq=sum(f['quantity'] for f in entries);xq=sum(f['quantity'] for f in exits);remaining=eq-xq
        if not 0<=remaining<=eq<=s['original_quantity']:raise ValueError('FILL_QUANTITY_MISMATCH')
        if b['net_position']!=z*remaining:raise ValueError('BROKER_LOCAL_POSITION_MISMATCH')
        avg=sum(f['price']*f['quantity'] for f in entries)/eq if eq else None
        if remaining and (not number(b['average_entry']) or abs(avg-b['average_entry'])>1e-7):raise ValueError('AVERAGE_ENTRY_MISMATCH')
        legs=b['protective_orders']
        working=[l for l in legs if l['status']=='Working']
        reports={r['command_id']:r for r in b.get('command_reports',[])}
        rejected=[]
        for i in pending:
            r=reports.get(i.get('command_id'))
            expected=ids.get('oso2Id' if i['operation']=='STOP' else 'oso1Id')
            if r and r.get('order_id',expected)!=expected:raise ValueError('COMMAND_REPORT_ORDER_MISMATCH')
            if r and r.get('rejected'):
                i.update(status='REJECTED',reason='BROKER_COMMAND_REJECTED');self._intent_save(c,i)
                rejected.append(i)
        pending=[i for i in pending if i['status'] in PENDING]
        if remaining:
            if len(working)!=2 or set(l['id'] for l in working)!={ids['oso1Id'],ids['oso2Id']}:raise ValueError('PROTECTIVE_ORDER_LINKAGE_UNKNOWN')
            if {l['kind'] for l in working}!={'STOP','TARGET'}:raise ValueError('PROTECTIVE_ORDER_TYPES_UNKNOWN')
            stop=next(l for l in working if l['kind']=='STOP');target=next(l for l in working if l['kind']=='TARGET')
            if (stop['id']!=ids['oso2Id'] or target['id']!=ids['oso1Id'] or not stop.get('oco')
                    or stop['oco']!=target.get('oco') or ids['orderId'] not in (stop.get('parent'),target.get('parent'))
                    or any(l.get('parent') not in (None,ids['orderId']) or l['side']!=-z or l['quantity']!=remaining
                           or l.get('confirmed') is not True or not price(l['price']) for l in working)):
                raise ValueError('PROTECTION_QUANTITY_OR_LINK_MISMATCH')
            if z*(stop['price']-s['last_stop'])<0:raise ValueError('STOP_LOOSENED')
            allowed_change={i['operation']:i for i in pending if i.get('command_id') in reports and reports[i['command_id']].get('confirmed') is True}
            for op,leg,key in [('STOP',stop,'last_stop'),('TARGET',target,'last_target')]:
                if leg['price']!=s[key]:
                    i=allowed_change.get(op)
                    if not i or leg['price']!=i['price']:raise ValueError('UNEXPECTED_MANUAL_ORDER_CHANGE')
                    i['status']='CONFIRMED';self._intent_save(c,i);s[key]=leg['price']
                    if op=='TARGET':s['runner']=True
            risk=max(0,z*(avg-stop['price']))*remaining*2
            if risk>s['initial_risk_usd']+1e-7:raise ValueError('TOTAL_RISK_INCREASED')
            s['current_stop_risk_usd']=risk
        elif any(l['status'] not in TERMINAL for l in legs) or not b['entry_terminal'] or b.get('external_exit_pending'):
            raise ValueError('FLAT_WITH_WORKING_ORDERS')
        for i in pending:
            if i['operation']=='REDUCE' and remaining==i['remaining_before']-i['quantity'] and xq>i['exit_quantity_before']:
                if not any(f['order_id']==i.get('broker_order_id') for f in exits):raise ValueError('REDUCTION_FILL_UNBOUND')
                i['status']='CONFIRMED';self._intent_save(c,i);s['partial_done']=True
            if i['operation']=='EXIT' and not remaining and eq:
                i['status']='CONFIRMED';self._intent_save(c,i)
        unfinished=[json.loads(r[0]) for r in c.execute("SELECT payload FROM ict_management_intents WHERE binding=? AND setup_id=? AND status IN ('REQUESTED','ACK','UNKNOWN')",(self.binding,s['setup_id']))]
        fees=sum(f['fees'] for f in fills.values()) if fills and all(number(f.get('fees')) and f['fees']>=0 for f in fills.values()) else None
        gross=sum(z*(f['price']-avg)*f['quantity']*2 for f in exits) if avg else None
        accounting=b.get('accounting',{})
        evidence=accounting.get('observation',{})
        if not remaining and eq and accounting.get('native_state')=='CLOSED' and evidence.get('costs_reconciled') is True:
            if (number(evidence.get('captured_cost_usd')) and evidence['captured_cost_usd']>=0
                    and number(evidence.get('gross_usd')) and abs(evidence['gross_usd']-gross)<.01
                    and number(evidence.get('net_usd')) and abs(evidence['net_usd']-(gross-evidence['captured_cost_usd']))<.01):
                fees=evidence['captured_cost_usd']
        state='CLOSED' if eq and not remaining else 'RUNNER' if s['runner'] else 'PROTECTED'
        if not eq:state='RECONCILIATION_REQUIRED'
        if unfinished:state='REDUCE_PENDING' if unfinished[0]['operation']=='REDUCE' else 'EXIT_PENDING' if unfinished[0]['operation']=='EXIT' else 'HOLD'
        if remaining and b.get('external_exit_pending'):state='EXIT_PENDING'
        if rejected:state='RECONCILIATION_REQUIRED'
        s.update(state=state,reason='BROKER_COMMAND_REJECTED' if rejected else 'BROKER_CONFIRMED' if eq else 'AWAITING_FILL',fills=fills,
                 remaining_quantity=remaining,entry_quantity=eq,exit_quantity=xq,average_entry=avg,unrealized_r=None,
                 realized_gross_usd=gross,fees=fees,realized_net_usd=gross-fees if fees is not None and gross is not None else None,
                 protective_orders=legs,observed_ms=b['observed_ms'],entry_terminal=b['entry_terminal'],broker_fingerprint=broker_fingerprint(b))

    def _intent_save(self,c,i):
        c.execute('INSERT OR REPLACE INTO ict_management_intents VALUES(?,?,?,?,?)',
                  (self.binding,i['setup_id'],i['id'],i['status'],encoded(i)))

    def on_event(self,oid,event,quote,risk,options,now,*,max_quote_age_ms,max_event_age_ms):
        """Research proposals only. Undefined options stay disabled; AI is not input."""
        s=self.get(oid)
        if not s:raise ValueError('UNREGISTERED_SETUP')
        if s['state'] not in ('PROTECTED','HOLD','RUNNER') or not s['remaining_quantity']:return dict(action='BLOCK',reason=s['state'])
        if not s['entry_terminal']:return dict(action='HOLD',reason='ENTRY_STILL_WORKING')
        if (event.get('confirmed') is not True or not event.get('source') or event.get('contract')!=s['contract']
                or not fresh(now,event.get('closed_ms'),max_event_age_ms)
                or event['closed_ms']<=0 or not number(event.get('open_ms')) or event['open_ms']>=event['closed_ms']
                or event['closed_ms']<s.get('last_event',{}).get('closed_ms',0)):
            return dict(action='BLOCK',reason='CLOSED_CAUSAL_EVENT_REQUIRED')
        if (not number(max_quote_age_ms) or max_quote_age_ms<=0 or quote.get('execution_eligible') is not True
                or quote.get('timestamp_semantics_verified') is not True or quote.get('contract')!=s['contract']
                or not fresh(now,quote.get('as_of_ms'),max_quote_age_ms) or not price(quote.get('bid'))
                or not price(quote.get('ask')) or quote['bid']>quote['ask']):return dict(action='BLOCK',reason='EXECUTABLE_QUOTE_REQUIRED')
        if risk.get('allowed') is not True or risk.get('binding')!=self.binding or not fresh(now,risk.get('checked_ms'),3000):
            return dict(action='BLOCK',reason='ACCOUNT_RISK_UNKNOWN')
        if not fresh(now,s['observed_ms'],3000):return dict(action='BLOCK',reason='BROKER_RECONCILIATION_REQUIRED')
        z=1 if s['direction']=='LONG' else -1;px=quote['bid'] if z==1 else quote['ask']
        action=dict(action='HOLD',reason='NO_ENABLED_RULE')
        def level(name):
            x=options.get(name,{})
            return x if (x.get('enabled') is True and x.get('source') not in (None,'','UNKNOWN') and price(x.get('price'))
                         and number(x.get('known_ms')) and number(x.get('expires_ms'))
                         and 0<x.get('known_ms',0)<=event['closed_ms']<=now<=x.get('expires_ms',0)) else None
        invalid=level('invalidation');partial=level('partial');runner=level('runner');trail=level('trail');be=level('break_even')
        close=event.get('close')
        if not price(close):return dict(action='BLOCK',reason='CLOSED_PRICE_REQUIRED')
        if invalid and z*(close-invalid['price'])<0:action=dict(action='EXIT',reason='STRUCTURAL_INVALIDATION')
        elif options.get('session_end',{}).get('enabled') is True and number(options['session_end'].get('at_ms')) and now>=options['session_end']['at_ms']:
            action=dict(action='EXIT',reason='SESSION_END')
        elif partial and not s['partial_done'] and integer(partial.get('quantity')) and 0<partial['quantity']<s['remaining_quantity'] and z*(px-partial['price'])>=0:
            action=dict(action='REDUCE',quantity=partial['quantity'],reason='RESEARCH_TP1')
        elif runner and s['partial_done'] and not s['runner'] and z*(runner['price']-s['last_target'])>0:
            action=dict(action='TARGET',price=runner['price'],reason='RESEARCH_RUNNER_LIQUIDITY')
        elif trail and event.get('kind')=='CONFIRMED_SWING' and trail['known_ms']==event['closed_ms']:
            action=dict(action='STOP',price=trail['price'],reason='RESEARCH_CONFIRMED_SWING')
        elif be and z*(px-be['price'])>=0:
            action=dict(action='STOP',price=s['average_entry'],reason='RESEARCH_BREAK_EVEN_NOT_COST_NEUTRAL')
        if action['action']=='STOP' and (not price(action['price']) or z*(action['price']-s['last_stop'])<=0 or z*(px-action['price'])<=0):
            action=dict(action='BLOCK',reason='STOP_MUST_TIGHTEN_AND_REMAIN_BEHIND_MARKET')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            fresh_state=json.loads(c.execute('SELECT payload FROM ict_positions WHERE binding=? AND setup_id=?',(self.binding,oid)).fetchone()[0])
            if fresh_state!=s:return dict(action='BLOCK',reason='POSITION_CHANGED_DURING_DECISION')
            s['unrealized_r']=z*(px-s['average_entry'])/s['initial_risk_points']
            # Closed bar excursions are observations, never simulated fills.
            if event['open_ms']>=min(f['ms'] for f in s['fills'].values()) and price(event.get('high')) and price(event.get('low')):
                s['favorable_high']=max(s['favorable_high'] or s['average_entry'],event['high'])
                s['favorable_low']=min(s['favorable_low'] or s['average_entry'],event['low'])
                extremes=[z*(p-s['average_entry'])/s['initial_risk_points'] for p in (s['favorable_high'],s['favorable_low'])]
                s['observed_mfe_r']=max(extremes);s['observed_mae_r']=min(extremes)
            s['last_event']=dict(event);s['last_proposal']=action
            record=dict(kind='RESEARCH_DECISION',event=event,quote=quote,risk=risk,options=options,
                        action=action,decision_ms=now,broker_fingerprint=s['broker_fingerprint'])
            c.execute('INSERT OR IGNORE INTO ict_position_events VALUES(?,?,?,?)',
                      (self.binding,oid,digest(record),encoded(record)))
            if action['action']=='HOLD' and not s['runner']:s['state']='HOLD'
            self.save(c,s)
        return dict(action,setup_id=oid,broker_fingerprint=s['broker_fingerprint'],decision_ms=now,
                    event=event,quote=quote,risk=risk,options=options,max_quote_age_ms=max_quote_age_ms,max_event_age_ms=max_event_age_ms)

    def request(self,oid,proposal,adapter,now):
        """Durably reserve before simulated IO. Unknown outcome is never resent."""
        # Production adapters unconditionally refuse management writes in 3A.
        adapter.require_write(proposal['action'])
        if proposal.get('setup_id')!=oid:raise ValueError('PROPOSAL_SETUP_MISMATCH')
        fresh=adapter.read(oid);self.reconcile(oid,fresh,now)
        s=self.get(oid)
        if s['broker_fingerprint']!=proposal['broker_fingerprint'] or s['state'] not in ('PROTECTED','HOLD','RUNNER'):
            raise ValueError('POSITION_CHANGED_BEFORE_REQUEST')
        latest=self.on_event(oid,proposal['event'],proposal['quote'],proposal['risk'],proposal['options'],now,
                             max_quote_age_ms=proposal['max_quote_age_ms'],max_event_age_ms=proposal['max_event_age_ms'])
        if any(latest.get(k)!=proposal.get(k) for k in ('action','price','quantity')):raise ValueError('PROPOSAL_EXPIRED_OR_CHANGED')
        key=digest(dict(setup=oid,operation=proposal['action'],event=proposal['event'],price=proposal.get('price'),quantity=proposal.get('quantity')))
        intent=dict(id=key,setup_id=oid,operation=proposal['action'],status='REQUESTED',created_ms=now,
                    remaining_before=s['remaining_quantity'],exit_quantity_before=s['exit_quantity'],
                    **{k:proposal[k] for k in ('price','quantity') if k in proposal})
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT payload FROM ict_management_intents WHERE binding=? AND intent_id=?',(self.binding,key)).fetchone()
            if old:return json.loads(old[0])
            if c.execute("SELECT 1 FROM ict_management_intents WHERE binding=? AND setup_id=? AND status IN ('REQUESTED','ACK','UNKNOWN')",(self.binding,oid)).fetchone():raise ValueError('MANAGEMENT_ACTION_PENDING')
            current=json.loads(c.execute('SELECT payload FROM ict_positions WHERE binding=? AND setup_id=?',(self.binding,oid)).fetchone()[0])
            if (current['broker_fingerprint']!=proposal['broker_fingerprint']
                    or current['state'] not in ('PROTECTED','HOLD','RUNNER')):raise ValueError('POSITION_CHANGED_BEFORE_REQUEST')
            self._intent_save(c,intent)
            current['state']='REDUCE_PENDING' if intent['operation']=='REDUCE' else 'EXIT_PENDING' if intent['operation']=='EXIT' else 'HOLD';self.save(c,current)
        try:
            reply=adapter.submit(s,intent)
            if reply.get('success') is not True:raise ValueError('BROKER_REJECTED_REQUEST')
            if not integer(reply.get('command_id')) or not reply['command_id']:raise ValueError('ACK_WITHOUT_COMMAND_ID')
            intent.update(status='ACK',command_id=reply['command_id'],broker_order_id=reply.get('broker_order_id'))
        except Exception:
            intent.update(status='UNKNOWN',reason='REQUEST_OUTCOME_UNKNOWN_NO_RETRY')
        with self.connect() as c:self._intent_save(c,intent)
        return intent
