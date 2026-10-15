"""Causal discovery of the two agreed sequences, shared with the May replay.

This selects geometry only. A saved AI watch must explicitly authorize this
selector, its family, direction, stop/entry rules and fixed objectives. No
nearest-target proxy, inferred approval, model call or broker action is made.
"""
from copy import deepcopy
from bisect import bisect_right
import json
from tanya_replay import Bar, aggregate, fvg_events, validate
from context_layer import session_aggregate
from entry_patterns import PROFILES, validate_pattern
from entry_rules import fingerprint

VERSION = 'two-family-sequence-scanner-v1'
HORIZON_SECONDS = 10800


def key(symbol, tf, at):
    return f'{symbol}:M{tf}:{at}'


def bar_evidence(symbol, tf, b):
    return dict(kind='bar', symbol=symbol, timeframe=tf, time=b.time,
                available_at=b.time+tf*60, open=b.open, high=b.high,
                low=b.low, close=b.close)


def pivots(symbol, rows):
    out = []
    for i in range(2, len(rows)-2):
        group = rows[i-2:i+3]
        if any(b['time']-a['time'] != 60 for a,b in zip(group,group[1:])):
            continue
        b = rows[i]
        for side in ('high','low'):
            other = [x[side] for j,x in enumerate(group) if j != 2]
            if (b[side] > max(other) if side == 'high' else b[side] < min(other)):
                out.append(dict(kind='pivot', symbol=symbol, timeframe=1,
                    pivot_time=b['time'], side=side, price=b[side],
                    available_at=group[-1]['available_at'],
                    id=f'{symbol}:PIVOT:{b["time"]}:{side}',
                    evidence_ids=[key(symbol,1,x['time']) for x in group]))
    return out


