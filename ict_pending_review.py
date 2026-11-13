"""Bounded AI review of an unfilled ICT parent; never a position manager.

One scheduled scanner request can KEEP, CANCEL, or request one GTD extension.
Only a verified broker version changes the local deadline. Unknown writes are
never retried. Original setup expiry, account gates and session end still win.
"""
import json
import os
from datetime import datetime, timezone

VERSION = 'ICT_PENDING_AI_20M_V1'
PROMPT = '''Also assess pending_order_review for the exact pending_order supplied.
This is an existing UNFILLED entry, not a position and not a new trade proposal.
Review its original thesis against current closed candles, opposing liquidity,
structure, FVG/IFVG validity and room to its unchanged fixed 2R target.
Return KEEP (retain current deadline), CANCEL (setup no longer valid), or EXTEND
(same setup remains valid and warrants the supplied one-time extension).
EXTEND is available only when extension_until_ms is supplied. Copy that deadline
exactly. KEEP/CANCEL use null extension_until_ms. Never change entry, SL, TP or
quantity, assume a fill, or renew an expired order. Cite current evidence IDs.
Missing/contradictory context cannot justify EXTEND. A WAIT for new proposals
is separate from this pending-order decision. The application rechecks broker
state, all risk gates and expiry; your response alone is not broker confirmation.'''


def enabled():
    return os.environ.get('ICT_PENDING_REVIEW_MODE') == VERSION


def state():
    """Small operator status; never triggers reads from the broker."""
    import builder_crosstrade as native
    result=dict(enabled=enabled(),mode=VERSION,initial_max_seconds=600,
                maximum_total_seconds=1200,maximum_extensions=1,
                cadence='EXISTING_SCHEDULED_SCANNER_CALLS',
                setup_and_session_expiry_still_apply=True,
                broker_extension_verification='VERSION_AND_COMMAND_REPORT_REQUIRED',
                recent=[])
    t=native._TRANSPORT
    if t is not None:
        init(t.api)
        with t.api.connect() as db:
            result['recent']=[dict(json.loads(r['payload']),received_ms=r['received_ms'])
                              for r in db.execute('SELECT * FROM ict_pending_reviews WHERE binding=? ORDER BY received_ms DESC LIMIT 5',(t.binding,))]
    return result


