"""Read-only dashboard explanation of AI scheduling; never grants permission."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
from ict_sessions import windows, session


def next_window(now, intervals=()):
    """Next scheduled session point outside known blocks, not a call promise."""
    day = datetime.fromtimestamp(now, NY).replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in range(10):
        date = day + timedelta(days=offset)
        for name, b, a in windows(date.date()):
            candidate = max(now, a)
            for left, right in sorted(intervals):
                if left <= candidate < right:
                    candidate = right
            if candidate < b:
                return candidate
    return None


def describe(state, news, enabled):
    now = state['now']
    current = datetime.fromtimestamp(now, NY)
    in_session = session(now)
    intervals = [(w['start'], w['end']) for w in news.get('policy_windows', [])]
    # The calendar's +/-30 minute event boundaries are inclusive.
    events = {e['id']: e for e in news.get('events', []) + news.get('us_events_this_week', [])}
    intervals += [(e['at'] - 1800, e['at'] + 1801) for e in events.values()]
    result = dict(state='OBSERVING', title='AI watching for a setup',
                  reason='Paid reviews run only when the normal scheduling checks allow them.',
                  next_at=None, next_label=None, upcoming_at=None, upcoming_reason=None,
                  checked_at=now)

    def finish(code, title, reason, earliest=None, scheduled=False):
        result.update(state=code, title=title, reason=reason)
        if earliest is not None:
            result.update(next_at=next_window(earliest, intervals),
                          next_label='Next possible session' if scheduled else 'Earliest recheck')
        return result

    if news.get('status') == 'CLEAR' and news.get('next_blackout_at'):
        boundary = news['next_blackout_at']
        reasons = [w['reason'] for w in news.get('policy_windows', []) if w['start'] == boundary]
        names = [e['title'] for e in events.values() if e['at'] - 1800 == boundary]
        result.update(upcoming_at=boundary, upcoming_reason=(
            'NFP-week pause' if any(r.startswith('NFP_WEEK_') for r in reasons) else
            'FOMC morning pause' if 'FOMC_MORNING' in reasons else
            'News blackout: ' + ', '.join(names) if names else 'Scheduled news pause'))
    if not enabled:
        return finish('PAUSED', 'AI calls paused', 'Scanner disabled in settings. An operator must enable it.')
    if news.get('status') == 'POLICY_BLOCKED':
        active = news.get('active_policy_reasons', [])
        reason = ('NFP week: reviews resume Friday after 10:00 NY and the post-release news buffer; other checks still apply.'
                  if 'NFP_WEEK_UNTIL_FRIDAY_1000_AND_POST_RELEASE_BUFFER' in active else
                  'NFP week: no new AI reviews from Wednesday 11:00 NY through Friday.'
                  if 'NFP_WEEK_AFTER_WEDNESDAY_1100' in active else
                  'FOMC decision day: morning AI reviews are paused.')
        return finish('PAUSED', 'AI calls paused', reason, now, scheduled=True)
    if news.get('status') == 'BLACKOUT':
        names = [events[k]['title'] for k in news.get('active_blackouts', []) if k in events]
        return finish('PAUSED', 'AI calls paused',
                      'High-impact news: ' + (', '.join(names) or 'scheduled event') + '. The 30-minute news buffer applies.', now)
    if news.get('status') != 'CLEAR':
        return finish('CHECK_REQUIRED', 'AI calls paused',
                      'News calendar missing, stale or invalid. Waiting for a valid calendar update; resume time unknown.')
    if state.get('budget_anomaly'):
        return finish('CHECK_REQUIRED', 'AI calls paused', 'AI cost reconciliation needs investigation. Charges are preserved.')
    if state.get('calls_today', 0) >= state.get('daily_call_cap', 0) or state.get('estimated_or_reserved_usd', 0) >= state.get('daily_usd_cap', 0):
        tomorrow = (current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()
        return finish('PAUSED', 'AI calls paused', 'Daily AI call or dollar limit reached.', tomorrow, scheduled=True)
    if not in_session:
        return finish('PAUSED', 'AI calls paused', 'Outside London / NYAM / NYPM review hours. London runs Tuesday–Friday only.', now, scheduled=True)
    current_budget=next((b for b in state.get('session_budgets',[]) if b['session']==in_session[0]),None)
    if current_budget and current_budget['used']>=current_budget['cap']:
        return finish('PAUSED', 'Session AI limit reached',
                      f"{in_session[0]}: all {current_budget['cap']} reviews used, including reserved attempts.", in_session[1], scheduled=True)
    worker = state.get('worker') or {}
    if not 0 <= now - worker.get('at', 0) < 180:
        return finish('CHECK_REQUIRED', 'AI status needs checking', 'No recent scanner heartbeat. Use Check & recover AI.')
    last = state.get('last_check') or {}
    fresh = 0 <= now - last.get('at', 0) < 180
    code = last.get('state') if fresh else None
    reasons = {
        'THREE_REVIEWS_PER_30_MINUTES': 'Three reviews in 30 minutes. Waiting for the rolling allowance to reopen.',
        '90_SECOND_COOLDOWN': 'Waiting for the minimum 90 seconds between reviews.',
        'SESSION_CALL_CAP': 'The dedicated review allowance for this session has been used.',
        'SESSION_EIGHT_CALL_CAP': 'All eight reviews for this session have been used.',
        'DAILY_CALL_CAP': 'Daily review allowance reached.',
        'DAILY_CALL_CAP_INCLUDING_BURNED': 'Daily review allowance reached, including charged attempts without a result.',
        'DEFER_PRODUCTION_AI_BUSY': 'A production AI review is running; ICT waits for it to finish.',
        'DEFER_PRODUCTION_SETUP_QUEUE': 'A queued production setup has priority.',
        'DEFER_PRODUCTION_AI_UNRESOLVED': 'A production request has an unresolved outcome. Investigation is required.',
        'UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION': 'An ICT request is running or unresolved. Waiting for reconciliation; it will not be resent.',
        'WAIT_FIRST_CLOSED_CANDLE': 'Waiting for the first closed candle of the session.',
        'WATCHING_CANDLES_NO_NEW_EVENT': 'No fresh structure event. Watching candles until a new event or the normal reassessment.',
    }
    if code in reasons:
        return finish('WAITING', 'AI calls waiting', reasons[code])
    if code == 'BLOCKED':
        return finish('CHECK_REQUIRED', 'AI calls paused', 'Last scanner check could not complete. Use Check & recover AI for details.')
    if code and (code.startswith(('DEFER_', 'ICT_NEWS_')) or 'CAP' in code or 'BUDGET' in code or 'UNKNOWN' in code):
        return finish('CHECK_REQUIRED', 'AI calls waiting', 'A scanner check is holding reviews: ' + code.replace('_', ' ').lower() + '.')
    if not 0 <= now - (state.get('market') or {}).get('as_of', 0) <= 180:
        return finish('WAITING', 'AI calls waiting', 'Waiting for fresh closed-candle observations.')
    return result
