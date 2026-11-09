"""Read-only replay of persisted ICT reviews. Never starts AI or broker workers."""
import collections
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
TFS = (1, 2, 3, 5, 15, 60, 240)
SYMBOLS = ('MNQ', 'NQ', 'ES')
RID = re.compile(r'ICT_INDEPENDENT_NYAM_(\d{8})_\d+(?::IFVG)?\Z')


def numeric(v):
    return type(v) in (int, float) and math.isfinite(v)


def record_path(root, rid):
    m = RID.fullmatch(rid)
    if not m:
        raise ValueError('Invalid review ID')
    return Path(root) / ('ict_2022_nyam_' + m[1]) / (rid.split(':')[0] + '.json')


def selected(record, family):
    decision, validation = record.get('decision') or {}, record.get('validation') or {}
    if family == 'IFVG':
        return decision.get('ifvg_review') or {}, validation.get('ifvg_validation') or {}
    return {k: v for k, v in decision.items() if k != 'ifvg_review'}, {k: v for k, v in validation.items() if k != 'ifvg_validation'}


def index(root):
    rows, unreadable = [], 0
    for p in Path(root).glob('ict_2022_nyam_*/ICT_INDEPENDENT_NYAM_*.json'):
        try:
            r = json.loads(p.read_text()); rid = r['id']
            if not RID.fullmatch(rid) or not numeric(r.get('requested_at')):
                raise ValueError('Malformed record')
            for family in ('ICT2022', 'IFVG'):
                d, v = selected(r, family)
                if family == 'IFVG' and not d:
                    continue
                rows.append(dict(id=rid + (':IFVG' if family == 'IFVG' else ''), family=family,
                    requested_at=r['requested_at'], date=datetime.fromtimestamp(r['requested_at'], NY).date().isoformat(),
                    decision=d.get('decision') or 'NO_RESPONSE', state=r.get('state'), validation=v.get('state'),
                    summary=d.get('summary') or r.get('error') or 'No saved response',
                    session=(r.get('packet') or {}).get('session_name'), has_packet=bool((r.get('packet') or {}).get('evidence'))))
        except (OSError, ValueError, KeyError, TypeError):
            unreadable += 1
    return dict(reviews=sorted(rows, key=lambda r: (r['requested_at'], r['id']), reverse=True), unreadable_records=unreadable)


