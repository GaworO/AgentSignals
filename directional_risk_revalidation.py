"""Server-side final risk proof using the existing Guard and native reservation.

No new risk thresholds, equity overrides or entry submissions. The existing
Guard retains its normal risk latch and protective-exit behavior. Callbacks are application functions, never
fields accepted from a webhook or a model response.
"""
import json
import math
import os
import time

import ab_v3_live
import builder_broker_sync
import builder_crosstrade
import continuation_live
import execution_safety
import guardrails
import directional_account_limits
import execution_validation


class RiskValidator:
    def __init__(self, *, signal_builder, budget_builder, feed_age, market_open,
                 news_flags, calendar_age, clock=time.time):
        self.signal_builder = signal_builder
        self.budget_builder = budget_builder
        self.feed_age = feed_age
        self.market_open = market_open
        self.news_flags = news_flags
        self.calendar_age = calendar_age
        self.clock = clock

    def __call__(self, order):
        proof = dict(order_id=order['order_id'], allowed=False,
                     checked_ms=int(self.clock()*1000), reason='risk_evidence_unavailable')
        try:
            reason = execution_safety.entry_blocker()
            if reason:
                return dict(proof, reason=reason)
            if guardrails.exec_mode() != 'auto' or os.environ.get('AUTO_SUBMIT') != '1':
                return dict(proof, reason='automatic_entries_disabled')
            if order.get('strategy') != 'AB_DIRECTIONAL' or not builder_crosstrade.selected():
                return dict(proof, reason='directional_native_route_required')
            with continuation_live._connect() as db:
                dispatch = db.execute('SELECT state,source_json FROM live_dispatches WHERE order_id=?',
                                      (order['order_id'],)).fetchone()
            if not dispatch or dispatch['state'] != 'DISPATCHING':
                return dict(proof, reason='owned_forward_dispatch_required')
            source = json.loads(dispatch['source_json'])
            signal = self.signal_builder(source)
            if any(signal.get(k) != order.get(v) for k,v in
                   (('dir','direction'),('entry','entry_price'),('SL','stop_price'),('TP','target_price'))):
                return dict(proof, reason='dispatch_geometry_changed')
            if (source.get('strategy') != 'AB_DIRECTIONAL' or source.get('order_id') != order['order_id']
                    or source.get('activation_ms') != order['activation_ms']
                    or source.get('expiry_ms') != order['expiry_ms']):
                return dict(proof, reason='dispatch_identity_changed')
            with ab_v3_live.connect() as db:
                prepared = db.execute('SELECT state,order_json FROM orders WHERE order_id=?',
                                      (order['order_id'],)).fetchone()
            if not prepared or prepared['state'] != 'PREPARED':
                return dict(proof, reason='entry_no_longer_prepared')
            saved = json.loads(prepared['order_json'])
            if any(saved.get(k) != order.get(k) for k in
                   ('strategy','direction','contract','route_id','account_label','quantity',
                    'entry_price','stop_price','target_price','activation_ms','expiry_ms')):
                return dict(proof, reason='prepared_order_changed')
            profile = guardrails.account_profile()
            budget = self.budget_builder(profile)
            qty = order['quantity']
            maximum = int(os.environ['EXEC_MAX_QTY'])
            amount = qty*abs(order['entry_price']-order['stop_price'])*2
            if (not profile.get('config_ok') or type(qty) is not int or not 0 < qty <= maximum
                    or not math.isfinite(budget) or not 0 < amount <= budget):
                return dict(proof, reason='account_quantity_or_risk_budget')
            signal['_risk_budget_usd'] = budget
            signal['_planned_group_risk_usd'] = budget
            sync = builder_broker_sync.instance()
            if not sync.enabled or not sync.poll(wait=True):
                return dict(proof, reason='fresh_broker_snapshot_required')
            snapshot = sync.snapshot()
            proof.update(account_binding=sync.binding,
                         checked_ms=int(snapshot['read_started_at']*1000))
            costs=execution_validation.costs(execution_validation.policy())
            reason=directional_account_limits.check(snapshot,sync.binding,order,
                int(self.clock()*1000),costs['round_trip_usd']+costs['stop_slippage_ticks']*.5)
            if reason:return dict(proof,reason=reason)
            _, news_hard = self.news_flags(signal)
            allowed, reason = guardrails.guard_ok(
                signal, feed_age_min=self.feed_age(), market_open=self.market_open(),
                news_hard=news_hard, cal_age_h=self.calendar_age(),
                reservation_id='ab_directional_'+order['order_id'])
            return dict(proof, allowed=allowed, reason=None if allowed else reason)
        except Exception:
            return proof  # No exception text may expose account/provider credentials.
