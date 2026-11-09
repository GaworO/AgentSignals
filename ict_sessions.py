"""Shared ICT review windows and dedicated call caps; no execution authority."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
LONDON = ZoneInfo('Europe/London')
LONDON_EFFECTIVE_DAY = date(2026, 11, 9)
SESSION_CAPS = {'LONDON': 6, 'NYAM': 8, 'NYPM': 8}
SCHEDULE = ('London Tue–Fri 08:00–10:00 Europe/London (6 calls) · '
            'NYAM Mon–Fri 09:30–11:00 (8) · NYPM Mon–Fri 13:30–16:00 (8) America/New_York')


def london_day(day):
    day = date.fromisoformat(day) if isinstance(day, str) else day
    return day >= LONDON_EFFECTIVE_DAY and day.weekday() in (1, 2, 3, 4)


def daily_call_limit(day):
    if str(day) == '2026-10-30':
        return 12  # Preserve the original dated authorization and history.
    return 16 + (6 if london_day(day) else 0)


def windows(day):
    """(name, end, start), all timestamps UTC; each window uses its own zone."""
    day = date.fromisoformat(day) if isinstance(day, str) else day
    if day.weekday() >= 5:
        return []
    specs = [('NYAM', NY, 9, 30, 11, 0), ('NYPM', NY, 13, 30, 16, 0)]
    if london_day(day):
        specs.insert(0, ('LONDON', LONDON, 8, 0, 10, 0))
    return [(name,
             datetime(day.year, day.month, day.day, eh, em, tzinfo=zone).timestamp(),
             datetime(day.year, day.month, day.day, sh, sm, tzinfo=zone).timestamp())
            for name, zone, sh, sm, eh, em in specs]


def session(now):
    # All configured windows fall on the same NY and London calendar date.
    return next((w for w in windows(datetime.fromtimestamp(now, NY).date())
                 if w[2] <= now < w[1]), None)


def session_call_limit(name):
    return SESSION_CAPS[name]


def session_budgets(day, costs):
    return [dict(session=name, cap=session_call_limit(name),
                 used=sum(start <= r['at'] < end for r in costs),
                 starts_at=start, ends_at=end)
            for name, end, start in windows(day)]