class SequenceScanner:
    def __init__(self, markets, cutoff, now):
        self.series = {}; self.lookup = {}; self.ps = {}; self.events = []
        self.all_evidence = {}
        if 'NQ' not in markets or 'MNQ' not in markets or 'ES' not in markets:
            raise ValueError('THREE_DISTINCT_MARKET_SERIES_REQUIRED')
        for symbol in ('NQ','MNQ','ES'):
            rows = [Bar(**{k:b[k] for k in ('time','open','high','low','close')})
                for b in markets[symbol] if b['time']+60 <= cutoff
                and b.get('received_at',b['time']+60) <= now]
            validate(rows)
            self.series[symbol] = {tf:[bar_evidence(symbol,tf,b) for b in
                (aggregate(rows,tf,cutoff) if tf < 60 else session_aggregate(rows,tf,cutoff))]
                for tf in (1,15,60,240)}
            self.lookup[symbol] = {b['time']:b for b in self.series[symbol][1]}
            self.ps[symbol] = pivots(symbol,self.series[symbol][1])
            for tf, bars in self.series[symbol].items():
                self.all_evidence.update({key(symbol,tf,b['time']):b for b in bars})
            self.all_evidence.update({p['id']:p for p in self.ps[symbol]})
            if symbol == 'NQ':
                self.events = [e for e in fvg_events(rows,1,cutoff)
                               if e['kind'] == 'IFVG' and e['available_at'] == cutoff]
                self.gaps = [dict(g, symbol='NQ', id='NQ:FVG:'+g['zone_id'],
                    evidence_ids=[key('NQ',15,g['available_at']-n*900) for n in (3,2,1)])
                    for g in fvg_events(rows,15,cutoff) if g['kind'] == 'FVG']
        self.htf = {tf:{b['time']:b for b in self.series['NQ'][tf]} for tf in (15,60,240)}
        self.all_evidence.update({g['id']:g for g in self.gaps})
        self.sweeps = []; self.structures = []; used = set(); last_sweep = {}
        by_side = {side:[p for p in self.ps['NQ'] if p['side'] == side] for side in ('low','high')}
        available = {side:[p['available_at'] for p in ps] for side,ps in by_side.items()}
        for b in self.series['NQ'][1]:
            for side in ('low','high'):
                index = bisect_right(available[side],b['time'])-1
                if index < 0: continue
                p = by_side[side][index]
                if b['time']-p['pivot_time'] > HORIZON_SECONDS: continue
                direction = 'long' if side == 'low' else 'short'
                sign = 1 if side == 'high' else -1
                if sign*(b[side]-p['price']) > 0 and ('sweep',p['id']) not in used:
                    used.add(('sweep',p['id']))
                    m = dict(bar=b,pivot=p,direction=direction)
                    self.sweeps.append(m); last_sweep[direction] = m
                    self.structures.append(dict(manipulation=m,bar=b,pivot=p,
                        direction=direction,role='liquidity_sweep'))
                direction = 'long' if side == 'high' else 'short'
                if sign*(b['close']-p['price']) > 0 and ('break',p['id']) not in used:
                    used.add(('break',p['id']))
                    m = last_sweep.get(direction)
                    if m and 0 <= b['time']-m['bar']['time'] <= HORIZON_SECONDS:
                        self.structures.append(dict(manipulation=m,bar=b,pivot=p,
                            direction=direction,role='directional_break'))

    # visible/packet/sequence below are the same causal selectors used by replay.

    def evidence_for(self, event, sequence):
        refs = [key('NQ',1,int(event['zone_id'].split(':')[1])-n*60) for n in (3,2,1)]
        refs.append(key('NQ',1,event['available_at']-60))
        event_id = 'NQ:IFVG:'+event['zone_id']
        source = dict(self.all_evidence)
        source[event_id] = dict(event,symbol='NQ',evidence_ids=refs)
        wanted = {event_id}
        def collect(value):
            if isinstance(value,dict):
                for v in value.values(): collect(v)
            elif isinstance(value,list):
                for v in value: collect(v)
            elif isinstance(value,str) and value in source: wanted.add(value)
        collect(sequence)
        def add(ref):
            if ref in out: return
            e = source[ref]; out[ref] = deepcopy(e)
            if e.get('kind') == 'FVG': out[ref].pop('id',None)
            for child in e.get('evidence_ids',[]): add(child)
            if e.get('kind') == 'bar' and e.get('symbol') == 'NQ':
                paired = key('MNQ',e['timeframe'],e['time'])
                if paired in source: add(paired)
        out = {}
        for ref in sorted(wanted): add(ref)
        return out

    def find(self, family, trigger, event=None):
        event = event or next((e for e in self.events if e['available_at'] == trigger['available_at']
            and e['direction'] == trigger['direction'] and e['zone_id'] == trigger['zone_id']),None)
        if event is None: raise ValueError('CURRENT_FIRST_M1_INVERSION_REQUIRED')
        result = self.sequence(event,family)
        return result, self.evidence_for(event,result[4])

    def visible(self,s,now,horizon=10800):
        return [b for b in self.series[s][1] if now-horizon<=b['time'] and b['available_at']<=now]


    def packet(self,trigger):
        ev={key('NQ',1,b['time']):b for b in self.visible('NQ',trigger['available_at'])}
        return dict(as_of=trigger['available_at'],evidence=ev)


    def sequence(self,event,family):
        trigger=self.lookup['NQ'][int(event['available_at'])-60];direction=event['direction'];sign=1 if direction=='long' else -1
        rows=self.visible('NQ',trigger['time']);packet=self.packet(trigger);ev=packet['evidence']
        selected=None
        if family==PROFILES[0]:
            options=[s for s in self.structures if s['direction']==direction and 0<trigger['time']-s['bar']['time']<=10800]
            for s in sorted(options,key=lambda x:(x['bar']['time'],x['role']=='directional_break'),reverse=True):
                sb=s['bar'];mb=s['manipulation']['bar'];p=s['pivot'];mp=s['manipulation']['pivot']
                retests=[b for b in rows if b['time']>=sb['available_at'] and b['low']<=sb['high'] and b['high']>=sb['low']]
                if not retests:continue
                r=retests[-1]
                ev[p['id']]=p;ev[mp['id']]=mp
                ev[key('NQ',1,mb['time'])]=mb;ev[key('NQ',1,sb['time'])]=sb
                seq=dict(retracement_zone_id=key('NQ',1,sb['time']),retracement_bar_id=key('NQ',1,r['time']),
                         manipulation=dict(level=dict(evidence_id=mp['id'],field='price'),bar_id=key('NQ',1,mb['time']),side='below' if sign==1 else 'above'),
                         structure=dict(swing_id=p['id'],bar_id=key('NQ',1,sb['time']),role=s['role'],mode='wick' if s['role']=='liquidity_sweep' else 'close'),objective=None,objective_touch_bar_id=None)
                selected=(seq,r,mb,f'{family}:{p["id"]}:{sb["time"]}:{s["role"]}:{direction}');break
        else:
            gaps=[g for g in self.gaps if g['direction']==('bull' if sign==1 else 'bear') and 0<=trigger['time']-g['available_at']<=10800]
            objectives=[]
            for tf,limit in ((60,8),(240,4)):
                known=[b for b in self.series['NQ'][tf] if b['available_at']<=trigger['time']][-limit:]
                objectives.extend((tf,b) for b in known)
            for g in sorted(gaps,key=lambda x:x['available_at'],reverse=True):
                formation=[self.htf[15].get(g['available_at']-n*900) for n in (3,2,1)]
                if any(b is None for b in formation) or sign*(formation[1]['close']-formation[1]['open'])<=0:continue
                retests=[b for b in rows if b['time']>=g['available_at'] and b['low']<=g['upper'] and b['high']>=g['lower']]
                if not retests:continue
                r=retests[-1];touches=[]
                for tf,obj in objectives:
                    field='low' if sign==1 else 'high';level=obj[field]
                    touches.extend((b,tf,obj,field) for b in rows if obj['available_at']<=b['time'] and b['available_at']<=formation[1]['time'] and b['low']<=level<=b['high'])
                if not touches:continue
                touch,tf,obj,field=max(touches,key=lambda x:(x[0]['time'],x[2]['available_at']))
                ev[g['id']]=g
                for b in formation:ev[key('NQ',15,b['time'])]=b
                ev[key('NQ',tf,obj['time'])]=obj
                seq=dict(retracement_zone_id=g['id'],retracement_bar_id=key('NQ',1,r['time']),manipulation=None,structure=None,
                         objective=dict(evidence_id=key('NQ',tf,obj['time']),field=field),objective_touch_bar_id=key('NQ',1,touch['time']))
                selected=(seq,r,touch,f'{family}:{g["id"]}:{tf}:{obj["time"]}:{direction}');break
        if selected is None:raise ValueError('NO_QUALIFYING_SEQUENCE')
        seq,retest,origin,setup_id=selected
        selection=dict(setup_profile=family,analysis_symbol='NQ',direction=direction,setup_sequence=seq)
        def point(ref,symbol):
            e=ev[ref['evidence_id']]
            if e['symbol']!=symbol or e['available_at']>trigger['time']:raise ValueError('NONCAUSAL_SEQUENCE_ANCHOR')
            return dict(price=e[ref['field']],available_at=e['available_at'],symbol=symbol)
        measurements=validate_pattern(packet,selection,trigger,point)
        # Verify the strict inversion independently of the event catalogue.
        born=int(event['zone_id'].split(':')[1]);a,b,c=[self.lookup['NQ'].get(born-n*60) for n in (3,2,1)]
        if any(x is None for x in (a,b,c)):raise ValueError('MISSING_INVERSION_FORMATION')
        boundary=a['low'] if sign==1 else a['high']
        if not (c['high']<a['low'] if sign==1 else c['low']>a['high']) or sign*(trigger['close']-boundary)<=0:raise ValueError('STRICT_INVERSION_REQUIRED')
        path=[self.lookup['NQ'].get(t) for t in range(born,trigger['time'],60)]
        if any(x is None for x in path) or any(sign*(x['close']-boundary)>0 for x in path):raise ValueError('NOT_FIRST_CONTIGUOUS_INVERSION')
        return trigger,retest,origin,setup_id,seq,measurements


