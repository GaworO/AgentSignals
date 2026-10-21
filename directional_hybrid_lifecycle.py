"""Durable context cache and causal invalidation for the existing review worker."""
import json
import directional_hybrid_context as context


def init(db):
    db.execute('CREATE TABLE IF NOT EXISTS hybrid_market(id INTEGER PRIMARY KEY CHECK(id=1), observed REAL, packet TEXT, derived TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS hybrid_events(id TEXT PRIMARY KEY, available REAL, payload TEXT)')
    columns={r[1] for r in db.execute('PRAGMA table_info(reviews)')}
    if 'review_kind' not in columns:
        db.execute("ALTER TABLE reviews ADD COLUMN review_kind TEXT NOT NULL DEFAULT 'LEGACY'")


def refresh(service, packet):
    derived=context.build(packet)
    with service.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT * FROM hybrid_market WHERE id=1').fetchone()
        if old and json.loads(old['packet'])['as_of']>packet['as_of']:
            raise ValueError('hybrid_source_regressed')
        # Same-cutoff corrections invalidate a pending review, rather than
        # retrospectively claiming that revised data was available previously.
        if old:
            prior=json.loads(old['packet'])
            if prior['as_of']==packet['as_of'] and prior['source_packet_id']!=packet['source_packet_id']:
                event=dict(kind='DATA_REVISION',available_at=packet['as_of'],id=context.sha([prior['source_packet_id'],packet['source_packet_id']]))
                db.execute('INSERT OR IGNORE INTO hybrid_events VALUES(?,?,?)',(event['id'],event['available_at'],json.dumps(event)))
        for event in derived['structure_events']['value']:
            db.execute('INSERT OR IGNORE INTO hybrid_events VALUES(?,?,?)',(event['id'],event['available_at'],json.dumps(event)))
        db.execute('INSERT OR REPLACE INTO hybrid_market VALUES(1,?,?,?)',
                   (service.clock(),json.dumps(packet),json.dumps(derived)))
    return derived


def reason(service, packet, setup=None):
    now=service.clock()
    with service.connect() as db:
        latest=db.execute('SELECT * FROM hybrid_market WHERE id=1').fetchone()
        if not latest or latest['observed']<service.started_at or not 0<=now-latest['observed']<=15:
            return 'HYBRID_CONTEXT_NOT_REFRESHED'
        current=json.loads(latest['packet'])
        if not 0<=now-current['as_of']<=120:return 'HYBRID_CONTEXT_STALE'
        if current['as_of']<packet['as_of']:return 'HYBRID_SOURCE_REGRESSED'
        if current['as_of']==packet['as_of'] and current['source_packet_id']!=packet['source_packet_id']:
            return 'HYBRID_DATA_REVISED'
        # Any later structural event is a conservative invalidator of cached
        # reasoning; no model is allowed to ignore a new contradictory event.
        events=db.execute('SELECT payload FROM hybrid_events WHERE available>? AND available<=?',(packet['as_of'],current['as_of'])).fetchall()
        if any(json.loads(r[0]).get('symbol') in ('NQ','MNQ',None) for r in events):
            return 'HYBRID_NEW_STRUCTURE_EVENT'
        # Data revision can share the original cutoff.
        if db.execute("SELECT 1 FROM hybrid_events WHERE available=? AND payload LIKE '%DATA_REVISION%'",(packet['as_of'],)).fetchone():
            return 'HYBRID_DATA_REVISED'
    if setup:
        if now>=min(setup['expiry_ms']/1000,packet['review_expires_at']):return 'EXPIRED'
        rows=sorted((e for e in current['evidence'].values() if e.get('kind')=='bar' and e.get('symbol')=='MNQ'
                     and e.get('timeframe')==1 and e['time']>=setup['activation_ms']/1000),key=lambda e:e['time'])
        expected=list(range(int(setup['activation_ms']/1000),int(current['as_of']),60))
        if [e['time'] for e in rows]!=expected:return 'HYBRID_SETUP_HISTORY_INCOMPLETE'
        for e in rows:
            if (e['low']<=setup['stop_price'] or e['high']>=setup['target_price']) if setup['direction']=='LONG' else (e['high']>=setup['stop_price'] or e['low']<=setup['target_price']):
                return 'HYBRID_ORIGINAL_BRACKET_TOUCHED'
    return None


def context_due(service, packet, session, day):
    """At most one session opening review plus one material-event review/day.

    A setup gets priority. Valid context is reusable as advice only; a setup
    still needs its own exact review. No hourly spending loop.
    """
    with service.connect() as db:
        rows=db.execute("SELECT * FROM reviews WHERE day=? AND review_kind='CONTEXT' ORDER BY started",(day,)).fetchall()
    if len(rows)>=3:return False
    packets=[json.loads(json.loads(r['request'])['input'][1]['content']) for r in rows]
    if not any(p.get('hybrid_session')==session['name'] for p in packets):return True
    last=packets[-1]
    return (not any(p.get('hybrid_context_reason')=='MATERIAL_EVENT' for p in packets)
            and any(e['available_at']>last['as_of'] and e['symbol'] in ('NQ','MNQ')
                    for e in context.build(packet)['structure_events']['value']))


def reusable_context(service, packet):
    with service.connect() as db:
        rows=db.execute("SELECT * FROM reviews WHERE review_kind='CONTEXT' AND status='validated' ORDER BY started DESC LIMIT 3").fetchall()
    for row in rows:
        p=json.loads(json.loads(row['request'])['input'][1]['content'])
        if service.clock()<p['review_expires_at'] and reason(service,p) is None:
            return dict(review_id=row['id'],as_of=p['as_of'],expires_at=p['review_expires_at'],
                        advisory_only=True,decision=json.loads(row['decision']))
    return None
