"""Reported-value entry limits; never claims synchronized equity/exposure."""
import math
from decimal import Decimal
from manual_account import snapshot_current
from risk_sizing import risk_budget

VERSION='reported-builder-entry-limits-v1'
FIELDS=('daily_loss_remaining','committed_risk_usd','committed_micro_contracts')


def assess(snapshot,now,cap=40):
    out=dict(version=VERSION,state='BLOCKED',reasons=[],broker_verified=False,orders_enabled=False,costs_included=False,budget_usd=None,max_contracts=None)
    def block(reason):out['reasons']=[reason];return out
    if not snapshot or not snapshot_current(snapshot,now):return block('ACCOUNT_DAILY_UPDATE_REQUIRED')
    out.update(balance_as_of=snapshot['as_of'],balance=snapshot['balance'],minimum_balance=snapshot['minimum_balance'])
    if snapshot['balance']<=snapshot['minimum_balance']:return block('ACCOUNT_LOSS_THRESHOLD_REACHED')
    if snapshot['balance']>=snapshot['target_balance']:return block('ACCOUNT_TARGET_REACHED_REVIEW_REQUIRED')
    if any(type(snapshot.get(k)) not in (int,float) or not math.isfinite(snapshot[k]) or snapshot[k]<0 for k in FIELDS):return block('ACCOUNT_RISK_FIELDS_REQUIRED')
    count=snapshot['committed_micro_contracts']
    if type(count) is not int:return block('ACCOUNT_CONTRACT_COUNT_INVALID')
    if snapshot['daily_loss_remaining']>1000:return block('ACCOUNT_DAILY_ALLOWANCE_INVALID')
    if snapshot['daily_loss_remaining']<=0:return block('ACCOUNT_DAILY_LOSS_PAUSE')
    remaining=cap-count
    if remaining<=0:return block('ACCOUNT_CONTRACT_CAP_REACHED')
    D=lambda x:Decimal(str(x))
    committed=D(snapshot['committed_risk_usd'])
    fractional=D(risk_budget(snapshot['balance']))-committed
    floor=D(snapshot['balance'])-D(snapshot['minimum_balance'])-committed-D('.01')
    daily=D(snapshot['daily_loss_remaining'])-committed-D('.01')
    budget=min(fractional,floor,daily)
    out.update(reported_values={k:snapshot[k] for k in FIELDS},fractional_budget_usd=risk_budget(snapshot['balance']),drawdown_headroom_usd=float(floor),daily_headroom_usd=float(daily))
    if budget<=0:return block('ACCOUNT_NO_REMAINING_RISK_BUDGET')
    out.update(state='REPORTED_LIMITS_CLEAR',budget_usd=float(budget.quantize(D('.01'))),max_contracts=remaining)
    return out


def apply_account(result,guard):
    result['account_guard']=guard
    if guard['state']!='REPORTED_LIMITS_CLEAR' and result['state']=='PLAN_READY':
        result.update(state='ACCOUNT_RISK_BLOCKED',reasons=guard['reasons']);result['compiled']['plan']=None
    return result
