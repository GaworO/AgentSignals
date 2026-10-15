"""Receipt-bounded MNQ daily ATR and current-session range, no imputed candles.

Default research convention: SMA of five completed Globex session true ranges.
Session 18:00–17:00 New York; the published 16:15–16:30 halt is not a data gap.
Special holiday hours require explicit sourced overrides; absent bars are never
silently skipped to replace a short/incomplete session with a convenient day.
"""
from dataclasses import dataclass,asdict
from datetime import datetime,timedelta,time
from zoneinfo import ZoneInfo
import hashlib
import json
import math

VERSION='mnq-daily-atr-sma-v1'
NY=ZoneInfo('America/New_York')
HOURS_SOURCE='https://www.cmegroup.com/articles/faqs/micro-e-mini-equity-index-futures-frequently-asked-questions.html'


@dataclass(frozen=True)
class Policy:
    period:int=5
    session:str='futures'
    overrides:dict=None

    def __post_init__(self):
        if type(self.period) is not int or not 1<=self.period<=20:raise ValueError('ATR_PERIOD_1_TO_20_REQUIRED')
        if self.session not in ('futures','regular'):raise ValueError('ATR_SESSION_FUTURES_OR_REGULAR_REQUIRED')
        if self.overrides is None:object.__setattr__(self,'overrides',{})
        if not isinstance(self.overrides,dict):raise ValueError('ATR_SESSION_OVERRIDES_OBJECT_REQUIRED')
        for day,spec in self.overrides.items():
            datetime.strptime(day,'%Y-%m-%d')
            if not isinstance(spec,dict) or not spec.get('source'):raise ValueError('SOURCED_ATR_SESSION_OVERRIDE_REQUIRED')
            if spec.get('closed') is not True:time.fromisoformat(spec['close'])

    @classmethod
    def from_config(cls,config):
        return cls(period=int(config.get('TANJA_DAILY_ATR_PERIOD','5')),
            session=config.get('TANJA_DAILY_ATR_SESSION','futures'),
            overrides=json.loads(config.get('TANJA_ATR_SESSION_OVERRIDES','{}')))


def trade_date(at,policy):
    local=datetime.fromtimestamp(at,NY)
    day=local.date()
    if policy.session=='futures' and local.hour>=18:day+=timedelta(days=1)
    return day


def bounds(day,policy):
    override=policy.overrides.get(day.isoformat(),{})
    if day.weekday()>=5 or override.get('closed') is True:return None
    start=datetime.combine(day-timedelta(days=1),time(18),NY) if policy.session=='futures' else datetime.combine(day,time(9,30),NY)
    end=datetime.combine(day,time.fromisoformat(override.get('close','17:00' if policy.session=='futures' else '16:00')),NY)
    if end<=start:raise ValueError('INVALID_ATR_SESSION_BOUNDARIES')
    return int(start.timestamp()),int(end.timestamp())


