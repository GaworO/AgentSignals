"""Public weekly schedule, receipt-timestamped and fail-closed. No AI/orders."""
import hashlib
import json
import time
import urllib.request
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo

NY=ZoneInfo('America/New_York')
URL='https://nfs.faireconomy.media/ff_calendar_thisweek.json'
VERSION='ff-usd-high-5m-before-after-v1'
REFRESH=3600
MAX_AGE=7200
BEFORE=AFTER=300


def week_start(now):
    day=datetime.fromtimestamp(now,NY).date()
    return day-timedelta(days=(day.weekday()+1)%7)


def normalize(raw,received_at):
    if not isinstance(raw,list) or not 1<=len(raw)<=2000:raise ValueError('Empty or invalid calendar')
    start=week_start(received_at);events=[]
    for item in raw:
        if not isinstance(item,dict):raise ValueError('Invalid calendar event')
        dt=datetime.fromisoformat(item['date'])
        if dt.tzinfo is None:raise ValueError('Event timezone required')
        local=dt.astimezone(NY)
        if not start<=local.date()<start+timedelta(days=7):raise ValueError('Calendar week mismatch')
        impact=item['impact'];country=item['country'];title=item['title']
        if impact not in ('High','Medium','Low','Holiday','Non-Economic'):raise ValueError('Unknown impact')
        if not isinstance(title,str) or not title.strip() or len(title)>250:raise ValueError('Invalid event name')
        if country not in ('USD','All') or impact!='High':continue
        # Some feeds encode undetermined/all-day events as midnight; do not
        # pretend their exact release time is known.
        uncertain=local.hour==0 and local.minute==0 or any(x in title.lower() for x in ('tentative','all day'))
        event=dict(title=title,currency=country,impact=impact,at=int(dt.timestamp()),date=local.date().isoformat(),time_uncertain=uncertain,known_at=received_at)
        event['id']=hashlib.sha256(json.dumps({k:v for k,v in event.items() if k!='known_at'},sort_keys=True).encode()).hexdigest()[:16]
        events.append(event)
    return dict(source=URL,version=VERSION,received_at=received_at,week_start=start.isoformat(),week_end=(start+timedelta(days=7)).isoformat(),events=sorted(events,key=lambda x:x['at']),raw_sha256=hashlib.sha256(json.dumps(raw,sort_keys=True).encode()).hexdigest())


def assess(snapshot,now,error=None):
    out=dict(version=VERSION,state='BLOCKED',reasons=[],source=URL,events=[],next_event=None,entry_block_before_seconds=BEFORE,entry_block_after_seconds=AFTER,orders_enabled=False)
    def blocked(reason):out['reasons']=[reason];return out
    if error:return blocked('NEWS_FETCH_FAILED')
    if not snapshot:return blocked('NEWS_CALENDAR_MISSING')
    out.update(received_at=snapshot['received_at'],week_start=snapshot['week_start'])
    if not 0<=now-snapshot['received_at']<=MAX_AGE:return blocked('NEWS_CALENDAR_STALE_OR_FUTURE')
    if snapshot['week_start']!=week_start(now).isoformat():return blocked('NEWS_CALENDAR_WRONG_WEEK')
    today=datetime.fromtimestamp(now,NY).date().isoformat()
    events=[e for e in snapshot['events'] if e['date']==today]
    out['events']=events
    if any(e['time_uncertain'] for e in events):return blocked('NEWS_TIME_UNCERTAIN')
    future=[e for e in events if e['at']>now]
    if future:
        e=future[0];out.update(next_event=e,pre_news_protect_at=e['at']-60,entry_valid_until=e['at']-BEFORE)
    previous=[e for e in events if e['at']+AFTER<=now]
    out['context_not_before']=max((e['at']+AFTER for e in previous),default=None)
    active=[e for e in events if e['at']-BEFORE<=now<e['at']+AFTER]
    if active:
        out['active_events']=active
        return blocked('NEWS_ENTRY_BLACKOUT')
    out.update(state='CLEAR',reasons=[])
    return out


class NewsCalendar:
    def __init__(self,store):
        self.store=store
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS news_snapshots(id INTEGER PRIMARY KEY,received REAL,status TEXT,payload TEXT,error TEXT)')
    def state(self,now):
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM news_snapshots WHERE received<=? ORDER BY received DESC,id DESC LIMIT 1',(now,)).fetchone()
        return assess(json.loads(row['payload']) if row and row['payload'] else None,now,row['error'] if row else None)
    def refresh(self,fetch=None,clock=time.time):
        now=clock()
        with self.store.connect() as db:
            latest=db.execute('SELECT MAX(received) FROM news_snapshots').fetchone()[0]
        if latest is not None and now-latest<REFRESH:return False
        try:
            if fetch is None:
                request=urllib.request.Request(URL,headers={'User-Agent':'TanjaResearchCalendar/1.0'})
                with urllib.request.urlopen(request,timeout=15) as response:
                    raw=response.read(1000001)
                    if len(raw)>1000000:raise ValueError('Oversized calendar')
                    raw=json.loads(raw)
            else:raw=fetch()
            received=clock();snapshot=normalize(raw,received);status='ok';error=None
            # Preserve when this exact schedule was first received. Changed
            # release times get a new ID and cannot inherit earlier knowledge.
            with self.store.connect() as db:
                prior=db.execute("SELECT payload FROM news_snapshots WHERE status='ok' AND received<=? ORDER BY received DESC,id DESC LIMIT 1",(received,)).fetchone()
            known={e['id']:e['known_at'] for e in json.loads(prior[0])['events']} if prior else {}
            for event in snapshot['events']:
                event['known_at']=min(received,known.get(event['id'],received))
        except Exception:
            received=clock();snapshot=None;status='error';error='Calendar download or validation failed'
        with self.store.connect() as db:
            db.execute('INSERT INTO news_snapshots(received,status,payload,error) VALUES(?,?,?,?)',(received,status,json.dumps(snapshot) if snapshot else None,error))
            db.execute('DELETE FROM news_snapshots WHERE id NOT IN (SELECT id FROM news_snapshots ORDER BY id DESC LIMIT 1000)')
        return True
    def run(self,stop):
        while not stop.is_set():
            try:self.refresh()
            except Exception:self.store.note('news_calendar','Calendar unavailable; entry guard remains closed')
            stop.wait(60)


def apply_news(result,news):
    result['scheduled_news']=news
    reasons=list(news['reasons'])
    gate=result.get('ai_context') or {}
    if news.get('context_not_before') and (gate.get('source_cutoff') or 0)<news['context_not_before']:
        reasons.append('NEWS_REQUIRES_FRESH_AI_CONTEXT')
    result['news_entry_reasons']=reasons
    if result['state']=='PLAN_READY':
        if reasons:
            result.update(state='NEWS_BLOCKED',reasons=reasons);result['compiled']['plan']=None
        elif news.get('entry_valid_until'):
            end=min(result['compiled']['plan']['context_valid_until'],news['entry_valid_until'])
            result['compiled']['plan']['context_valid_until']=end
            result['selected']['selection']['context_valid_until']=end
    return result


def propose_stop_with_calendar(calendar,*,now,**position):
    """For a caller-supplied position/quote only. Never submits an order."""
    from management_review import propose_stop
    news=calendar.state(now);event=news.get('next_event')
    result=propose_stop(**position,as_of=now,news_at=event['at'] if event else None,news_known_at=int(event['known_at']) if event else None)
    result['scheduled_news']=news
    result['news_protection_verified']=False
    return result
