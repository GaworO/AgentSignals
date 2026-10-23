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
import execution_policy
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
            if ab_v3_live.native_fixed_bracket():
                reason=ab_v3_live.native_bracket_readiness()
                if reason:return dict(proof,reason=reason)
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
            permission=execution_policy.entry_blocker(signal)
            if permission:return dict(proof,reason=permission)
            sync = builder_broker_sync.instance()
            if not sync.enabled or not sync.poll(wait=True):
                return dict(proof, reason='fresh_broker_snapshot_required')
            snapshot = sync.snapshot()
            # Repeat the account-wide inventory barrier after the final read.
            # The earlier submission read may have preceded a manual fill.
            if snapshot['positions'] or snapshot['orders']:
                return dict(proof,reason='broker:account_has_positions_or_orders')
            if getattr(sync,'reconciliation_error',None):
                return dict(proof,reason='broker:native_reconciliation_failed')
            if not sync.state()['current']:
                return dict(proof,reason='broker:account_snapshot_not_current')
            proof.update(account_binding=sync.binding,
                         checked_ms=int(snapshot['read_started_at']*1000))
            parameters=execution_validation.policy()
            if parameters.get('contract')!=order.get('contract'):
                return dict(proof,reason='cost_contract_mismatch')
            costs=execution_validation.costs(parameters)
            cost_per_contract=costs['round_trip_usd']+costs['stop_slippage_ticks']*.5
            if amount+qty*cost_per_contract>budget:
                return dict(proof,reason='cost_inclusive_trade_risk_budget')
            reason=directional_account_limits.check(snapshot,sync.binding,order,
                int(self.clock()*1000),cost_per_contract)
            if reason:return dict(proof,reason=reason)
            sizing=directional_account_limits.capacity(snapshot,sync.binding,order,
                int(self.clock()*1000),cost_per_contract,budget)
            proof['sizing']=sizing
            if not sizing['allowed']:return dict(proof,reason=sizing['reason'])
            if qty>sizing['max_quantity']:return dict(proof,reason='account_capacity_changed')
            _, news_hard = self.news_flags(signal)
            allowed, reason = guardrails.guard_ok(
                signal, feed_age_min=self.feed_age(), market_open=self.market_open(),
                news_hard=news_hard, cal_age_h=self.calendar_age(),
                reservation_id='ab_directional_'+order['order_id'])
            return dict(proof, allowed=allowed, reason=None if allowed else reason)
        except Exception:
            return proof  # No exception text may expose account/provider credentials.


def _size_for_entry(signal, *, clock=time.time):
    """Fresh account cap before native preparation; the final gate repeats it.

    Returns zero on unknown evidence. This function has no order-write operation.
    """
    order_id=signal.get('_continuation_order_id')
    result=dict(allowed=False,max_quantity=0,order_id=order_id,reason='account_capacity_unknown')
    try:
        if not signal.get('_v3_directional') or not order_id or not builder_crosstrade.selected():
            return dict(result,reason='directional_native_route_required')
        try:
            p=execution_validation.policy();fees=execution_validation.costs(p)
        except (OSError,ValueError,TypeError,KeyError):
            return dict(result,reason='execution_policy_or_costs_unavailable')
        contract=os.environ.get('AB_V3_CONTRACT')
        if p.get('contract')!=contract:return dict(result,reason='cost_contract_mismatch')
        # Prices are the frozen scanner's prices. Never recompute SL to fit qty.
        order=dict(order_id=order_id,contract=contract,direction=signal['dir'],
                   entry_price=signal['entry'],stop_price=signal['SL'],target_price=signal['TP'])
        sync=builder_broker_sync.instance()
        if not sync.enabled or not sync.poll(wait=True):
            return dict(result,reason='fresh_broker_snapshot_required')
        reason=sync.entry_blocker()
        if reason:return dict(result,reason=reason)
        cost=fees['round_trip_usd']+fees['stop_slippage_ticks']*.5
        result=directional_account_limits.capacity(sync.snapshot(),sync.binding,order,
            int(clock()*1000),cost,signal.get('_risk_budget_usd'))
    except Exception:
        return result
    return result


def size_for_entry(signal, *, clock=time.time):
    result=_size_for_entry(signal,clock=clock)
    import execution_trace
    # Durable failure propagates to _exec_order's existing no-send error path.
    if signal.get('_continuation_order_id'):
        execution_trace.record(signal['_continuation_order_id'],'account_position_sizing',evidence=result)
    return result
