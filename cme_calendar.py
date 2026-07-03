"""
CME Globex EQUITY-INDEX (MNQ) trading-hours calendar — isolated add-on (v21 style: never breaks intake).

Why: the v20 heartbeat only knew weekends + the daily maintenance halt, so every exchange holiday
(e.g. 2026-07-03 early close 12:00 CT) fired a false "FEED STALE" alert. This module answers ONE
question — "should TradingView be delivering MNQ bars right now?" — with the holiday layer included.

All rules are evaluated on the EXCHANGE clock (America/Chicago, DST-correct). This is deliberate:
CME publishes hours in CT, and the agent's fixed UTC-4 chart clock drifts 1h from civil time in
winter, which made the old maintenance-halt window (17:00-18:00 "ET") wrong Nov-Mar.

Weekly schedule (equity index futures):
  open  Sun 17:00 CT  ->  close Fri 16:00 CT
  daily maintenance halt 16:00-17:00 CT
Holiday layer:
  FULL_CLOSED  = trade dates with no session at all (Good Friday, Christmas, New Year's Day)
  EARLY_CLOSE  = dates where equity-index products HALT intraday (usually 12:00 CT; 12:15 CT on
                 Christmas Eve / day-after-Thanksgiving) and reopen 17:00 CT for the next trade date

⚠ CME finalizes each holiday's hours ~2 weeks ahead (cmegroup.com/trading-hours.html). Entries below
are the standard pattern; VERIFY before each holiday. Uncertain 2027 entries are marked. To patch a
date WITHOUT redeploying, use ENV:
  CME_FULL_CLOSED = "2027-12-31,2028-01-17"          (comma list of YYYY-MM-DD)
  CME_EARLY_CLOSE = "2027-06-18=12:00,2027-12-23=12:15"  (comma list of YYYY-MM-DD=HH:MM in CT)
ENV entries are ADDED to the built-ins (an ENV early-close on a built-in date overrides its time).
"""
import os, datetime as dt
from zoneinfo import ZoneInfo

CT = ZoneInfo('America/Chicago')   # exchange clock — the ONLY tz these rules are true in

def _d(s): return dt.date.fromisoformat(s)

# --- trade dates with NO equity-index session at all ---
FULL_CLOSED = {
    _d('2026-12-25'),   # Christmas (Fri)
    _d('2027-01-01'),   # New Year's Day (Fri)
    _d('2027-03-26'),   # Good Friday
    _d('2027-12-24'),   # Christmas observed (Dec 25 = Sat) — VERIFY when CME publishes
}

# --- intraday halts: date -> halt minute-of-day in CT (equity-index products) ---
def _m(h, mi=0): return h * 60 + mi
EARLY_CLOSE = {
    _d('2026-07-03'): _m(12),      # Independence Day observed (Jul 4 = Sat)
    _d('2026-09-07'): _m(12),      # Labor Day
    _d('2026-11-26'): _m(12),      # Thanksgiving
    _d('2026-11-27'): _m(12, 15),  # day after Thanksgiving
    _d('2026-12-24'): _m(12, 15),  # Christmas Eve (no evening reopen — Dec 25 closed; automatic via trade-date rule)
    _d('2027-01-18'): _m(12),      # MLK Day
    _d('2027-02-15'): _m(12),      # Presidents' Day
    _d('2027-05-31'): _m(12),      # Memorial Day
    _d('2027-06-18'): _m(12),      # Juneteenth observed (Jun 19 = Sat) — UNCERTAIN, VERIFY
    _d('2027-07-05'): _m(12),      # Independence Day observed (Jul 4 = Sun)
    _d('2027-09-06'): _m(12),      # Labor Day
    _d('2027-11-25'): _m(12),      # Thanksgiving
    _d('2027-11-26'): _m(12, 15),  # day after Thanksgiving
    _d('2027-12-23'): _m(12, 15),  # eve of observed Christmas — UNCERTAIN, VERIFY
}

# --- ENV overrides (patch a date without redeploy) ---
try:
    for _s in filter(None, (x.strip() for x in os.environ.get('CME_FULL_CLOSED', '').split(','))):
        FULL_CLOSED.add(_d(_s))
    for _s in filter(None, (x.strip() for x in os.environ.get('CME_EARLY_CLOSE', '').split(','))):
        _date, _hm = _s.split('='); _h, _mi = _hm.split(':')
        EARLY_CLOSE[_d(_date)] = _m(int(_h), int(_mi))
except Exception as _e:
    print('[cme_calendar] ENV parse err (ignored):', _e, flush=True)

def _trade_date(t_ct):
    """Bars from 17:00 CT onward belong to the NEXT calendar day's trade date (Globex convention).
    This one rule auto-handles eves: Dec 24 evening session would be the Dec 25 trade date -> closed."""
    d = t_ct.date()
    return d + dt.timedelta(days=1) if t_ct.hour >= 17 else d

def market_open(t=None):
    """True when CME Globex equity-index (MNQ) should be printing bars at aware-datetime t (default: now)."""
    t = dt.datetime.now(CT) if t is None else t.astimezone(CT)
    wd = t.weekday(); m = t.hour * 60 + t.minute
    if wd == 5:                    return False   # Saturday
    if wd == 6 and m < _m(17):     return False   # Sunday before 17:00 CT open
    if wd == 4 and m >= _m(16):    return False   # Friday after 16:00 CT close
    if _m(16) <= m < _m(17):       return False   # daily maintenance halt
    if _trade_date(t) in FULL_CLOSED: return False
    halt = EARLY_CLOSE.get(t.date())
    if halt is not None and halt <= m < _m(17): return False   # holiday intraday halt until evening reopen
    return True

def status(t=None):
    """Small debug dict for /status: open flag + which rule applies today (if any)."""
    t = dt.datetime.now(CT) if t is None else t.astimezone(CT)
    d = t.date(); td = _trade_date(t)
    note = ''
    if td in FULL_CLOSED: note = f'holiday: {td} closed'
    elif d in EARLY_CLOSE:
        hm = EARLY_CLOSE[d]; note = f'early close today {hm // 60:02d}:{hm % 60:02d} CT'
    return {'open': market_open(t), 'ct': t.strftime('%Y-%m-%d %H:%M CT'), 'note': note}