def attach(packet, markets, *, cutoff, now, max_candidates=4, max_extra_bytes=40000):
    """Expose bounded sequence evidence to AI; never mark a trade approved."""
    if 'NQ' not in markets: return packet
    scanner = SequenceScanner(markets,cutoff,now)
    evaluations = []; accepted = 0; extra = {}
    for event in sorted(scanner.events,key=lambda e:e['zone_id'],reverse=True):
        for family in reversed(PROFILES):
            record = dict(candidate_id='NQ:IFVG:'+event['zone_id'],setup_profile=family,
                direction=event['direction'],available_at=cutoff,timeframe=1,
                state='SEQUENCE_NOT_FOUND',executable=False)
            try:
                result,evidence = scanner.find(family,event,event)
                addition = {k:v for k,v in evidence.items() if k not in packet['evidence']}
                proposed = dict(extra,**addition)
                if accepted >= max_candidates or len(json.dumps(proposed,separators=(',',':')).encode()) > max_extra_bytes:
                    record.update(state='SCANNER_EVIDENCE_LIMIT',reasons=['BOUNDED_PACKET_RETAINED_OTHER_SEQUENCES'])
                else:
                    for ref,e in evidence.items():
                        if ref in packet['evidence'] and packet['evidence'][ref] != e:
                            # Pivots from the shared scanner use a separate ID
                            # convention; bars/gaps must remain exactly immutable.
                            raise ValueError('SCANNER_EVIDENCE_CONFLICT')
                    extra = proposed; accepted += 1
                    record.update(state='SEQUENCE_MEASURED_NEEDS_AI_CONTEXT',reasons=[],setup_id=result[3],
                        sequence=result[4],measurements=result[5],
                        explanation=('Manipulation → swing break → latest retracement → strict M1 inversion'
                            if family == PROFILES[0] else 'H1/H4 objective touch → directional M15 gap → latest retracement → strict M1 inversion'))
            except (ValueError,KeyError,TypeError) as exc: record['reasons'] = [str(exc)]
            evaluations.append(record)
    packet['evidence'].update(extra)
    packet['setup_scanner'] = dict(version=VERSION,context_approved=False,orders_enabled=False,
        horizon_seconds=HORIZON_SECONDS,selection='same causal geometry as the May two-family replay',
        candidates=evaluations,selection_assumptions=dict(pivots='strict two-left/two-right, known after close',
            manipulation_zone='selected structure-break candle range',htf_objectives='last eight H1 / four H4 highs or lows',
            retest='latest completed zone touch before inversion starts',targets='AI-selected MNQ objectives; no nearest-target proxy'))
    packet['packet_id'] = fingerprint({k:v for k,v in packet.items() if k != 'packet_id'})
    return packet