def aggregate(bars, tf):
    """Only contiguous complete buckets. Never invent flat candles across gaps."""
    groups = collections.defaultdict(list)
    for b in sorted(bars, key=lambda b: b[0]):
        t = b[0]; d = datetime.fromtimestamp(t, NY)
        anchor = d.replace(hour=18, minute=0, second=0, microsecond=0)
        if d < anchor:
            anchor -= timedelta(days=1)
        origin = int(anchor.timestamp()) if tf >= 60 else 0
        start = int(origin + (t - origin) // (tf * 60) * tf * 60)
        groups[start].append(b)
    return [[t, bs[0][1], max(b[2] for b in bs), min(b[3] for b in bs), bs[-1][4]]
            for t, bs in sorted(groups.items()) if [b[0] for b in bs] == list(range(t, t + tf * 60, 60))]


def candle(e):
    values = [e.get(k) for k in ('time', 'open', 'high', 'low', 'close')]
    if not all(numeric(v) for v in values) or values[2] < max(values[1], values[4]) or values[3] > min(values[1], values[4]):
        return None
    return values


def archive(config, start, end):
    from ict_history import readonly
    root = Path(config.get('DATA_DIR', '/data/builder50'))
    result, gaps = {}, []
    for sym, table in (('MNQ', 'bars'), ('NQ', 'collection_bars'), ('ES', 'bars')):
        bars, tickers = {}, set()
        try:
            with readonly(root / 'market_context' / 'tanja.sqlite3') as db:
                for row in db.execute(f'SELECT payload FROM {table} WHERE symbol=? AND start>=? AND start<? ORDER BY start', (sym, start, end)):
                    try:
                        p = json.loads(row[0]); t = p['bar_open_ms'] / 1000
                        if p.get('confirmed') is not True or p.get('bar_close_ms', float('inf')) / 1000 > end:
                            continue
                        b = candle(dict(time=t, **{k: p.get(k) for k in ('open', 'high', 'low', 'close')}))
                        if b:
                            bars[t] = b; tickers.add(p.get('ticker') or sym)
                    except (ValueError, KeyError, TypeError):
                        continue
        except (sqlite3.Error, OSError):
            gaps.append(sym + ': market-context archive unavailable')
        # Research observations are a fallback only; do not overwrite the primary archive.
        if not bars:
            try:
                with readonly(root / 'research' / 'ict_cloud.sqlite3') as db:
                    for row in db.execute('SELECT body FROM candles WHERE symbol=? AND time>=? AND time<? ORDER BY time', (sym, start, end)):
                        try:
                            p = json.loads(row[0]); b = candle(p)
                            if b and p.get('timeframe') == 1 and numeric(p.get('available_at')) and p['available_at'] <= end:
                                bars[b[0]] = b
                        except (ValueError, TypeError):
                            continue
            except (sqlite3.Error, OSError):
                pass
        ordered = sorted(bars.values())
        result[sym] = dict(bars={str(tf): aggregate(ordered, tf) for tf in TFS}, tickers=sorted(tickers),
            minute_count=len(ordered), gaps=[dict(start=a[0]+60, end=b[0]) for a,b in zip(ordered, ordered[1:]) if b[0]-a[0]>60])
        if not ordered:
            gaps.append(sym + ': no archived candles for this day')
    return result, gaps


def detail(root, config, rid, now=None):
    path = record_path(root, rid)
    r = json.loads(path.read_text())
    if r.get('id') != rid.split(':')[0]:
        raise ValueError('Saved record identity mismatch')
    family = 'IFVG' if rid.endswith(':IFVG') else 'ICT2022'
    decision, validation = selected(r, family)
    packet = r.get('packet') or {}; packet_source = 'SAVED_PACKET'
    # Prefer the literal sent user message if a pre-compaction packet was also saved.
    for item in (r.get('request') or {}).get('input') or []:
        if isinstance(item, dict) and item.get('role') == 'user' and isinstance(item.get('content'), str):
            try:
                sent = json.loads(item['content'])
                if isinstance(sent, dict) and isinstance(sent.get('evidence'), dict):
                    packet = sent; packet_source = 'SAVED_API_INPUT'; break
            except ValueError:
                pass
    evidence = packet.get('evidence') or {}; cutoff = packet.get('as_of')
    at = r.get('requested_at')
    if not numeric(at):
        raise ValueError('Saved request timestamp missing')
    day = datetime.fromtimestamp(at, NY).replace(hour=0, minute=0, second=0, microsecond=0)
    start, end = day.timestamp(), (day + timedelta(days=1)).timestamp()
    now = datetime.now().timestamp() if now is None else now
    archived, gaps = archive(config, start, min(end, now))
    packet_bars = {s: {str(tf): [] for tf in TFS} for s in SYMBOLS}
    unsafe = []
    for eid, e in evidence.items():
        if not isinstance(e, dict) or e.get('kind') != 'bar':
            continue
        sym, tf = e.get('symbol'), str(e.get('timeframe')); b = candle(e)
        if sym not in packet_bars or tf not in packet_bars[sym] or not b:
            unsafe.append(eid); continue
        if not numeric(cutoff) or not numeric(e.get('available_at')) or e['available_at'] > cutoff or b[0] + int(tf)*60 > cutoff:
            unsafe.append(eid); continue
        packet_bars[sym][tf].append(b + [eid])
    for groups in packet_bars.values():
        for bs in groups.values():
            bs.sort()
    plan = decision.get('plan') or {}; zones = []
    ids = plan.get('entry_fvg_bar_ids') or []
    if len(ids) == 3 and all(i in evidence and i not in unsafe for i in ids):
        a, b, c = [evidence[i] for i in ids]
        if all(candle(x) for x in (a,b,c)):
            lo, hi = (a['high'], c['low']) if c['low'] > a['high'] else (c['high'], a['low'])
            if hi > lo:
                zones.append(dict(low=lo, high=hi, start=a['time'], confirmed=c.get('available_at'),
                    tf=a.get('timeframe'), symbol=a.get('symbol'), family=family,
                    inversion=(evidence.get(plan.get('inversion_bar_id')) or {}).get('available_at')))
    conflicts = []
    for sym in SYMBOLS:
        by_time = {b[0]: b for b in archived[sym]['bars']['1']}
        for b in packet_bars[sym]['1']:
            if b[0] in by_time and b[:5] != by_time[b[0]]:
                conflicts.append(b[5])
    from ict_history import broker_records
    orders, broker_gaps = broker_records(config)
    orders = [o for o in orders if o.get('review_id') == r['id'] and o.get('family') == family]
    fixed_tp = None
    entry, stop = plan.get('entry_price'), plan.get('stop_price')
    if all(numeric(x) for x in (entry, stop)) and ((plan.get('direction') == 'LONG' and entry > stop) or (plan.get('direction') == 'SHORT' and stop > entry)):
        fixed_tp = entry + 2 * (entry - stop)
    return dict(id=rid, family=family, state=r.get('state'), date=day.date().isoformat(), day_start=start, day_end=end,
        requested_at=at, received_at=r.get('received_at'), finished_at=r.get('finished_at'), as_of=cutoff,
        model=(r.get('request') or {}).get('model'), recorded_commit=r.get('commit'), error=r.get('error'),
        decision=decision, validation=validation, evidence=evidence, packet_bars=packet_bars, archive=archived,
        archive_gaps=gaps, unsafe_bar_ids=unsafe, archive_conflicts=conflicts, zones=zones,
        hypothetical_fixed_2r=fixed_tp, broker_orders=orders, broker_gaps=broker_gaps,
        context={k:packet.get(k) for k in ('coverage','market_roles','conventions','daily_atr','atr_review_context','missing_context','assessment_kind','trigger_event','session_name','contract_configured','scheduled_news')},
        read_only=True, packet_source=packet_source, chart_basis='SAVED_AI_PACKET_AND_SEPARATE_ARCHIVE_NOT_EXECUTABLE_QUOTES')


def register(app, private, cloud):
    from flask import Response, jsonify
    def reviews():
        return jsonify(index(cloud.root))
    def review(rid):
        try:
            return jsonify(detail(cloud.root, cloud.config, rid))
        except FileNotFoundError:
            response = jsonify(error='Saved review not found'); response.status_code = 404; return response
        except (ValueError, TypeError, KeyError):
            response = jsonify(error='Invalid or incomplete saved review'); response.status_code = 400; return response
    app.add_url_rule('/ict-shadow/scanner/index', 'ict_scanner_index', private(reviews))
    app.add_url_rule('/ict-shadow/scanner/review/<rid>', 'ict_scanner_review', private(review))
    for name, mime in (('js', 'application/javascript'), ('css', 'text/css')):
        def asset(name=name, mime=mime):
            return Response(Path(__file__).with_name('ict_scanner.'+name).read_text(), mimetype=mime)
        app.add_url_rule('/ict-shadow/scanner.'+name, 'ict_scanner_'+name, private(asset))
