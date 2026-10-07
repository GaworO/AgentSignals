"""Durable assumed-position lifecycle; Boundary observes, only M3/runner request exits.

No broker fill is fabricated. Local modeled closure never writes realized P&L.
SQLite write transactions serialize invalidation with the final dispatch recheck,
including across Gunicorn workers. A sent/unknown exit is never retried.
"""
import json
import math
import os
from pathlib import Path
import threading

import pandas as pd
import v3_boundary as boundary
import v3_frozen_exit as frozen
import v3_runner as runner

LIFECYCLE = 'ASSUMED_BOUNDARY_M3_RUNNER_V1'
ACTIVE = ('ASSUMED_OPEN','BROKER_CONFIRMED_OPEN')
INVALIDATIONS = ('NO_FILL','CANCELLED','EXPIRED')
TERMINAL = (*INVALIDATIONS,'CLOSED')
M3 = dict(M1=False,M2=False,M3=True)


def live():
    import ab_v3_live
    return ab_v3_live


def check_models():
    boundary.models()


def accept_entry(source,result):
    api=live();oid=source['order_id'];at=api.now_ms()
    with api.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM orders WHERE order_id=?',(oid,)).fetchone()
        if not row or row['state'] in TERMINAL or row['position_json']: return
        if row['state'] not in ('PREPARED','WAITING_BROKER_FILL'): return
        order=json.loads(row['order_json']);order['dispatch_reason']=result.get('reason')
        state=str(result.get('state','ERROR'));position=None
        if state=='SENT':
            qty=result.get('quantity') or order.get('quantity')
            if not isinstance(qty,(int,float)) or not math.isfinite(qty) or int(qty)!=qty or qty<=0:
                raise ValueError('accepted_entry_quantity_missing')
            order['quantity']=int(qty)
            state='ASSUMED_OPEN'
            position=dict(assumed=True,fill_basis='ENTRY_ACCEPTED',fill_ms=at,event_ms=at,
                          entry_price=order['entry_price'],quantity=int(qty),position_id='assumed:'+oid,
                          contract=order['contract'],route_id=order['route_id'],account_label=order['account_label'],
                          direction=order['direction'],exclusive=True)
        c.execute('UPDATE orders SET state=?,order_json=?,position_json=?,updated_ms=? WHERE order_id=?',
                  (state,api.dump(order),api.dump(position) if position else None,at,oid))
        if position:
            c.execute('INSERT OR IGNORE INTO exit_trades(trade_id,exit_mode,status,updated_ms) VALUES(?,?,?,?)',
                      (oid,api.exit_mode(),'OPEN',at))
    if position:
        # Warmup is market data only. Accepted trade exists before any slow IO/inference.
        try: hydrate(order)
        except (OSError,ValueError,KeyError,TypeError): pass
        threading.Thread(target=api._tick,daemon=True).start()


def invalidate(c,oid,event,at):
    api=live()
    c.execute('UPDATE orders SET state=?,position_json=NULL,updated_ms=? WHERE order_id=? AND state<>?',
              (event,at,oid,'CLOSED'))
    c.execute('UPDATE actions SET state=?,updated_ms=? WHERE order_id=?',(event,at,oid))
    c.execute('DELETE FROM manager_states WHERE order_id=?',(oid,))
    # Historical score/runner rows remain audit evidence; no active position can use them.
    api._exit_transition(c,oid,event,at,'explicit_position_invalidation')