def init(api):
    with api.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS ict_pending_reviews(
            binding TEXT, review_id TEXT, order_id TEXT, received_ms INTEGER,
            payload TEXT, PRIMARY KEY(binding,review_id))''')


def binding(t, row):
    from ict_shadow_research import digest
    return digest(dict(account=t.binding, order_id=row['order_id'],
                       client_id=row['client_id'], command=row['command_json'],
                       receipt=row['receipt_json'], created_ms=row['created_ms']))


def extension_action(t, row):
    with t.api.connect() as db:
        r = db.execute("SELECT * FROM native_actions WHERE binding=? AND order_id=? AND operation='ict_extend'",
                       (t.binding, row['order_id'])).fetchone()
    return dict(r) if r else None


def snapshot(now):
    """Read existing reconciled observations only; no broker or AI call."""
    if not enabled():
        return None
    import builder_crosstrade as native
    import ict_execution
    from ict_sessions import session
    t = native._TRANSPORT
    if t is None:
        return None
    init(t.api)
    with t.api.connect() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM native_orders WHERE binding=? AND state NOT IN ('NO_FILL','CLOSED','CLOSED_PENDING_COSTS')", (t.binding,))]
    if len(rows) != 1:
        return None
    r = rows[0]; order = json.loads(r['command_json'])['order']
    if not ict_execution.is_order(order) or not now * 1000 < min(r['expiry_ms'], order['expiry_ms']):
        return None
    with t.api.connect() as db:
        phase = db.execute('SELECT * FROM native_order_phase WHERE binding=? AND order_id=?', (t.binding, r['order_id'])).fetchone()
        cancelling = db.execute("SELECT 1 FROM native_actions WHERE binding=? AND order_id=? AND operation='cancel'", (t.binding, r['order_id'])).fetchone()
    if not phase or phase['phase'] != 'WORKING' or phase['recovery'] or cancelling or not 0 <= now*1000-phase['updated_ms'] <= 30000:
        return None
    fact = json.loads(phase['exposure_json'])
    if fact.get('entry_quantity') != 0 or fact.get('broker_positions'):
        return None
    receipt = json.loads(r['receipt_json'] or '{}')
    if set(receipt) != {'orderId', 'oso1Id', 'oso2Id'}:
        return None
    window = session(now)
    if not window:
        return None
    until = min(r['created_ms']+1200000, r['expiry_ms']+600000,
                order['expiry_ms'], int(window[1]*1000))
    # The extension review uses the ordinary five-minute cadence, ahead of GTD.
    # A response arriving at/after GTD cannot resurrect the order.
    available = (now*1000-r['created_ms'] >= 300000 and
                 until > r['expiry_ms'] and not extension_action(t, r))
    try:
        original = ict_execution.record_for(order)
        decision = original['decision']
        if order.get('ict_family') == 'IFVG':
            decision = decision['ifvg_review']
        thesis = {k: decision.get(k) for k in ('summary', 'plan', 'assessments')}
    except (OSError, KeyError, ValueError, TypeError):
        return None
    return dict(order_id=r['order_id'], binding=binding(t, r), broker_parent_id=receipt['orderId'],
                family=order.get('ict_family', 'ICT2022'), direction=order['direction'],
                contract=order['contract'], entry=order['entry_price'], stop=order['stop_price'],
                target=order['target_price'], quantity=order['quantity'],
                original_review_id=order['ict_review_id'], original_thesis=thesis,
                submitted_ms=r['created_ms'], current_expiry_ms=r['expiry_ms'],
                original_setup_expiry_ms=order['expiry_ms'], observed_ms=phase['updated_ms'],
                extension_until_ms=until if available else None, maximum_total_seconds=1200)


def schema(p):
    from ict_shadow_research import obj, STR, IDS
    return obj(dict(order_id=dict(type='string', enum=[p['order_id']]),
                    binding=dict(type='string', enum=[p['binding']]),
                    decision=dict(type='string', enum=['KEEP','CANCEL','EXTEND']),
                    reason=STR, evidence_ids=IDS,
                    extension_until_ms=dict(anyOf=[dict(type='null'), dict(type='integer')])) )


def validate(packet, decision, received):
    p = packet['pending_order']; d = decision['pending_order_review']
    assert d['order_id'] == p['order_id'] and d['binding'] == p['binding'], 'PENDING_BINDING_MISMATCH'
    assert d['decision'] in ('KEEP','CANCEL','EXTEND'), 'PENDING_DECISION_INVALID'
    assert isinstance(d['reason'], str) and d['reason'].strip(), 'PENDING_REASON_REQUIRED'
    assert d['evidence_ids'] and all(i in packet['evidence'] for i in d['evidence_ids']), 'PENDING_EVIDENCE_REQUIRED'
    assert 0 <= received-packet['as_of'] <= 120, 'PENDING_REVIEW_STALE'
    assert packet['as_of']*1000 <= received*1000 < p['current_expiry_ms'], 'PENDING_REVIEW_EXPIRED'
    if d['decision'] == 'EXTEND':
        end = d['extension_until_ms']
        assert type(end) is int and end == p['extension_until_ms'], 'PENDING_EXTENSION_NOT_OFFERED'
        assert p['current_expiry_ms'] < end <= min(p['submitted_ms']+1200000, p['current_expiry_ms']+600000, p['original_setup_expiry_ms'], packet['session_end']*1000), 'PENDING_EXTENSION_BOUND'
    else:
        assert d['extension_until_ms'] is None, 'KEEP_CANCEL_CANNOT_EXTEND'
    return dict(state='VALIDATED_PENDING_REVIEW', **d)


def ingest(records, now):
    if not enabled():
        return
    import builder_crosstrade as native
    import ict_shadow_research as research
    t = native._TRANSPORT
    if t is None:
        return
    init(t.api)
    for r in records:
        try:
            p = r['packet']
            if not p.get('pending_order') or r['state'] != 'research_completed' or r.get('error'):
                continue
            if not 0 <= now-r['received_at'] <= 120:
                continue
            raw = r['response']
            texts = [c['text'] for x in raw['output'] if x.get('type') == 'message' for c in x.get('content', []) if c.get('type') == 'output_text']
            if raw.get('status') != 'completed' or len(texts) != 1 or json.loads(texts[0]) != r['decision']:
                continue
            if research.digest({k:v for k,v in p.items() if k!='packet_id'}) != p['packet_id']:
                continue
            d = validate(p, r['decision'], r['received_at'])
            row = t.row(d['order_id'])
            if row is None or binding(t, row) != d['binding']:
                continue
            d.update(review_id=r['id'], current_expiry_ms=p['pending_order']['current_expiry_ms'])
            with t.api.connect() as db:
                db.execute('INSERT OR IGNORE INTO ict_pending_reviews VALUES(?,?,?,?,?)',
                           (t.binding, r['id'], d['order_id'], int(r['received_at']*1000), json.dumps(d)))
        except (ValueError, KeyError, TypeError, AssertionError):
            continue


def decision(t, row, now):
    init(t.api)
    with t.api.connect() as db:
        records = db.execute('SELECT * FROM ict_pending_reviews WHERE binding=? AND order_id=? ORDER BY received_ms DESC', (t.binding,row['order_id'])).fetchall()
    valid = [json.loads(r['payload']) for r in records if 0 <= now-r['received_ms'] <= 120000 and json.loads(r['payload'])['binding'] == binding(t,row)]
    # An accepted cancel is sticky; later KEEP never reverses it.
    return next((r for r in valid if r['decision']=='CANCEL'), valid[0] if valid else None)


def apply(t, row, receipt, entry, snapshot, fact):
    """Called by native reconciliation; returns optional exact-parent cancel reason."""
    if not enabled():
        return None
    import builder_crosstrade as native
    import ict_execution
    import execution_safety
    from ict_sessions import session
    order = json.loads(row['command_json'])['order']; now=int(t.clock()*1000)
    if not ict_execution.is_order(order):
        return None
    action = extension_action(t,row)
    if action and action['state'] != 'CONFIRMED':
        payload=json.loads(action['payload_json']); end=payload['expiry_ms']
        reply=json.loads(action['response_json'] or '{}'); command_id=reply.get('response',{}).get('commandId')
        v=entry['version']
        try:
            confirmed=(entry['order']['ordStatus']=='Working' and native.stable_version(entry)
                       and str(v.get('timeInForce','')).lower()=='gtd' and native.stamp(v.get('expireTime'))==end
                       and any(c.get('commandType')=='Modify' and c.get('id')==v.get('id') for c in entry['commands'])
                       and (command_id is None or command_id==v.get('id')))
        except (ValueError,TypeError):
            confirmed=False
        # Never revive an expired/cancelled parent, even if a late change landed.
        if confirmed and now < row['expiry_ms'] and not fact['entry_quantity']:
            with t.api.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if not db.execute("SELECT 1 FROM native_actions WHERE binding=? AND order_id=? AND operation='cancel'",(t.binding,row['order_id'])).fetchone():
                    db.execute('UPDATE native_orders SET expiry_ms=? WHERE binding=? AND order_id=? AND expiry_ms=?',(end,t.binding,row['order_id'],row['expiry_ms']))
                    db.execute("UPDATE native_actions SET state='CONFIRMED' WHERE binding=? AND action_id=?",(t.binding,action['action_id']))
                    t.audit(db,row['order_id'],'ICT_EXTENSION_CONFIRMED',payload)
                    row['expiry_ms']=end
        return None
    if fact['entry_quantity'] or snapshot['positions'] or entry['order']['ordStatus']!='Working':
        return None
    d=decision(t,row,now)
    if not d or now>=row['expiry_ms']:
        return None
    if d['decision']=='CANCEL':
        return 'ict_ai_pending_cancel:'+d['review_id']
    if d['decision']!='EXTEND' or action:
        return None
    end=d['extension_until_ms']; window=session(t.clock())
    if (not window or not row['expiry_ms'] < end <= min(row['created_ms']+1200000, row['expiry_ms']+600000, order['expiry_ms'],int(window[1]*1000))
            or d['current_expiry_ms']!=row['expiry_ms'] or now-row['created_ms']<300000
            or not 0 <= t.clock()-snapshot['observed_at'] <= 3
            or not execution_safety.writes_enabled() or execution_safety.entry_blocker()
            or t.directional_permission(order) or native.pending_blocker(order,snapshot,set(receipt.values()))):
        return None
    # Serialized, durable single attempt. No replacement order or quantity/price edit.
    with native._LOCK:
        life=t.entry_identity(row,receipt)
        if life['order']['ordStatus']!='Working' or not native.stable_version(life) or t.fills({receipt['orderId']},snapshot,row['created_ms']):
            return None
        if any(r.get('ordStatus') in ('Filled','PartFilled','PartiallyFilled') or str(r.get('reportType','')).lower() in ('fill','partialfill') for r in life['reports']):
            return None
        # Extending exposure requires current evidence of the original OSO
        # children, not merely IDs retained from the original acknowledgment.
        if set(receipt) != {'orderId','oso1Id','oso2Id'}:
            return None
        legs=[t.lifecycle(receipt[k]) for k in ('oso1Id','oso2Id')]
        expected={'limit':order['target_price'],'stop':order['stop_price']}
        if not native.oco_linked(legs[0]['order'],legs[1]['order']):
            return None
        if not any(l['order'].get('parentId')==receipt['orderId'] for l in legs):
            return None
        if {str(l['version'].get('orderType','')).lower() for l in legs} != set(expected):
            return None
        for leg in legs:
            kind=str(leg['version']['orderType']).lower()
            if (leg['order']['ordStatus']!='Suspended' or not native.stable_version(leg)
                    or leg['version'].get('orderQty')!=order['quantity']
                    or str(leg['order'].get('action','')).lower()!=('sell' if order['direction']=='LONG' else 'buy')
                    or leg['version'].get('price' if kind=='limit' else 'stopPrice')!=expected[kind]):
                return None
        life=t.entry_identity(row,receipt)
        if (life['order']['ordStatus']!='Working' or not native.stable_version(life)
                or any(r.get('ordStatus') in ('Filled','PartFilled','PartiallyFilled')
                       or str(r.get('reportType','')).lower() in ('fill','partialfill') for r in life['reports'])):
            return None
        if int(t.clock()*1000)>=row['expiry_ms']:
            return None
        payload=dict(broker_id=receipt['orderId'],expiry_ms=end,review_id=d['review_id'])
        aid='ict-extend:'+row['order_id']
        with t.api.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM native_actions WHERE binding=? AND order_id=? AND operation IN ('ict_extend','cancel','close')",(t.binding,row['order_id'])).fetchone():
                return None
            if db.execute('SELECT 1 FROM native_exit_requests WHERE binding=? AND order_id=?',(t.binding,row['order_id'])).fetchone():
                return None
            db.execute('INSERT INTO native_actions VALUES(?,?,?,?,?,?,?,?)',(t.binding,aid,row['order_id'],'ict_extend','SENDING',native.encode(payload),None,now))
        reply={};state='UNKNOWN'
        try:
            reply=t.request('PUT',t.prefix+'/orders/'+str(receipt['orderId'])+'/change',
                            dict(tif='gtd',expireTime=datetime.fromtimestamp(end/1000,timezone.utc).isoformat().replace('+00:00','Z')))
            if (reply.get('success') is True and reply.get('destination')=='tradovate' and reply.get('account')==t.sync.name
                    and reply.get('api')=='modify_order' and native.numeric(reply.get('response',{}).get('commandId')) and not reply.get('warning')):
                state='ACKNOWLEDGED'
        except Exception:
            pass
        with t.api.connect() as db:
            db.execute('UPDATE native_actions SET state=?,response_json=? WHERE binding=? AND action_id=?',(state,native.encode(reply),t.binding,aid))
            t.audit(db,row['order_id'],'ICT_EXTENSION_ATTEMPT',dict(payload,state=state))
    return None
