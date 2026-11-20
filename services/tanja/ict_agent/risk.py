"""Independent fail-closed risk checks. No API or broker client in this module."""
import math
from dataclasses import dataclass, asdict
from .market import SPECS
from .common import digest

@dataclass(frozen=True)
class Policy:
    per_trade_usd: float
    max_daily_loss_usd: float
    max_open_risk_usd: float
    max_contracts: int
    fee_per_side_usd: float
    slippage_ticks_per_side: int
    max_quote_age_seconds: int
    max_account_age_seconds: int
    require_news: bool = True

    def __post_init__(self):
        for name in ('per_trade_usd','max_daily_loss_usd','max_open_risk_usd'):
            if type(getattr(self,name)) not in (int,float) or not math.isfinite(getattr(self,name)) or getattr(self,name)<=0:
                raise ValueError('INVALID_RISK_POLICY')
        if type(self.fee_per_side_usd) not in (int,float) or not math.isfinite(self.fee_per_side_usd) or self.fee_per_side_usd<0:
            raise ValueError('INVALID_FEE')
        for name in ('max_contracts','slippage_ticks_per_side','max_quote_age_seconds','max_account_age_seconds'):
            value=getattr(self,name)
            if type(value) is not int or value<(0 if name=='slippage_ticks_per_side' else 1):raise ValueError('INVALID_POLICY_INTEGER')
        if type(self.require_news) is not bool:raise ValueError('INVALID_NEWS_POLICY')

def check(plan, policy, account, market, now, *, kill=False, duplicate=False):
    reasons=[]
    if policy is None:return dict(approved=False,quantity=0,reasons=['RISK_POLICY_NOT_CONFIGURED'])
    if type(now) is not int or now<0:return dict(approved=False,quantity=0,reasons=['INVALID_CLOCK'])
    if kill:reasons.append('KILL_SWITCH')
    if duplicate:reasons.append('DUPLICATE_OPPORTUNITY')
    try:
        spec=SPECS[plan['symbol']]
        e,s,t=(plan[k] for k in ('entry','stop','target'))
        if not all(type(x) in (float,int) and math.isfinite(x) and x>0 and abs(x/spec['tick']-round(x/spec['tick']))<1e-6 for x in (e,s,t)):
            raise ValueError()
        if plan['direction']=='LONG':oriented=s<e<t
        elif plan['direction']=='SHORT':oriented=t<e<s
        else:oriented=False
        if not oriented:raise ValueError()
        if not plan['available_at']<=now<plan['expires_at']:reasons.append('PLAN_EXPIRED_OR_FUTURE')
        if plan['contract']!=market.get('contract') or plan['symbol']!=market.get('symbol'):reasons.append('CONTRACT_MISMATCH')
        if market.get('spec')!=spec:reasons.append('CONTRACT_MULTIPLIER_MISMATCH')
    except (KeyError,ValueError,TypeError):
        return dict(approved=False,quantity=0,reasons=['INVALID_PLAN_OR_INSTRUMENT'])
    if market.get('dated_contract_verified') is not True:reasons.append('DATED_CONTRACT_UNVERIFIED')
    if market.get('session_open') is not True:reasons.append('SESSION_NOT_VERIFIED_OPEN')
    if market.get('connected') is not True:reasons.append('FEED_DISCONNECTED')
    quote=market.get('quote_at')
    if type(quote) is not int or not 0<=now-quote<=policy.max_quote_age_seconds:reasons.append('STALE_QUOTE')
    bid,ask=market.get('bid'),market.get('ask')
    if not all(type(x) in (int,float) and math.isfinite(x) and x>0 for x in (bid,ask)) or bid>ask:
        reasons.append('EXECUTABLE_QUOTE_MISSING')
    if policy.require_news and market.get('news_clear') is not True:reasons.append('NEWS_UNKNOWN_OR_RESTRICTED')
    if account.get('reconciled') is not True or account.get('connected') is not True:reasons.append('BROKER_NOT_RECONCILED')
    acct_at=account.get('as_of')
    if type(acct_at) is not int or not 0<=now-acct_at<=policy.max_account_age_seconds:reasons.append('ACCOUNT_STALE')
    if account.get('protection_ok') is not True:reasons.append('PROTECTION_NOT_CONFIRMED')
    for key in ('open_contracts','reserved_contracts'):
        if type(account.get(key)) is not int or account[key]<0:reasons.append('ACCOUNT_SIZE_UNKNOWN')
    for key in ('daily_pnl_usd','open_risk_usd','reserved_risk_usd'):
        value=account.get(key)
        if type(value) not in (float,int) or not math.isfinite(value):reasons.append('ACCOUNT_NUMBERS_UNKNOWN');break
    if 'ACCOUNT_NUMBERS_UNKNOWN' in reasons or 'ACCOUNT_SIZE_UNKNOWN' in reasons:
        return dict(approved=False,quantity=0,reasons=list(dict.fromkeys(reasons)))
    if account['open_risk_usd']<0 or account['reserved_risk_usd']<0:reasons.append('INVALID_OPEN_RISK')
    open_risk=account['open_risk_usd']+account['reserved_risk_usd']
    remaining_daily=policy.max_daily_loss_usd+min(0,account['daily_pnl_usd'])-open_risk
    budget=min(policy.per_trade_usd,policy.max_open_risk_usd-open_risk,remaining_daily)
    cost=2*policy.fee_per_side_usd+2*policy.slippage_ticks_per_side*spec['tick_value']
    per_contract=abs(e-s)*spec['multiplier']+cost
    capacity=policy.max_contracts-account['open_contracts']-account['reserved_contracts']
    quantity=max(0,min(capacity,math.floor(budget/per_contract)))
    if remaining_daily<=0:reasons.append('DAILY_LOSS_LIMIT')
    if quantity==0:reasons.append('NO_RISK_CAPACITY')
    return dict(approved=not reasons,quantity=quantity if not reasons else 0,reasons=list(dict.fromkeys(reasons)),
        risk_per_contract_usd=per_contract,cost_reserve_per_contract_usd=cost,budget_usd=budget,
        policy_version=digest(asdict(policy)),live_enabled=False)
