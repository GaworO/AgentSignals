"""Validated Forex Factory weekly calendar; no order or notification side effects.

JSON carries offsets. The publisher's XML export uses UTC (verified against
its ICS TZID timestamps); never interpret those clock times in the host zone.
"""
import datetime as dt
import json
import math
from pathlib import Path
import threading
import time
import xml.etree.ElementTree as ET

UTC = dt.timezone.utc
BASE = 'https://nfs.faireconomy.media/ff_calendar_thisweek.'
REFRESH_SECONDS = 6 * 3600
RETRY_SECONDS = 15 * 60
MAX_BYTES = 1_000_000
IMPACTS = {'high', 'medium', 'low', 'holiday', 'non-economic'}


def week_start(now):
    day = dt.datetime.fromtimestamp(now, UTC).date()
    return day - dt.timedelta(days=(day.weekday() + 1) % 7)


def normalize(body, kind, now):
    if len(body) > MAX_BYTES:
        raise ValueError('oversized_calendar')
    if kind == 'json':
        rows = json.loads(body)
    else:
        root = ET.fromstring(body)
        if root.tag != 'weeklyevents':
            raise ValueError('invalid_xml_root')
        rows = [{node.tag: (node.text or '').strip() for node in event} for event in root]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2000:
        raise ValueError('empty_or_invalid_calendar')
    start = week_start(now)
    raw = []
    current_week_rows = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('invalid_event')
        title = row.get('title')
        impact = str(row.get('impact', '')).lower()
        if not isinstance(title, str) or not title.strip() or impact not in IMPACTS:
            raise ValueError('invalid_event_metadata')
        if kind == 'json':
            when = dt.datetime.fromisoformat(row['date'])
            if when.tzinfo is None:
                raise ValueError('missing_timezone')
            when = when.astimezone(UTC)
            date = when.date()
        else:
            date = dt.datetime.strptime(row['date'], '%m-%d-%Y').date()
            when = None
            if impact == 'high':
                # All-day/tentative high-impact releases must not silently vanish.
                when = dt.datetime.strptime(row['date'] + ' ' + row.get('time', '').lower(),
                                            '%m-%d-%Y %I:%M%p').replace(tzinfo=UTC)
        # The publisher selects a New York week, but XML dates are UTC.
        # Late Saturday NY events can therefore spill into the next UTC Sunday.
        if not start <= date < start + dt.timedelta(days=8):
            raise ValueError('calendar_week_mismatch')
        current_week_rows += date < start + dt.timedelta(days=7)
        if impact == 'high':
            if when.hour == 0 and when.minute == 0 or any(x in title.lower() for x in ('tentative', 'all day')):
                raise ValueError('uncertain_high_impact_time')
            raw.append(dict(epoch=when.timestamp(), title=title,
                            country=row.get('country') or row.get('currency') or '',
                            impact='high', source='ForexFactory'))
    if not current_week_rows:
        raise ValueError('calendar_week_mismatch')
    return dict(received_at=now, week_start=start.isoformat(), source='ForexFactory/' + kind.upper(),
                event_count=len(rows), raw_events=raw)


class Calendar:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.lock = threading.Lock()
        self.snapshot = None
        self.last_attempt = None
        self.error = None
        try:
            value = json.loads(self.path.read_text())
            if self._valid(value, self.clock()):
                self.snapshot = value
        except (OSError, ValueError, TypeError, KeyError):
            pass

    @staticmethod
    def _valid(value, now):
        try:
            if not isinstance(value, dict) or value['week_start'] != week_start(now).isoformat():
                return False
            received = float(value['received_at'])
            if not math.isfinite(received) or not 0 <= now - received:
                return False
            if value['source'] not in ('ForexFactory/JSON', 'ForexFactory/XML') or not 1 <= value['event_count'] <= 2000:
                return False
            if not isinstance(value['raw_events'], list):
                return False
            for row in value['raw_events']:
                epoch = float(row['epoch'])
                if not math.isfinite(epoch) or not week_start(now) <= dt.datetime.fromtimestamp(epoch, UTC).date() < week_start(now) + dt.timedelta(days=8) or not row['title'] or row['impact'] != 'high':
                    return False
            return True
        except (ValueError, TypeError, KeyError, OverflowError):
            return False

    def age_hours(self):
        now = self.clock()
        if not self._valid(self.snapshot, now):
            return None
        return (now - self.snapshot['received_at']) / 3600

    def refresh(self, client):
        # All callers share the lock/backoff; a failed source cannot be hit per bar.
        with self.lock:
            now = self.clock()
            age = self.age_hours()
            if age is not None and age < REFRESH_SECONDS / 3600:
                return
            if self.last_attempt is not None and now - self.last_attempt < RETRY_SECONDS:
                return
            self.last_attempt = now
            errors = []
            for kind in ('json', 'xml'):
                try:
                    response = client.get(BASE + kind, timeout=15)
                    if response.status_code != 200:
                        raise ValueError('HTTP_' + str(response.status_code))
                    value = normalize(response.content, kind, self.clock())
                    self.snapshot = value
                    self.error = None
                    try:
                        self.path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = self.path.with_suffix('.tmp')
                        temporary.write_text(json.dumps(value))
                        temporary.replace(self.path)
                    except OSError:
                        print('[cal] cache write failed; using validated in-memory calendar', flush=True)
                    return
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, ValueError) and str(exc).startswith('HTTP_') else type(exc).__name__
                    errors.append(kind.upper() + ':' + reason)
            self.error = '; '.join(errors)
            print('[cal] fetch failed (' + self.error + '); retry in 15m', flush=True)

    def view(self):
        value = self.snapshot or {}
        age = self.age_hours()
        raw = value.get('raw_events', []) if age is not None else []
        return dict(at=dt.datetime.fromtimestamp(value['received_at'], UTC).replace(tzinfo=None) if age is not None else None,
                    events=[(row['epoch'], row['title']) for row in raw], raw_events=raw,
                    source=value.get('source'), error=self.error, age_hours=age)
