"""Saved ICT history only. No broker polling, AI calls or ledger writes."""
import calendar
import collections
from datetime import datetime,timedelta
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

NY=ZoneInfo('America/New_York')

def day(at):return datetime.fromtimestamp(at,NY).date().isoformat()
def session(at):
    from ict_sessions import session as active_session
    window=active_session(at)
    if window and window[0]=='LONDON':return 'LONDON'
    d=datetime.fromtimestamp(at,NY);m=d.hour*60+d.minute
    return 'NYAM' if 570<=m<660 else 'NYPM' if 810<=m<960 else 'OTHER'
def number(x):return type(x) in (int,float) and math.isfinite(x)
def timestamp(value):
    if not value:return None
    try:return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
    except (TypeError,ValueError):return None

def bounds(period,anchor,now):
    today=datetime.fromtimestamp(now,NY).date()
    try:d=datetime.strptime(anchor,'%Y-%m-%d').date()
    except (ValueError,TypeError):raise ValueError('Choose a date in YYYY-MM-DD format.')
    if d>today:raise ValueError('Future history is unavailable.')
    if period=='day':a=b=d
    elif period=='week':a=d-timedelta(days=d.weekday());b=min(a+timedelta(days=6),today)
    elif period=='month':a=d.replace(day=1);b=min(d.replace(day=calendar.monthrange(d.year,d.month)[1]),today)
    elif period=='all':return None,today.isoformat()
    else:raise ValueError('Choose day, week, month or all.')
    return a.isoformat(),b.isoformat()

