"""Read-only eligibility watchdog for already-owned, unfilled native entries.

This does not authorize submission or substitute candles for executable quotes.
Only exact already-owned orders are excluded from the flat account risk view;
foreign orders/positions always veto waiting. No paid AI calls.
"""
import json
import os
import time
import ab_v3_live
import builder_broker_sync
import continuation_live
import directional_account_limits as limits
import directional_account_reconciliation as reconciliation
import directional_ai_review
import execution_policy
import execution_safety
import execution_validation
import guardrails
from directional_thesis import ThesisValidator


class PendingValidator:
    def __init__(self, *, signal_builder, budget_builder, feed_age, market_open, news_flags, calendar_age, clock=time.time):
        self.signal_builder=signal_builder;self.budget_builder=budget_builder;self.feed_age=feed_age;self.market_open=market_open
        self.news_flags=news_flags;self.calendar_age=calendar_age;self.clock=clock

    def __call__(self, order, snapshot, owned_ids):
        try:
            now=int(self.clock()*1000)
            reason=execution_safety.entry_blocker()
            if reason:return reason
            if guardrails.exec_mode()!='auto' or os.environ.get('AUTO_SUBMIT')!='1':return 'automatic_entries_disabled'
            import ab_q1_execution, ab_q1_review, ab_ai_context
            q1=ab_q1_execution.is_order(order)
            if execution_policy.mode() not in ('V3_ONLY','DIRECTIONAL_Q1_ONLY'):return 'pending_execution_policy_changed'
            if order.get('strategy')!='AB_DIRECTIONAL' and not q1:return 'pending_strategy_not_directional'
            if q1:
                reason=ab_q1_execution.permission(order)
                if reason:return reason
            if not q1 and (os.environ.get('AB_V3_MODE','SHADOW').upper()!='LIVE' or os.environ.get('V3_ENTRY_ARMED')!='1'
                    or os.environ.get('AB_DIRECTIONAL_LIVE_'+order['direction'])!='1'):return 'pending_direction_not_armed'
            if not order['activation_ms']<=now<order['expiry_ms']:return 'pending_setup_expired'
            if snapshot['positions']:return 'pending_account_exposure'
            if any(x.get('id') not in owned_ids for x in snapshot['orders']):return 'pending_foreign_order'
            if not 0<=now-snapshot['read_started_at']*1000<=3000:return 'pending_account_stale'
            with ab_v3_live.connect() as db:
                row=db.execute('SELECT order_json FROM orders WHERE order_id=?',(order['order_id'],)).fetchone()
                if not row:return 'pending_order_missing'
                original=json.loads(row[0])
                if any(original.get(k)!=order.get(k) for k in ('direction','contract','quantity','entry_price','stop_price','target_price','expiry_ms')):
                    return 'pending_geometry_changed'
                if not ab_v3_live.m1_fresh(db,order['contract'],now):return 'pending_m1_not_fresh'
            why,review=(ab_q1_review.check(order,ab_ai_context.INSTANCE) if q1 else directional_ai_review.check(order['direction'],order['order_id']))
            if why:return why
            prior=order.get('ai_review_at_native_dispatch')
            if not prior or not review or review.get('review_id')!=prior.get('review_id'):return 'pending_review_changed'
            if not now<prior['expires_at']*1000:return 'pending_review_expired'
            thesis=ThesisValidator(clock=self.clock)(order,pending=True)
            if not thesis.get('valid'):return 'pending_thesis:'+str(thesis.get('reason','unknown'))
            sync=builder_broker_sync.instance()
            # Keep the real snapshot untouched. Remove only independently proved
            # own parent/children for the prospective remaining-entry allowance.
            flat=dict(snapshot,orders=[])
            proof=limits.read_static_evidence()
            if proof.get('account_controls_source')=='CROSSTRADE_RECONCILED':
                assessed=reconciliation.assess(flat,reconciliation.history(sync),reconciliation.margin_row(sync),
                    proof,sync.binding,order['contract'],int(self.clock()*1000))
                if not assessed['verified']:return 'pending_account_risk_unverified'
                proof=dict(proof,account_controls=assessed['controls'])
            parameters=execution_validation.policy_for(order)  # existing quote admission policy stays mandatory
            fees=execution_validation.costs(parameters)
            cost=fees['round_trip_usd']+fees['stop_slippage_ticks']*.5
            profile=guardrails.account_profile()
            budget=self.budget_builder(profile)
            amount=order['quantity']*(abs(order['entry_price']-order['stop_price'])*2+cost)
            if not profile.get('config_ok') or not 0<amount<=budget:return 'pending_strategy_risk_budget'
            if order['quantity']>int(os.environ['EXEC_MAX_QTY']):return 'pending_quantity_cap'
            reason=limits.check(flat,sync.binding,order,int(self.clock()*1000),cost,evidence=proof)
            if reason:return reason
            with continuation_live._connect() as db:
                dispatch=db.execute('SELECT source_json FROM live_dispatches WHERE order_id=?',(order['order_id'],)).fetchone()
            if not dispatch:return 'pending_dispatch_missing'
            signal=ab_q1_execution.signal(json.loads(dispatch[0])) if q1 else self.signal_builder(json.loads(dispatch[0]))
            _,news_hard=self.news_flags(signal)
            allowed,why=guardrails.guard_ok(signal,feed_age_min=self.feed_age(),market_open=self.market_open(),
                news_hard=news_hard,cal_age_h=self.calendar_age(),_pending_owned=True)
            if not allowed:return why
            if int(self.clock()*1000)>=order['expiry_ms']:return 'pending_expired_during_check'
            return None
        except Exception:return 'pending_evidence_unavailable'