def expected_minutes(day,policy,cutoff=None):
    span=bounds(day,policy)
    if span is None:return []
    start,end=span
    if cutoff is not None:end=min(end,cutoff//60*60)
    halt_start=int(datetime.combine(day,time(16,15),NY).timestamp())
    halt_end=int(datetime.combine(day,time(16,30),NY).timestamp())
    return [t for t in range(start,end,60) if policy.session!='futures' or not halt_start<=t<halt_end]


def calculate(rows,*,cutoff,now,policy=None):
    policy=policy or Policy()
    if type(cutoff) is not int or cutoff%60 or type(now) not in (int,float) or not math.isfinite(now) or cutoff>now:
        raise ValueError('CAUSAL_ATR_CLOCK_REQUIRED')
    day=trade_date(cutoff-1,policy)
    result=dict(version=VERSION,symbol='MNQ',available_at=cutoff,checked_at=now,trade_date=day.isoformat(),
        period=policy.period,method='SMA of completed-session true ranges',session=policy.session,
        convention=f'{policy.period}-session SMA true range; '+('18:00–17:00 New York, scheduled 16:15–16:30 halt excluded' if policy.session=='futures' else '09:30–16:00 New York'),
        source='stored-confirmed-MNQ-M1',hours_source=HOURS_SOURCE,policy=asdict(policy),complete=False,
        state='NEEDS_HISTORY',reasons=[],atr=None,range=None,range_to_atr=None,reached=None,
        completed_sessions=[],missing_sessions=[])
    current=bounds(day,policy)
    if current is None or cutoff<=current[0]:
        result.update(state='OUTSIDE_ATR_SESSION',reasons=['ATR_CURRENT_SESSION_NOT_OPEN'])
        return result
    dates=[];previous=day
    while len(dates)<policy.period+1:
        previous-=timedelta(days=1)
        if bounds(previous,policy):dates.append(previous)
    dates.reverse();oldest=bounds(dates[0],policy)[0]
    visible={}
    for raw in rows:
        t=raw.get('time',raw.get('bar_open_ms',-1000)//1000)
        if type(t) is not int or t%60:raise ValueError('INVALID_ATR_MINUTE_CLOCK')
        if t<oldest or t+60>cutoff:continue
        received=raw.get('received_at',raw.get('received',t+60))
        if type(received) not in (int,float) or not math.isfinite(received) or received<t+60:raise ValueError('INVALID_ATR_RECEIPT_CLOCK')
        if received>now:continue
        if raw.get('symbol','MNQ')!='MNQ':raise ValueError('ATR_REQUIRES_MNQ_SERIES')
        values=[raw.get(k) for k in ('open','high','low','close')]
        if (not all(type(x) in (int,float) and math.isfinite(x) and x>0 and x*4==round(x*4) for x in values)
            or not values[2]<=min(values[0],values[3])<=max(values[0],values[3])<=values[1]):raise ValueError('INVALID_ATR_CANDLE')
        if t in visible:raise ValueError('DUPLICATE_ATR_MINUTE')
        visible[t]=dict(time=t,**{k:raw[k] for k in ('open','high','low','close')})
    def session(d,partial=False):
        start,end=bounds(d,policy);end=min(end,cutoff) if partial else end
        expected=expected_minutes(d,policy,end)
        missing=[t for t in expected if t not in visible]
        bars=[b for t,b in sorted(visible.items()) if start<=t and t+60<=end]
        s=dict(trade_date=d.isoformat(),start=start,end=end,expected_minutes=len(expected),observed_minutes=len(bars),
            complete=bool(expected) and not missing,missing_minutes=len(missing),
            high=max((b['high'] for b in bars),default=None),low=min((b['low'] for b in bars),default=None),
            close=bars[-1]['close'] if bars else None)
        if missing:result['missing_sessions'].append(dict(trade_date=d.isoformat(),missing_minutes=len(missing),first_missing=missing[0]))
        return s
    history=[session(d) for d in dates]
    active=session(day,True);result['current_session']=active
    if active['high'] is not None:result['range']=active['high']-active['low']
    for prev,s in zip(history,history[1:]):
        s=dict(s)
        s['previous_close']=prev['close']
        s['true_range']=max(s['high']-s['low'],abs(s['high']-prev['close']),abs(s['low']-prev['close'])) if s['complete'] and prev['complete'] else None
        result['completed_sessions'].append(s)
    result['complete']=active['complete'] and all(s['complete'] for s in history)
    result['input_sha256']=hashlib.sha256(json.dumps([visible[t] for t in sorted(visible)],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if not result['complete']:
        result['reasons']=['ATR_CONTIGUOUS_SESSION_HISTORY_REQUIRED']
        return result
    result['atr']=sum(s['true_range'] for s in result['completed_sessions'])/policy.period
    if result['atr']<=0:
        result.update(complete=False,state='INVALID_BASELINE',reasons=['ATR_POSITIVE_BASELINE_REQUIRED'])
        return result
    result.update(state='READY',range_to_atr=result['range']/result['atr'],reached=result['range']>=result['atr'],reasons=[])
    return result


def attach(packet,measurement):
    from entry_rules import fingerprint
    packet['daily_atr']=measurement
    packet['packet_id']=fingerprint({k:v for k,v in packet.items() if k!='packet_id'})
    return packet