def hydrate(order):
    """Read the same verified detector archive, restricted to the frozen contract epoch."""
    api=live();meta=order.get('boundary_context')
    if not meta or os.environ.get('AB_V3_DETECTOR_CONTRACT_VERIFIED')!='1': return
    path=Path(os.environ.get('DATA_DIR',str(api.HERE)))/'archive.csv'
    if not path.exists(): return
    d=pd.read_csv(path);times=pd.to_datetime(d.ts_event,utc=True)
    ms=times.map(lambda t:int(t.value//1000000))
    d=d[(ms>=meta['context_start_ms'])&(ms+60000<=order['activation_ms'])].copy()
    if d.empty: return
    d.ts_event=times.loc[d.index].map(lambda t:t.isoformat())
    with api.connect() as c:
        for row in d.to_dict('records'):
            at,b=api._bar(row,'M1',api.now_ms())
            c.execute('INSERT OR IGNORE INTO bars VALUES(?,?,?,?,?)',('M1',order['contract'],at,api.dump(b),api.now_ms()))


def on_m1(bar):
    api=live()
    if api.mode() not in ('SHADOW','LIVE') or os.environ.get('AB_V3_DETECTOR_CONTRACT_VERIFIED')!='1': return
    api.ingest(dict(contract=api.contract(),tf='M1',bars=[bar]),notify_m1=False)
    threading.Thread(target=api._tick,daemon=True).start()


def _close(c,oid,at,basis):
    api=live()
    c.execute("UPDATE orders SET state='CLOSED',updated_ms=? WHERE order_id=?",(at,oid))
    row=c.execute('SELECT position_json FROM orders WHERE order_id=?',(oid,)).fetchone()
    p=json.loads(row[0]) if row and row[0] else {}
    p.update(closure_basis=basis,closed_ms=at)
    c.execute('UPDATE orders SET position_json=? WHERE order_id=?',(api.dump(p),oid))
    api._exit_transition(c,oid,'CLOSED',at,basis)


def _request(c,oid,manager,reason,event_ms,price,at):
    api=live()
    ex=c.execute('SELECT signal_time FROM exit_trades WHERE trade_id=?',(oid,)).fetchone()
    if not ex or ex[0] is not None: return
    # Event time is retained separately. A minute batch is executable on receipt,
    # never reported as an exact historical fill at 2R or at a previous M1 open.
    c.execute('UPDATE exit_trades SET exit_mode=?,primary_manager=?,primary_reason=?,triggered_managers=?,triggered_reasons=?,signal_time=?,requested_price=?,updated_ms=? WHERE trade_id=?',
              (api.exit_mode(),manager,reason,api.dump([manager]),api.dump([reason]),at,price,at,oid))
    api._exit_transition(c,oid,'EXIT_SIGNALLED',at)


def _touches(c,order,position,at):
    """First completed 1s observations after assumed/confirmed fill, not entry expiry."""
    api=live();start=(int(position['fill_ms'])+999)//1000*1000
    seconds=api.frame(c,'1s',order['contract'],start,at//1000*1000)
    z=1 if order['direction']=='LONG' else -1
    out={}
    for name,level,favourable in [('SL',order['stop_price'],False),('2R',order['baseline_target_price'],True),('3R',order['target_price'],True)]:
        col=('high' if z==1 else 'low') if favourable else ('low' if z==1 else 'high')
        hit=seconds[z*(seconds[col]-level)>=0] if favourable else seconds[z*(seconds[col]-level)<=0]
        if len(hit): out[name]=int(hit.index[0].value//1000000)+1000
    return out


def monitor():
    api=live()
    if api.mode() not in ('SHADOW','LIVE'): return
    with api.connect() as c:
        rows=c.execute("SELECT * FROM orders WHERE state IN ('ASSUMED_OPEN','BROKER_CONFIRMED_OPEN')").fetchall()
    for row in rows:
        _monitor_one(row)


def _monitor_one(row):
    api=live();at=api.now_ms();oid=row['order_id']
    order=json.loads(row['order_json']);position=json.loads(row['position_json'])
    if order.get('lifecycle')!=LIFECYCLE: return
    with api.connect() as c:
        touches=_touches(c,order,position,at)
        rd=c.execute('SELECT payload_json FROM runner_decisions WHERE order_id=?',(oid,)).fetchone()
        existing=json.loads(rd[0]) if rd else None
        latest=c.execute("SELECT MAX(ts_ms) FROM bars WHERE tf='M1' AND contract=? AND ts_ms+60000<=?",(order['contract'],at)).fetchone()[0]
        known=int(latest)+60000 if latest is not None else None
        start=(order.get('boundary_context') or {}).get('context_start_ms',position['fill_ms']-48*3600000)
        minutes=api.frame(c,'M1',order['contract'],start,known) if known else pd.DataFrame()
        last=c.execute('SELECT MAX(known_ms) FROM boundary_states WHERE order_id=?',(oid,)).fetchone()[0]
    scores=[];error=None;decision=None;frozen_state=None
    first2=touches.get('2R')
    # Boundary continues observing after 2R, but M3 cannot create a post-touch exit.
    if known and (last is None or known>last) and known>position['fill_ms']//60000*60000+60000:
        try: scores=boundary.evaluate(order,position,minutes,known)
        except (ValueError,KeyError,TypeError,IndexError,OSError) as exc: error=str(exc)
        with api.connect() as check:
            still=check.execute('SELECT state,position_json FROM orders WHERE order_id=?',(oid,)).fetchone()
            if not still or still[0] not in ACTIVE or still[1]!=row['position_json']: return
        current=next((r for r in reversed(scores) if r['known_ms']==known),None)
        if current is None:
            current=dict(known_ms=known,classification='NO_BOUNDARY_YET',win_score=None,loss_score=None,
                         boundary_score=None,error=error or 'boundary_no_state')
            scores.append(current)
        if not existing and (first2 is None or known <= (first2-1000)//60000*60000):
            try:
                frozen_state=frozen.evaluate(order,position,minutes,known,M3)
                decision=boundary.manager(current,frozen_state)
            except ValueError as exc:error=str(exc)
    runner_result=None
    if first2 and not existing and order.get('runner_enabled'):
        touchbar=(first2-1000)//60000*60000
        pre=minutes[minutes.index<pd.Timestamp(touchbar,unit='ms',tz='UTC')] if not minutes.empty else minutes
        try:
            with api.connect() as seconds_db:
                first_second=max(touchbar,(int(position['fill_ms'])+999)//1000*1000)
                seconds=api.frame(seconds_db,'1s',order['contract'],first_second,first2)
                expected=pd.date_range(pd.Timestamp(first_second,unit='ms',tz='UTC'),
                                       pd.Timestamp(first2-1000,unit='ms',tz='UTC'),freq='s')
                if not seconds.index.equals(expected): raise ValueError('runner_touch_seconds_gap')
            runner_result=runner.decide(order,position,pre,touchbar)
        except (ValueError,KeyError,TypeError,OSError) as exc:
            runner_result=dict(selected=False,probability=None,threshold=.75,error=str(exc))
        runner_result.update(touch_ms=first2,touch_bar_ms=touchbar,decision_kind='HOLD_3R' if runner_result['selected'] else 'EXIT_2R')
    at=api.now_ms()
    with api.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        fresh=c.execute('SELECT * FROM orders WHERE order_id=?',(oid,)).fetchone()
        # Includes promotion during scoring: recompute against the new fill next tick.
        if not fresh or fresh['state'] not in ACTIVE or fresh['position_json']!=row['position_json']: return
        for score in scores:
            c.execute('INSERT OR IGNORE INTO boundary_states VALUES(?,?,?)',(oid,score['known_ms'],api.dump(score)))
        if decision:
            if decision['decision']!='CLOSE':
                ex=c.execute('SELECT primary_manager,status FROM exit_trades WHERE trade_id=?',(oid,)).fetchone()
                if ex and ex[0]=='M3' and ex[1] in ('EXIT_SIGNALLED','EXIT_BLOCKED') and not c.execute('SELECT 1 FROM actions WHERE order_id=?',(oid,)).fetchone():
                    c.execute('UPDATE exit_trades SET signal_time=NULL,primary_reason=NULL,primary_manager=NULL WHERE trade_id=?',(oid,))
                    api._exit_transition(c,oid,'MONITORING',at,'current_manager_veto')
            decision.update(known_ms=known,event_ms=known)
            c.execute('INSERT OR REPLACE INTO manager_states VALUES(?,?)',(oid,api.dump(decision)))
        saved_boundary=c.execute('SELECT payload_json FROM boundary_states WHERE order_id=? ORDER BY known_ms DESC LIMIT 1',(oid,)).fetchone()
        saved_manager=c.execute('SELECT payload_json FROM manager_states WHERE order_id=?',(oid,)).fetchone()
        research=dict(known_ms=known,boundary=json.loads(saved_boundary[0]) if saved_boundary else None,
                      manager=json.loads(saved_manager[0]) if saved_manager else None,feature_error=error)
        c.execute('UPDATE exit_trades SET research_json=?,updated_ms=? WHERE trade_id=?',(api.dump(research),at,oid))
        # Stop/bracket are superior to expert opinions. Only assumed positions can
        # be closed by observed prices; real broker state waits for CLOSED.
        terminal=None
        if touches.get('SL'): terminal=('SL',touches['SL'])
        if touches.get('3R') and (terminal is None or touches['3R']<terminal[1]): terminal=('TARGET',touches['3R'])
        if terminal:
            if position.get('assumed'):
                _close(c,oid,terminal[1],'ASSUMED_BRACKET_'+terminal[0])
            else:
                c.execute("UPDATE orders SET state='EXIT_PENDING',updated_ms=? WHERE order_id=?",(at,oid))
                api._exit_transition(c,oid,'EXIT_ACKNOWLEDGED',at,'bracket_touch_waiting_broker_closed')
            return
        fresh_data=known is not None and 0<=at-known<=15000
        if decision and decision['decision']=='CLOSE' and fresh_data:
            _request(c,oid,'M3',decision['reason'],known,float(minutes.iloc[-1]['close']),at)
            return
        if runner_result:
            c.execute('INSERT OR IGNORE INTO runner_decisions VALUES(?,?,?,?,?,?)',
                      (oid,first2,runner_result['touch_bar_ms'],int(runner_result['selected']),api.dump(runner_result),at))
            if not runner_result['selected']:
                if 0<=at-first2<=75000 and fresh_data:
                    _request(c,oid,'RUNNER','RUNNER_2R_DATA_FALLBACK' if runner_result.get('error') else 'RUNNER_2R_EXIT',first2,order['baseline_target_price'],at)
                else: api._exit_transition(c,oid,'EXIT_BLOCKED',at,'runner_stale_touch')


def send_exit(aid,oid,payload):
    api=live()
    with api.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM orders WHERE order_id=?',(oid,)).fetchone()
        action=c.execute('SELECT state FROM actions WHERE action_id=?',(aid,)).fetchone()
        if not row or row['state']!='EXIT_UNKNOWN' or not action or action[0]!='EXIT_UNKNOWN': return
        # Single linearization point with invalidation. No check/send gap across workers.
        status='EXIT_REQUESTED';state='EXIT_UNKNOWN';response=None;error='broker_response_unknown_no_retry'
        try:
            r=api._send_broker_exit(payload);body=r.json()
            response=dict(http_status=int(r.status_code),success=body.get('success'),
                          broker_order_id=body.get('orderId') or body.get('order_id') or body.get('id'),body=body)
            if 200<=r.status_code<300 and body.get('success') is True:
                status='EXIT_ACKNOWLEDGED';state='EXIT_PENDING';error=None
            elif 400<=r.status_code<500:
                status='EXIT_REJECTED';state='EXIT_REJECTED';error='broker_rejected_close'
        except Exception: pass
        at=api.now_ms()
        c.execute('UPDATE actions SET state=?,updated_ms=? WHERE action_id=?',(state,at,aid))
        c.execute('UPDATE orders SET state=?,updated_ms=? WHERE order_id=?',(state,at,oid))
        c.execute('UPDATE exit_trades SET broker_order_id=?,broker_response_json=?,broker_ack_time=?,updated_ms=? WHERE trade_id=?',
                  (response.get('broker_order_id') if response else None,api.dump(response) if response else None,at if error is None else None,at,oid))
        api._exit_transition(c,oid,status,at,error)
        position=json.loads(row['position_json'] or '{}')
        if state=='EXIT_PENDING' and position.get('assumed'):
            _close(c,oid,at,'ASSUMED_EXIT_ACCEPTED')


def guard_exit_requested(reason):
    """Guard takes ownership before its HTTP action, even on an uncertain response."""
    api=live()
    with api.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        for row in c.execute("SELECT order_id FROM orders WHERE state IN ('PREPARED','WAITING_BROKER_FILL','ASSUMED_OPEN','BROKER_CONFIRMED_OPEN','EXIT_UNKNOWN')").fetchall():
            oid=row[0]
            c.execute("UPDATE orders SET state='GUARD_EXIT_PENDING',updated_ms=? WHERE order_id=?",(api.now_ms(),oid))
            api._exit_transition(c,oid,'EXIT_REQUESTED',api.now_ms(),'guard:'+reason)


def guard_exit_accepted():
    api=live()
    with api.connect() as c:
        for row in c.execute("SELECT * FROM orders WHERE state='GUARD_EXIT_PENDING'").fetchall():
            if json.loads(row['position_json'] or '{}').get('assumed'):
                _close(c,row['order_id'],api.now_ms(),'ASSUMED_GUARD_EXIT_ACCEPTED')
