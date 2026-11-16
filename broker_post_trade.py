"""Persistent, read-only post-trade watchdog; never grants order permission.

Uses the existing account cache and native journals, not extra provider/AI calls.
The native reconciler remains the sole owner of evidence-based settlement.
"""
import json
import threading

INTERVAL = 15
STALE_SECONDS = 45
ESCALATE_SECONDS = 120  # Allows the existing 60s fee-history refresh to complete.
NOTE = 'Post-trade settlement only; fresh setup, AI, session, news and Guard checks still apply.'


class Watchdog:
    def __init__(self, transport):
        self.t = transport
        self.lock = threading.Lock()
        with self.t.api.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS native_post_trade_check '
                      '(binding TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    def inspect(self):
        t = self.t
        broker = t.sync.state()
        snapshot = t.sync.snapshot()
        with t.api.connect() as c:
            latest = c.execute('SELECT * FROM native_orders WHERE binding=? ORDER BY created_ms DESC LIMIT 1', (t.binding,)).fetchone()
            pending = [dict(r) for r in c.execute("SELECT order_id,state,error FROM native_orders WHERE binding=? AND state NOT IN ('NO_FILL','CLOSED')", (t.binding,))]
            actions = [dict(r) for r in c.execute("SELECT order_id,operation,state FROM native_actions WHERE binding=? AND state IN ('SENDING','UNKNOWN','ACKNOWLEDGED')", (t.binding,))]
            recovery = [dict(r) for r in c.execute('SELECT order_id,reason FROM native_exit_requests WHERE binding=?', (t.binding,))]
            alerts = [dict(r) for r in c.execute('SELECT order_id,reason FROM native_operator_alerts WHERE binding=? AND resolved=0', (t.binding,))]
            phases = [dict(r) for r in c.execute('SELECT order_id,reason FROM native_order_phase WHERE binding=? AND recovery=1', (t.binding,))]
            local = c.execute('SELECT state FROM orders WHERE order_id=?', (latest['order_id'],)).fetchone() if latest else None
        result = dict(order_id=latest['order_id'] if latest else None, broker_current=bool(broker.get('current')),
                      positions=broker.get('open_positions'), working_orders=broker.get('working_orders'),
                      pending_orders=pending, unresolved_actions=actions, recovery_requests=recovery,
                      operator_alerts=alerts, recovery_phases=phases, reasons=[], note=NOTE,
                      authorizes_entries=False)
        reasons = result['reasons']
        if not broker.get('current') or not snapshot:
            reasons.append('broker_account_not_current')
        if latest is None and not reasons and not (pending or actions or recovery or alerts or phases):
            return dict(result, state='NO_TRADES', level='info', summary='No native trade to reconcile yet.')
        # Expected pending entries / live positions are not post-trade failures.
        active = bool(broker.get('current') and snapshot and (snapshot['positions'] or snapshot['orders']))
        if active and latest and latest['state'] not in ('CLOSED','CLOSED_PENDING_COSTS','NO_FILL') and not (recovery or alerts or phases or any(a['state']=='UNKNOWN' for a in actions)):
            return dict(result, state='ACTIVE', level='info', summary='Position or working order active; closure check follows settlement.')
        if active:
            reasons.append('broker_exposure_or_orders_remain')
        if actions: reasons.append('native_actions_unresolved')
        if pending: reasons.append('native_order_or_costs_unsettled')
        if recovery: reasons.append('recovery_request_pending')
        if alerts: reasons.append('operator_alert_unresolved')
        if phases: reasons.append('recovery_phase_pending')
        if broker.get('reconciliation_error'): reasons.append('native_reconciliation_failed')
        if latest and latest['state'] == 'CLOSED':
            observation = json.loads(latest['observation_json'] or '{}')
            if observation.get('costs_reconciled') is not True:
                reasons.append('closed_trade_fees_unreconciled')
            if observation.get('entry_quantity', 0) != observation.get('exit_quantity', -1):
                reasons.append('closed_trade_fill_quantities_differ')
            if not local or local['state'] != 'CLOSED':
                reasons.append('local_closed_state_mismatch')
            result['net_usd'] = observation.get('net_usd')
        if latest and latest['state']=='NO_FILL' and (not local or local['state'] not in ('NO_FILL','CANCELLED','EXPIRED')):
            reasons.append('local_no_fill_state_mismatch')
        if not active:
            import directional_account_reconciliation as risk
            r = risk.state(t.sync)
            result['account_risk_verified'] = r.get('verified') is True and not r.get('missing')
            if not result['account_risk_verified']:
                reasons.extend('account:'+str(x) for x in (r.get('missing') or ['risk_unverified']))
            why = t.entry_blocker()
            if why: reasons.append(why)
        result['reasons'] = sorted(set(reasons))
        return dict(result, state='PENDING' if reasons else 'SETTLED', level='warn' if reasons else 'ok',
                    summary='Reconciliation pending: '+', '.join(result['reasons']) if reasons else
                    'Last trade reconciled; no leftover broker reconciliation block.')

    def check(self):
        if not self.lock.acquire(blocking=False): return False
        try:
            now = self.t.clock()
            try:
                result = self.inspect()
            except Exception:
                result = dict(state='PENDING', level='warn', reasons=['post_trade_check_failed'],
                              order_id=None, summary='Post-trade verification failed; inspect service logs.',
                              authorizes_entries=False, note=NOTE)
            with self.t.api.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                old = c.execute('SELECT payload FROM native_post_trade_check WHERE binding=?', (self.t.binding,)).fetchone()
                previous = json.loads(old[0]) if old else {}
                since = previous.get('blocked_since') if previous.get('state') in ('PENDING','BLOCKED') else None
                # A reason change or restart must not reset an unresolved alarm.
                result['blocked_since'] = (since if since is not None else now) if result['state']=='PENDING' else None
                if result['state']=='PENDING' and now-result['blocked_since'] >= ESCALATE_SECONDS:
                    result.update(state='BLOCKED', level='critical', summary='Post-trade reconciliation blocked: '+', '.join(result['reasons']))
                result.update(checked_at=now, interval_seconds=INTERVAL, escalation_seconds=ESCALATE_SECONDS)
                changed = any(previous.get(k)!=result.get(k) for k in ('state','reasons','order_id'))
                c.execute('INSERT OR REPLACE INTO native_post_trade_check VALUES(?,?)', (self.t.binding,json.dumps(result,sort_keys=True)))
                if changed and result.get('order_id'):
                    self.t.audit(c,result['order_id'],'POST_TRADE_CHECK',dict(state=result['state'],reasons=result['reasons']))
            if changed:
                print('[post-trade]', result['state'], result.get('order_id'), result['summary'], flush=True)
            return changed
        finally:
            self.lock.release()

    def state(self):
        try:
            with self.t.api.connect() as c:
                row=c.execute('SELECT payload FROM native_post_trade_check WHERE binding=?', (self.t.binding,)).fetchone()
            result=json.loads(row[0]) if row else dict(state='STARTING', level='info', summary='Post-trade check starting.', reasons=[])
            if row and not 0 <= self.t.clock()-result['checked_at'] <= STALE_SECONDS:
                result.update(state='STALE',level='critical',summary='Post-trade watchdog is stale; verification required.',reasons=['post_trade_watchdog_stale'])
            result.update(authorizes_entries=False,note=NOTE)
            return result
        except Exception:
            return dict(state='UNKNOWN',level='critical',summary='Post-trade watchdog unavailable.',reasons=['post_trade_watchdog_unavailable'],authorizes_entries=False)

    def start(self):
        if getattr(self,'thread',None): return
        def run():
            while True:
                try:
                    if self.check():
                        # Reuse existing health transition reporting, no new channel.
                        import guardrails
                        guardrails.health()
                except Exception:
                    print('[post-trade] watchdog pass failed; retrying',flush=True)
                threading.Event().wait(INTERVAL)
        self.thread=threading.Thread(target=run,daemon=True,name='broker-post-trade-watchdog')
        self.thread.start()