def readonly(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    db.row_factory=sqlite3.Row;return db

def broker_records(config):
    """Use complete local native journals; ACK alone is never fill evidence."""
    root=Path(config.get('DATA_DIR','/data/builder50'))
    gaps=[];rows=[]
    try:
        with readonly(config.get('CONTINUATION_LIVE_DB',root/'continuation_live.sqlite3')) as db:
            dispatches=[dict(r) for r in db.execute("SELECT order_id,state,guard_reason,direction,quantity,activation_ms,source_json,created_at,updated_at FROM live_dispatches WHERE strategy='ICT_AI' ORDER BY created_at DESC")]
    except (sqlite3.Error,OSError):return [],['ICT_DISPATCH_HISTORY_UNAVAILABLE']
    native={};positions={}
    identity=[config.get(k,'') for k in ('BUILDER_BROKER_ACCOUNT_ID','BUILDER_BROKER_ACCOUNT_NAME','BUILDER_BROKER_ENVIRONMENT')]
    binding=hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    try:
        if not all(identity):raise ValueError('Account identity unavailable')
        with readonly(config.get('AB_V3_DB',root/'ab_v3.sqlite3')) as db:
            native={r['order_id']:dict(r) for r in db.execute('SELECT order_id,state,receipt_json,observation_json,updated_ms FROM native_orders WHERE binding=?',(binding,))}
            positions={r['order_id']:json.loads(r['position_json'] or '{}') for r in db.execute('SELECT order_id,position_json FROM orders')}
    except (sqlite3.Error,OSError,ValueError):gaps.append('NATIVE_BROKER_HISTORY_UNAVAILABLE')
    for d in dispatches:
        try:
            source=json.loads(d['source_json']);n=native.get(d['order_id'],{})
            obs=json.loads(n.get('observation_json') or '{}');receipt=json.loads(n.get('receipt_json') or '{}')
            pos=positions.get(d['order_id'],{});eq=obs.get('entry_quantity');xq=obs.get('exit_quantity')
            filled=type(eq) is int and eq>0 and bool(obs.get('owned_fill_ids'))
            closed=filled and xq==eq and n.get('state') in ('CLOSED','CLOSED_PENDING_COSTS')
            if n.get('state') in ('CLOSED','CLOSED_PENDING_COSTS') and not closed:gaps.append('BROKER_CLOSURE_EVIDENCE_INCOMPLETE')
            reconciled=closed and n.get('state')=='CLOSED' and obs.get('costs_reconciled') is True and number(obs.get('net_usd'))
            created=timestamp(d['created_at'])
            fill=pos.get('fill_ms')
            fill=fill/1000 if filled and number(fill) else None
            close=obs.get('exit_fill_ms')
            close=close/1000 if closed and number(close) else None
            if filled and not fill:gaps.append('BROKER_FILL_TIMESTAMP_MISSING')
            if closed and not close:gaps.append('BROKER_CLOSE_TIMESTAMP_MISSING')
            status='WIN' if reconciled and obs['net_usd']>0 else 'LOSS' if reconciled and obs['net_usd']<0 else 'BREAKEVEN' if reconciled else 'CLOSED_COSTS_PENDING' if closed else 'FILLED' if filled else 'NO_FILL' if n.get('state')=='NO_FILL' else n.get('state') or d['state']
            rows.append(dict(order_id=d['order_id'],review_id=source.get('ict_review_id'),family=source.get('ict_family','ICT2022'),
                direction=d['direction'],quantity=d['quantity'],created_at=created,filled_at=fill,closed_at=close,
                state=status,dispatch_state=d['state'],broker_state=n.get('state'),reason=d['guard_reason'],
                acknowledged=bool(receipt.get('orderId')),filled=filled,closed=closed,costs_reconciled=reconciled,
                entry=obs.get('entry_price',source.get('entry_price')),stop=source.get('stop_price'),target=source.get('target_price'),
                exit=obs.get('exit_price'),net_usd=obs.get('net_usd') if reconciled else None,
                fees_usd=obs.get('captured_cost_usd') if reconciled else None,fees_final=obs.get('fees_final') is True,
                broker_ids={k:receipt[k] for k in ('orderId','oso1Id','oso2Id') if receipt.get(k)}))
        except (ValueError,TypeError,KeyError):gaps.append('MALFORMED_ICT_ORDER_RECORD')
    return rows,sorted(set(gaps))

def summary(reviews,costs,brokers,included):
    r=[x for x in reviews if included(x.get('requested_at'))]
    c=[x for x in costs if included(x.get('at'))]
    calls={x['id'] for x in c}|{x.get('parent_review_id') or x['id'] for x in r}
    created=[b for b in brokers if included(b.get('created_at'))]
    fills=[b for b in brokers if b['filled'] and included(b.get('filled_at'))]
    closed=[b for b in brokers if b['closed'] and included(b.get('closed_at'))]
    known=[b for b in closed if b['costs_reconciled']]
    gain=sum(max(0,b['net_usd']) for b in known);loss=-sum(min(0,b['net_usd']) for b in known)
    pending=len(closed)-len(known)
    return dict(calls=len(calls),assessments=len(r),no_trade=sum((x.get('decision') or {}).get('decision') in ('WAIT','NO_TRADE','REJECT') for x in r),
        proposals=sum(bool((x.get('decision') or {}).get('plan')) for x in r),
        validated=sum((x.get('validation') or {}).get('state')=='VALIDATED_SHADOW_PROPOSAL' for x in r),
        failed_reviews=sum(x.get('family')!='IFVG' and (bool(x.get('error')) or x.get('state') not in ('research_completed',)) for x in r),
        estimated_usd=sum(x['amount'] for x in c if number(x.get('amount'))),reserved_usd=sum(x['reserve'] for x in c if x.get('amount') is None),
        cost_records_missing=len(calls-{x['id'] for x in c}),
        broker_attempts=len(created),broker_acknowledged=sum(x['acknowledged'] for x in created),broker_fills=len(fills),
        broker_fills_missing_time=sum(x['filled'] and not x.get('filled_at') for x in created),
        broker_no_fill=sum(x['state']=='NO_FILL' for x in created),
        wins=sum(x['net_usd']>0 for x in known),losses=sum(x['net_usd']<0 for x in known),breakeven=sum(x['net_usd']==0 for x in known),
        closed_costs_pending=pending,reconciled_closures=len(known),
        net_usd=gain-loss if known and not pending else None,known_net_usd=gain-loss if known else None,
        profit_factor=gain/loss if loss and not pending else None,
        pf_reason='COSTS_PENDING' if pending else 'NO_LOSSES' if known and not loss else 'NO_CLOSED_TRADES' if not known else 'AVAILABLE',
        shadow_no_fill=sum((x.get('outcome') or {}).get('state')=='SIMULATED_NO_FILL' for x in r))

def build(state,costs,brokers,gaps,period,anchor,selected_session):
    if selected_session not in ('ALL','LONDON','NYAM','NYPM','OTHER'):raise ValueError('Unknown session filter.')
    start,end=bounds(period,anchor,state['now'])
    def included(at):
        return number(at) and at>0 and at<=state['now'] and (start is None or day(at)>=start) and day(at)<=end and (selected_session=='ALL' or session(at)==selected_session)
    reviews=list(state['reviews'])
    recorded={r.get('parent_review_id') or r['id'] for r in reviews}
    # Burned/in-flight cost reservations must not disappear just because a
    # response file is missing. This row asserts no AI decision or trade.
    for c in costs:
        if c['id'] not in recorded and number(c.get('at')):
            reviews.append(dict(id=c['id'],requested_at=c['at'],family='UNRECORDED',state='NO_SAVED_RESPONSE',
                decision=None,validation={},outcome={},record_basis='COST_RECORD_ONLY',
                error='A cost record exists, but no saved AI response is available. Decision and outcome unknown.'))
    selected=sorted((dict(r,session=session(r['requested_at'])) for r in reviews if included(r.get('requested_at'))),key=lambda r:r['requested_at'],reverse=True)
    orders=[b for b in brokers if any(included(b.get(k)) for k in ('created_at','filled_at','closed_at'))]
    dates={day(r['requested_at']) for r in selected}|{day(c['at']) for c in costs if included(c.get('at'))}
    dates|={day(b[k]) for b in orders for k in ('created_at','filled_at','closed_at') if included(b.get(k))}
    if start:
        a=datetime.strptime(start,'%Y-%m-%d').date();b=datetime.strptime(end,'%Y-%m-%d').date()
        dates|={(a+timedelta(days=i)).isoformat() for i in range((b-a).days+1)}
    daily=[dict(day=d,**summary(reviews,costs,brokers,lambda at,d=d:included(at) and day(at)==d)) for d in sorted(dates,reverse=True)]
    sessions=[dict(session=name,**summary(reviews,costs,brokers,lambda at,name=name:included(at) and session(at)==name)) for name in ('LONDON','NYAM','NYPM','OTHER')]
    return dict(from_day=start,through_day=end,period=period,session=selected_session,now=state['now'],
        summary=summary(reviews,costs,brokers,included),daily=daily,sessions=sessions,reviews=selected,broker_orders=orders,
        broker_history_gaps=gaps,timezone='America/New_York',
        accounting_basis='AI by request time; orders by creation time; fills by broker fill time; realized net P&L by broker close time. Captured fees may be revised.',
        coverage='Saved ICT2022 / IFVG records only. No recorded activity is not proof the scanner ran continuously. Shadow outcomes are simulations, not income.')

def collect(cloud,period,anchor,selected_session):
    s=cloud.state()
    with cloud.db() as db:costs=[dict(r) for r in db.execute('SELECT * FROM costs')]
    brokers,gaps=broker_records(cloud.config)
    return build(s,costs,brokers,gaps,period,anchor,selected_session)
