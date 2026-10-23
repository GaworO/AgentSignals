"""Read-only price explanation for a review. NEVER authorizes execution.

The real, independent execution_validation.check remains the final order gate.
This report uses the existing monitor cache, labels source uncertainty, and does
not fetch a quote, claim an order, change flags or call a broker.
"""
import math


def assess(setup, review, quote, *, now, config, dispatch=None, parameters=None, policy_error=None):
    reasons=[]; checks=[]
    def gate(name, passed, reason, evidence=None):
        checks.append(dict(gate=name,status='PASS' if passed is True else 'BLOCK' if passed is False else 'UNKNOWN',
                           reason='ok' if passed is True else reason,evidence=evidence))
        if passed is not True:reasons.append(reason)
    original={k:setup.get(k) for k in ('order_id','direction','contract','activation_ms','expiry_ms',
                                      'entry_price','stop_price','target_price')}
    gate('setup_time',setup['activation_ms']<=now*1000<setup['expiry_ms'],'signal_expired_or_not_active')
    gate('ai',review.get('state')=='ALLOW','v3_ai:'+str(review.get('state','UNAVAILABLE')).lower(),
         {k:review.get(k) for k in ('review_id','available_at','expires_at')})
    gate('execution_switches',config.get('LIVE_EXECUTION_ENABLED')=='true' and
         config.get('EXEC_KILL_SWITCH','false')=='false' and config.get('V3_ENTRY_ARMED')=='1',
         'execution_interlocks_not_armed')
    gate('automatic_entries',config.get('AUTO_SUBMIT')=='1','automatic_entries_disabled')
    gate('policy',parameters is not None,policy_error or 'validated_execution_policy_missing')
    gate('risk_allowances',None,'account_allowances_require_live_revalidation' if config.get('MFFU_RISK_EVIDENCE_FILE')
         else 'mffu_current_allowances_missing')
    raw=(quote or {}).get('raw') or {};normalized=(quote or {}).get('quote') or {}
    bid,ask=raw.get('bid'),raw.get('ask')
    valid=all(type(x) in (int,float) and math.isfinite(x) and x>0 for x in (bid,ask)) and bid<=ask
    verified=normalized.get('execution_eligible') is True
    gate('quote_source',verified,'quote_source_suitability_unverified',normalized.get('source'))
    age=(now*1000-normalized['as_of_ms']) if normalized.get('as_of_ms') is not None else None
    gate('quote_freshness',age is not None and 0<=age<=parameters['max_quote_age_ms'] if parameters and age is not None else None,
         'quote_freshness_unverified',dict(age_ms=age,observed_ms=(quote or {}).get('observed_ms')))
    sign=1 if setup['direction']=='LONG' else -1
    entry,stop,target=[setup[k] for k in ('entry_price','stop_price','target_price')]
    loss=sign*(entry-stop);reward=sign*(target-entry)
    px=(ask if sign==1 else bid) if valid else None
    spread=(ask-bid)/.25 if valid else None
    inside=abs(px-entry)<=parameters['entry_zone_points'] if parameters and px is not None else None
    gate('entry_region',inside,'entry_region_unknown' if inside is None else 'outside_validated_entry_zone',
         dict(observational_side_price=px,limit=entry))
    gate('spread',spread<=parameters['max_spread_ticks'] if parameters and spread is not None else None,
         'spread_limit_unknown_or_exceeded',spread)
    cost=None;cost_source='UNKNOWN'
    if parameters:
        cost=(parameters.get('costs') or {}).get('round_trip_usd');cost_source='validated_execution_policy'
    else:
        try:
            cost=float(config['SETUP_GROUP_RT_COST_USD']);cost_source='unvalidated_config_estimate'
        except (KeyError,TypeError,ValueError):pass
    if cost is not None and (not math.isfinite(cost) or cost<0):cost=None;cost_source='UNKNOWN'
    slip=(parameters.get('costs') or {}).get('stop_slippage_ticks',0) if parameters else 0
    net=(reward*2-cost)/(loss*2+cost+slip*.5) if cost is not None and loss>0 else None
    gate('net_rr',net>=parameters['min_rr'] if net is not None and parameters else None,
         'net_reward_risk_below_minimum' if net is not None and net<2 else 'net_rr_not_verified',net)
    # Phase 2 does not execute the risk or thesis validators as a side effect
    # of showing a report. Their absence is not silently counted as PASS.
    gate('live_thesis_and_account',None,'independent_final_execution_validation_required')
    frozen=setup.get('frozen_context',{}).get('current_dol',{})
    return dict(version='directional-review-observation-v1',observed_at=now,
        mode='OBSERVATION_ONLY',execution_authorized=False,decision='BLOCK',
        original_order=original,order_type='LIMIT',confirmed_executable_price=None,
        quote=dict(raw=raw,source=normalized.get('source'),timestamps_semantics_verified=normalized.get('timestamp_semantics_verified',False),
                   observed_ms=(quote or {}).get('observed_ms'),age_ms=age,spread_ticks=spread,
                   adapter_reason=(quote or {}).get('reason'),observation_only=True),
        observational_side_price=px,marketable_if_quote_were_executable=(sign*(px-entry)<=0 if px is not None else None),
        stop_distance_points=loss,gross_r=(reward/loss if loss>0 else None),estimated_net_r=net,
        costs=dict(round_trip_usd=cost,source=cost_source,stop_slippage_ticks=slip),
        nearest_frozen_liquidity=frozen,inside_entry_region=inside,
        actual_dispatch=dispatch,checks=checks,blocking_reasons=list(dict.fromkeys(reasons)))
