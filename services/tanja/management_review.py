"""Directional user-selected stop proposals; not broker orders.
Strict M1 one-left/one-right pivots and a one-tick buffer are engineering
conventions. Short news offset is the directional extension of the user rule.
No positions, fills, event calendar or quote feed are inferred by this module.
"""
import math


def propose_stop(bars, *, direction, entry, current_stop, opened_at, as_of,
                 market_price, price_known_at, news_at=None, news_known_at=None):
    def no(reason):return dict(action='NONE',reason=reason,is_order=False)
    if direction not in ('long','short'):return no('INVALID_DIRECTION')
    sign=1 if direction=='long' else -1
    prices=(entry,current_stop,market_price)
    if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 or v*4!=round(v*4) for v in prices):return no('INVALID_PRICE')
    if any(type(v) is not int or v<0 for v in (opened_at,as_of,price_known_at)) or opened_at>as_of:return no('INVALID_CLOCK')
    if not 0<=as_of-price_known_at<=2:return no('NEEDS_FRESH_QUOTE')
    choices=[]
    if news_at is not None:
        if any(type(v) is not int or v<0 for v in (news_at,news_known_at)):return no('INVALID_NEWS_CLOCK')
        if news_at-60<=as_of<news_at and news_known_at<=news_at-60 and opened_at<=news_at-60:
            choices.append((entry-sign*1.5,'PRE_NEWS_ENTRY_OFFSET',news_at-60))
    closed=[]
    for b in bars:
        t=b.get('time')
        if type(t) is not int or t<0 or t%60:return no('INVALID_M1_CLOCK')
        if t+60>as_of:continue
        received=b.get('received_at',t+60)
        if type(received) not in (int,float) or not math.isfinite(received) or received<t+60:return no('INVALID_RECEIPT')
        if received>as_of:continue
        if any(type(b.get(k)) not in (int,float) or not math.isfinite(b[k]) or b[k]<=0 or b[k]*4!=round(b[k]*4) for k in ('open','high','low','close')):return no('INVALID_M1_PRICE')
        if not b['low']<=min(b['open'],b['close'])<=max(b['open'],b['close'])<=b['high']:return no('INVALID_M1_OHLC')
        if closed and t<=closed[-1]['time']:return no('UNSORTED_OR_DUPLICATE_M1')
        closed.append(b)
    side='low' if sign==1 else 'high'
    for i in range(len(closed)-2,0,-1):
        a,b,c=closed[i-1:i+2]
        if b['time']<opened_at or b['time']-a['time']!=60 or c['time']-b['time']!=60:continue
        if not sign*b[side]<min(sign*a[side],sign*c[side]):continue
        # Latest confirmed swing only. Broken/gapped swings do not revive an older pivot.
        later=closed[i+1:]
        continuous=all(y['time']-x['time']==60 for x,y in zip(closed[i:],closed[i+1:]))
        if continuous and not any(sign*(x[side]-b[side])<=0 for x in later):
            choices.append((b[side]-sign*.25,'LATEST_CONFIRMED_M1_SWING',c['time']+60))
        break
    choices=[x for x in choices if x[0]>0 and sign*(x[0]-current_stop)>0 and sign*(market_price-x[0])>0]
    if not choices:return no('NO_VALID_TIGHTENING')
    price,reason,known=max(choices,key=lambda x:sign*x[0])
    return dict(action='REVIEW_STOP_PROPOSAL',stop_price=price,reason=reason,anchor_available_at=known,
                effective_not_before=as_of,is_order=False,requires_broker_confirmation=True)
