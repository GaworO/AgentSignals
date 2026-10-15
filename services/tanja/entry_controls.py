"""User's ATR entry restriction; a measurement is not inferred by the model."""
import math
from datetime import datetime
from zoneinfo import ZoneInfo


def continuation_check(measurement, trade_kind, now):
    if trade_kind=='reversal':return dict(state='CLEAR',reasons=[],applies=False)
    if trade_kind!='continuation':return dict(state='BLOCKED',reasons=['TRADE_KIND_REQUIRED'],applies=True)
    m=measurement or {}; at=m.get('available_at')
    valid=(m.get('symbol')=='MNQ' and type(at) is int and 0<=now-at<=120
        and m.get('complete') is True and bool(m.get('source')) and bool(m.get('convention'))
        and type(m.get('atr')) in (int,float) and math.isfinite(m['atr']) and m['atr']>0
        and type(m.get('range')) in (int,float) and math.isfinite(m['range']) and m['range']>=0)
    if valid:
        valid=datetime.fromtimestamp(at,ZoneInfo('America/New_York')).date()==datetime.fromtimestamp(now,ZoneInfo('America/New_York')).date()
    reason='DAILY_ATR_MEASUREMENT_REQUIRED' if not valid else 'DAILY_ATR_REACHED_CONTINUATION_BLOCKED' if m['range']>=m['atr'] else None
    return dict(state='BLOCKED' if reason else 'CLEAR',reasons=[reason] if reason else [],applies=True)
