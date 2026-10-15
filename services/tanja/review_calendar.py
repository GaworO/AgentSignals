"""AI spending calendar, not a CME market-open or position-management calendar.

User policy: no new reviews on NY weekends or observed US federal holidays.
Also skip Good Friday (cash-market holiday). Some blocked dates have open
futures sessions. Intake and protective position management must not use this
calendar to shut down. Unknown years fail closed until the calendar is updated.

Verified 2026-10-09:
https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/
https://www.nyse.com/trade/hours-calendars
"""
from datetime import datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
VERSION = 'us-observed-plus-good-friday-2026-2027-v1'
SUPPORTED_YEARS = (2026, 2027)
HOLIDAYS = {
    '2026-01-01': 'New Year’s Day',
    '2026-01-19': 'Martin Luther King Jr. Day',
    '2026-02-16': 'Washington’s Birthday',
    '2026-04-03': 'Good Friday',
    '2026-05-25': 'Memorial Day',
    '2026-06-19': 'Juneteenth',
    '2026-07-03': 'Independence Day (observed)',
    '2026-09-07': 'Labor Day',
    '2026-10-12': 'Columbus Day',
    '2026-11-11': 'Veterans Day',
    '2026-11-26': 'Thanksgiving',
    '2026-12-25': 'Christmas Day',
    '2027-01-01': 'New Year’s Day',
    '2027-01-18': 'Martin Luther King Jr. Day',
    '2027-02-15': 'Washington’s Birthday',
    '2027-03-26': 'Good Friday',
    '2027-05-31': 'Memorial Day',
    '2027-06-18': 'Juneteenth (observed)',
    '2027-07-05': 'Independence Day (observed)',
    '2027-09-06': 'Labor Day',
    '2027-10-11': 'Columbus Day',
    '2027-11-11': 'Veterans Day',
    '2027-11-25': 'Thanksgiving',
    '2027-12-24': 'Christmas Day (observed)',
    '2027-12-31': 'New Year’s Day 2028 (observed)',
}


def review_day(now):
    local = datetime.fromtimestamp(now, NY)
    day = local.date().isoformat()
    name = HOLIDAYS.get(day)
    if local.year not in SUPPORTED_YEARS:
        status = 'CALENDAR_UPDATE_REQUIRED'
    elif local.weekday() >= 5:
        status = 'WEEKEND_NO_AI'
    elif name:
        status = 'HOLIDAY_NO_AI'
    else:
        status = 'READY'
    return dict(status=status, new_reviews_allowed=status == 'READY',
                date=day, timezone='America/New_York', holiday=name,
                version=VERSION, valid_through='2027-12-31',
                scope='Observed US federal holidays plus Good Friday; not exchange hours')
