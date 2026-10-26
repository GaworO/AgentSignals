"""Recovery integration in the existing AI worker and protected entry route.

Disabled by default. Never retries a reserved AI call or uncertain dispatch.
All broker writes remain owned by builder_crosstrade and the original Guard.
"""
import copy
import json
import time
import threading
import directional_nofill_recovery as journal
import directional_recovery_plans as plans
import directional_recovery_review as review

_LOCK = threading.Lock()


def capture(raw, candidate):
    """Freeze original scanner anchors by physical instrument and closed timestamp.

    hh_bar is a confirmation index, NOT necessarily the extreme candle. The
    causal scanner's extreme is the max/min from rejection origin through BOS.
    Missing proof disables recovery only; original detection is unchanged.
    """
    try:
        origin, bos = int(candidate['origin_bar']), int(candidate['bos_bar'])
        gap = int(candidate.get('sfvg_bar') if candidate.get('kind') == 'FVG'
                  and candidate.get('sfvg_bar') is not None else candidate['fvg_bar'])
        if not 0 <= origin <= bos < len(raw) or not 2 <= gap < len(raw):
            raise ValueError('scanner_indices_unknown')
        side=candidate['dir']; iid=int(candidate['instrument_id'])
        frame=raw.iloc[origin:bos+1]
        field='high' if side=='LONG' else 'low'
        values=frame[field].to_numpy()
        extreme=origin+int(values.argmax() if side=='LONG' else values.argmin())
        def bar(i):
            r=raw.iloc[i];at=int(r.ts_event.value//1000000)
            if int(r.instrument_id)!=iid or at+60000>candidate['entry_ms']:
                raise ValueError('scanner_reference_not_causal')
            return dict(time=at//1000,available_at=at//1000+60,timeframe=1,
                        instrument_id=iid,**{k:float(r[k]) for k in ('open','high','low','close')})
        rows=[bar(i) for i in range(gap-2,gap+1)]
        if any(b['time']!=a['available_at'] for a,b in zip(rows,rows[1:])):
            raise ValueError('scanner_gap_not_contiguous')
        return dict(status='RECORDED',source='ORIGINAL_DIRECTIONAL_SCANNER',
                    origin=bar(origin),extreme=bar(extreme),fvg=rows,
                    bos_ms=int(candidate['bos_ms']),direction=side,instrument_id=iid)
    except (KeyError,ValueError,TypeError,IndexError,AttributeError):
        return dict(status='UNKNOWN',reason='SCANNER_RECOVERY_ANCHORS_UNAVAILABLE')


def ref(contract, at):return f'{contract}:M1:{at}'


def original_record(order, context):
    value=plans.original_view(order)
    if context.get('status')!='RECORDED':raise ValueError('SCANNER_RECOVERY_ANCHORS_UNAVAILABLE')
    sign=1 if order['direction']=='LONG' else -1
    value['recovery_context']=copy.deepcopy(context)
    value['recovery_anchors']=dict(order_id=order['order_id'])
    for name in ('origin','extreme'):
        b=context[name];field=('low' if sign==1 else 'high') if name=='origin' else ('high' if sign==1 else 'low')
        value['recovery_anchors'][name]=dict(candle_id=ref(order['contract'],b['time']),price=b[field])
    return value


def register_prepared(order, source):
    import ab_ai_context
    service=ab_ai_context.INSTANCE
    if not service or not journal.enabled(service.config) or order.get('recovery_version'):return
    context=source.get('recovery_context',{})
    try:
        original=original_record(order,context)
        with service.connect() as db:
            journal.init(db);journal.register(db,original,int(service.clock()*1000))
    except ValueError as exc:
        service.event(order['order_id'],'recovery_registration_blocked',reason=str(exc))


def market_packet(service, packet, original):
    """Augment the genuine market packet with authenticated physical MNQ history."""
    import ab_v3_live
    p=copy.deepcopy(packet);contract=original['contract'];ctx=original['recovery_context']
    start=min(b['time'] for b in [ctx['origin'],ctx['extreme'],*ctx['fvg']])*1000
    with ab_v3_live.connect() as db:
        rows=db.execute("SELECT ts_ms,bar_json,received_ms FROM bars WHERE tf='M1' AND contract=? AND ts_ms>=? AND ts_ms+60000<=? ORDER BY ts_ms",
                        (contract,start,p['as_of']*1000)).fetchall()
    for r in rows:
        b=json.loads(r['bar_json']);at=r['ts_ms']//1000
        if r['received_ms']>int(service.clock()*1000):raise ValueError('FUTURE_FEED_RECEIPT')
        p['evidence'][ref(contract,at)]=dict(kind='bar',symbol='MNQ',contract=contract,timeframe=1,
            time=at,available_at=at+60,source='AUTHENTICATED_TV_CLOSED_M1',
            **{k:b[k] for k in ('open','high','low','close')})
    for b in [ctx['origin'],ctx['extreme'],*ctx['fvg']]:
        current=p['evidence'].get(ref(contract,b['time']),{})
        if any(current.get(k)!=b[k] for k in ('time','available_at','open','high','low','close')):
            raise ValueError('SCANNER_REFERENCE_MISSING_OR_CHANGED')
    # Export only causal, eligible events actually emitted by the original
    # scanner. Outcome/fill columns never enter the model packet.
    import continuation_shadow
    with continuation_shadow._connect() as db:
        events=db.execute("SELECT candidate_id,payload_json FROM continuation_candidates WHERE strategy='AB_DIRECTIONAL' AND eligible=1 AND direction=? AND decision_ms>? AND decision_ms<=?",
                          (original['direction'],original['activation_ms'],p['as_of']*1000)).fetchall()
    for item in events:
        recorded=json.loads(item['payload_json']);context=recorded.get('recovery_context',{})
        rules=recorded.get('entry_rules',{})
        if (context.get('status')!='RECORDED' or rules.get('eligible') is not True
                or context.get('instrument_id')!=ctx.get('instrument_id')):continue
        refs=[ref(contract,b['time']) for b in context['fvg']]
        swing_ref=ref(contract,context['origin']['time'])
        if any(k not in p['evidence'] for k in [*refs,swing_ref]):continue
        bos_closed=context['bos_ms']//1000+60
        if bos_closed<=original['activation_ms']//1000:continue
        event_id='RECOVERY_SCANNER:'+item['candidate_id']
        p['evidence'][event_id]=dict(kind='BOS',symbol='MNQ',contract=contract,
            direction=original['direction'],source='ORIGINAL_DIRECTIONAL_SCANNER',
            available_at=max(bos_closed,context['fvg'][-1]['available_at']),fvg_candle_ids=refs,
            bos_closed_at=bos_closed,confirmed_swing_candle_id=swing_ref,
            structural_source_candle_id=swing_ref,displacement_confirmed=True,
            evidence_ids=[*refs,swing_ref],scanner_entry_rules=rules)
    return p


def selections(original, packet, *, stop_buffer_points=.25):
    ctx=original['recovery_context'];contract=original['contract']
    first=[ref(contract,b['time']) for b in ctx['fvg']]
    result=[dict(kind='FIBO_50_LIMIT',fvg_candle_ids=first)]
    # Choose the nearest deeper *valid* gap deterministically; AI still judges it.
    rows=sorted((e for k,e in packet['evidence'].items() if k==ref(contract,e.get('time'))
                 and e.get('contract')==contract and e.get('timeframe')==1),key=lambda b:b['time'])
    deeper=[]
    for a,b,c in zip(rows,rows[1:],rows[2:]):
        s=dict(kind='DEEP_FVG_LIMIT',fvg_candle_ids=[ref(contract,x['time']) for x in (a,b,c)])
        built=plans.build(original,packet,s,packet['as_of']*1000,stop_buffer_points=stop_buffer_points)
        if built['status']=='ELIGIBLE_FOR_AI':deeper.append((abs(built['plan']['entry_price']-original['entry_price']),s))
    result.append(min(deeper,key=lambda pair:pair[0])[1] if deeper else dict(kind='DEEP_FVG_LIMIT',fvg_candle_ids=first))
    # Only real scanner evidence, never synthesize a BOS from a price breakout.
    events=[(k,e) for k,e in packet['evidence'].items() if e.get('source')=='ORIGINAL_DIRECTIONAL_SCANNER'
            and e.get('kind') in ('BOS','MSS') and e.get('direction')==original['direction']
            and original['activation_ms']//1000<e.get('available_at',0)<=packet['as_of']]
    if events:
        key,event=max(events,key=lambda item:item[1]['available_at'])
        result.append(dict(kind='NEW_BOS_FVG_RETEST',fvg_candle_ids=event.get('fvg_candle_ids'),
                           source_policy=plans.SWING_POLICY,structure_event_id=key))
    else:result.append(dict(kind='NEW_BOS_FVG_RETEST',fvg_candle_ids=first))
    return result


def broker_proof(oid):
    import builder_crosstrade
    t=builder_crosstrade._TRANSPORT
    if t is None or not t.sync.poll(wait=True):raise ValueError('BROKER_REFRESH_UNAVAILABLE')
    if not t.poll_lock.acquire(blocking=False):raise ValueError('BROKER_RECONCILIATION_BUSY')
    try:
        snapshot=t.sync.snapshot();row=t.row(oid)
        if not row:raise ValueError('ORIGINAL_NATIVE_INTENT_MISSING')
        t.reconcile_one(row,snapshot)  # Existing cancellation/protection ownership rules.
        row=t.row(oid);obs=json.loads(row['observation_json'] or '{}')
        ids=json.loads(row['receipt_json'] or '{}')
        return dict(order_id=oid,observed_ms=int(obs.get('observed_at',0)*1000),
            verified=row['state']=='NO_FILL' and not row.get('error') and obs.get('native_active')==[],
            entry_quantity=obs.get('entry_quantity'),entry_status=obs.get('entry_status'),state=row['state'],
            positions=snapshot['positions'],working_orders=snapshot['orders'],broker_ids=ids,
            no_fill_verified_since_ms=obs.get('no_fill_verified_since_ms'))
    finally:t.poll_lock.release()


def annotation(service, oid, direction):
    unavailable=dict(owner='AB_DIRECTIONAL',order_id=oid,state='UNAVAILABLE',executable=False)
    if not journal.enabled(service.config):return unavailable
    with service.connect() as db:
        journal.init(db)
        row=db.execute('SELECT * FROM nofill_opportunities WHERE replacement_id=?',(oid,)).fetchone()
        if not row:return unavailable
        result=db.execute('SELECT * FROM nofill_review_results WHERE review_id=?',(row['review_id'],)).fetchone()
    if not result or result['status']!='SELECTED' or row['state'] not in ('SELECTED','REPLACEMENT_RESERVED','REPLACEMENT_SUBMITTED'):return unavailable
    plan=json.loads(result['selected_plan']);plans.verify_plan(plan)
    if plan['direction']!=direction:return dict(unavailable,state='SETUP_MISMATCH')
    if not plan['activation_ms']<=service.clock()*1000<plan['expiry_ms']:return dict(unavailable,state='EXPIRED')
    return dict(unavailable,state='ALLOW',setup=plan,review_id=row['review_id'],cutoff=plan['activation_ms']//1000,
                available_at=result['received_ms']/1000,expires_at=plan['expiry_ms']/1000,recovery=True)


def validate_entry(service, order, *, pending=False):
    """Fresh thesis and immutable AI binding, used by the existing final gate."""
    if not journal.enabled(service.config):raise ValueError('NO_FILL_RECOVERY_DISABLED')
    with service.connect() as db:
        journal.init(db)
        row=db.execute('SELECT * FROM nofill_opportunities WHERE replacement_id=?',(order['order_id'],)).fetchone()
        if not row:raise ValueError('RECOVERY_NOT_REGISTERED')
        saved=db.execute('SELECT request FROM reviews WHERE id=?',(row['review_id'],)).fetchone()
        output=db.execute('SELECT selected_plan FROM nofill_review_results WHERE review_id=?',(row['review_id'],)).fetchone()
    plan=json.loads(output['selected_plan']);plans.verify_plan(plan)
    if any(order.get(k)!=plan[k] for k in plans.GEOMETRY):raise ValueError('RECOVERY_DISPATCH_GEOMETRY_CHANGED')
    if order.get('parent_opportunity_id')!=row['opportunity_id']:raise ValueError('RECOVERY_PARENT_CHANGED')
    original=json.loads(row['original'])
    fresh=market_packet(service,service.packet(service.reader(),service.clock()),original)
    review.revalidate(plan,json.loads(saved['request']),fresh,int(service.clock()*1000))
    if not pending:
        why=journal.broker_reason(broker_proof(row['opportunity_id']),row['opportunity_id'],int(service.clock()*1000))
        if why:raise ValueError(why)
    return dict(order_id=order['order_id'],valid=True,checked_ms=int(service.clock()*1000),evidence_id=plans.digest(fresh))


def dispatch_row(plan, original_source):
    source=copy.deepcopy(original_source)
    source.update({k:plan[k] for k in plans.GEOMETRY})
    source.update(strategy='AB_DIRECTIONAL',recovery_version=2,parent_opportunity_id=plan['opportunity_id'],
                  recovery_plan_id=plan['plan_id'],risk_points=plan['stop_distance_points'],
                  dol_id='FIXED_2R',state='PENDING')
    return source


def process(service, packet, external_calls, *, proof_reader=broker_proof,
            packet_builder=market_packet, selector=selections, dispatcher=None):
    """At most one request per tick, on the EXISTING serialized AI worker."""
    if not journal.enabled(service.config) or not _LOCK.acquire(blocking=False):return False
    try:
        import continuation_live
        now=int(service.clock()*1000)
        with service.connect() as db:
            journal.init(db)
            rows=[dict(r) for r in db.execute("SELECT * FROM nofill_opportunities WHERE state IN ('ORIGINAL','CANCEL_UNCERTAIN','REVIEW_ELIGIBLE','REPLACEMENT_RESERVED') ORDER BY activation_ms")]
        for row in rows:
            if now<row['activation_ms']+600000:continue
            oid=row['opportunity_id'];original=json.loads(row['original'])
            try:
                proof=proof_reader(row['replacement_id'] or oid)
                with service.connect() as db:
                    state=journal.advance(db,oid,proof,int(service.clock()*1000))
                if row['replacement_id'] or state['state']!='REVIEW_ELIGIBLE':continue
                p=packet_builder(service,packet,original)
                choices=selector(original,p,stop_buffer_points=float(service.config.get('SL_ANCHOR_BUF','.25')))
                # Refresh broker proof after context construction; it must still be fresh.
                proof=proof_reader(oid)
                with service.connect() as db:
                    db.execute('BEGIN IMMEDIATE');journal.advance(db,oid,proof,int(service.clock()*1000))
                    reserved=journal.prepare_review(db,oid,p,choices,external_calls,int(service.clock()*1000),config=service.config)
                if not reserved['allowed']:continue
                response=None
                try:response=service.transport(reserved['request'])
                except Exception:pass  # Reserved call is consumed; never retry timeout.
                try:
                    fresh=packet_builder(service,service.packet(service.reader(),service.clock()),original)
                    proof=proof_reader(oid)
                except Exception:
                    fresh=p;proof=dict(order_id=oid,verified=False)
                with service.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    result=journal.complete_review(db,oid,reserved['review_id'],response,fresh,proof,int(service.clock()*1000))
                if result['state']!='SELECTED':return True
                plan=result['selected_plan']
                # Reserve BEFORE invoking the old dispatcher. A crash never resends it.
                with continuation_live._connect() as db:
                    prior=db.execute('SELECT source_json FROM live_dispatches WHERE order_id=?',(oid,)).fetchone()
                if not prior:raise ValueError('ORIGINAL_DISPATCH_MISSING')
                source=dispatch_row(plan,json.loads(prior[0]))
                with service.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    changed=db.execute("UPDATE nofill_opportunities SET state='REPLACEMENT_RESERVED',replacement_id=? WHERE opportunity_id=? AND state='SELECTED' AND replacement_id IS NULL",(plan['order_id'],oid)).rowcount
                if not changed:return True
                stamp=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(service.clock()))
                if not continuation_live._claim(source,stamp):return True
                try:
                    fn=dispatcher or continuation_live._DISPATCHER
                    outcome=fn(source) if fn else dict(state='BLOCKED',reason='dispatcher_unavailable')
                except Exception:outcome=dict(state='SUBMISSION_UNKNOWN',reason='RECOVERY_DISPATCH_UNCERTAIN_NO_RETRY')
                continuation_live._finish(plan['order_id'],outcome,stamp)
                with service.connect() as db:
                    journal.event(db,oid,int(service.clock()*1000),'REPLACEMENT_DISPATCH',outcome)
                return True
            except Exception as exc:
                service.event(oid,'recovery_blocked',reason=str(exc) if isinstance(exc,ValueError) else type(exc).__name__)
        return False
    finally:_LOCK.release()
