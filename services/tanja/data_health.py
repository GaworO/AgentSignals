"""Read-only operational alerts; no exchange holiday calendar or order authority."""
from datetime import datetime
from zoneinfo import ZoneInfo


def data_health(state, now):
    ny = datetime.fromtimestamp(now, ZoneInfo('America/New_York'))
    day, hour = ny.weekday(), ny.hour
    pause = day == 5 or (day == 4 and hour >= 17) or (day == 6 and hour < 18) or (day < 4 and hour == 17)
    messages, levels = [], []
    for symbol in ('ES', 'NQ', 'MNQ'):
        feed = state['feeds'].get(symbol, {})
        age = feed.get('age_seconds')
        if age is None:
            messages.append(f'{symbol}: no candles received. Check the TradingView alert and webhook delivery log.')
            levels.append('warning' if pause else 'error')
        elif age > 120:
            if pause:
                messages.append(f'{symbol}: no fresh candles during the standard scheduled pause.')
                levels.append('info')
            else:
                messages.append(f'{symbol}: latest candle is {int(age // 60)} minutes old. Check TradingView alert status and delivery.')
                levels.append('error')
    for item in state.get('diagnostics', []):
        if 0 <= now-item['at'] <= 300:
            messages.append(f'Recent intake/processing issue: {item["kind"]} — {item["message"]}')
            levels.append('warning')
            break
    jobs = state.get('jobs', [])
    if jobs and jobs[0]['status'] == 'failed' and now-jobs[0]['cutoff'] <= 300:
        messages.append('Latest context processing failed; check Market data diagnostics.')
        levels.append('error')
    fresh_pair = all(state['feeds'].get(s, {}).get('state') == 'CURRENT' for s in ('ES','MNQ'))
    if fresh_pair:
        if not jobs or now-jobs[0]['cutoff'] > 180 or (jobs[0]['status'] != 'done' and now-jobs[0]['cutoff'] > 180):
            messages.append('ES/MNQ are arriving but context processing is delayed. Check the worker and paired candle timestamps.')
            levels.append('warning')
    level = max(levels, key={'info':1,'warning':2,'error':3}.get) if levels else 'ok'
    return dict(level=level,messages=list(dict.fromkeys(messages)),scheduled_pause=pause,
                checked_at=now,stale_after_seconds=120,holiday_calendar_verified=False)


def account_snapshot(directory):
    """Manual private snapshot for display/research sizing, never live authority."""
    import json, math
    try:
        value=json.loads((directory/'account_snapshot.json').read_text())
        keys=('balance','minimum_balance','target_balance')
        if not all(type(value.get(k)) in (int,float) and math.isfinite(value[k]) and value[k]>0 for k in keys):
            return None
        if not isinstance(value.get('as_of'),str) or len(value['as_of'])>100:
            return None
        return dict(**{k:value[k] for k in keys},as_of=value['as_of'],manual=True,broker_verified=False)
    except (OSError,ValueError,TypeError,AttributeError):
        return None
