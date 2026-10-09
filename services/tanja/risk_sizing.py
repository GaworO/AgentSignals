"""Fixed fractional MNQ planned price risk. No account or order authority."""
from decimal import Decimal, ROUND_DOWN
import math

RISK_FRACTION = Decimal('0.005')

def risk_budget(balance):
    if type(balance) not in (int,float) or not math.isfinite(balance) or balance<=0:
        raise ValueError('CURRENT_BALANCE_REQUIRED')
    return float((Decimal(str(balance))*RISK_FRACTION).quantize(Decimal('.01'),rounding=ROUND_DOWN))

def contracts_for_stop(entry, stop, direction, budget, max_contracts):
    if direction not in ('long','short') or type(max_contracts) is not int or max_contracts<1:
        raise ValueError('INVALID_SIZING_INPUT')
    if any(type(x) not in (int,float) or not math.isfinite(x) or x<=0 for x in (entry,stop,budget)):
        raise ValueError('INVALID_SIZING_PRICE_OR_BUDGET')
    if any(Decimal(str(x))%Decimal('.25') for x in (entry,stop)):
        raise ValueError('OFF_TICK_SIZING_PRICE')
    distance=(Decimal(str(entry))-Decimal(str(stop)))*(1 if direction=='long' else -1)
    if distance<=0:raise ValueError('INVALID_STOP_DIRECTION')
    # MNQ is $2 per index point. Never round up or force a minimum contract.
    return min(max_contracts,int(Decimal(str(budget))//(distance*2)))
