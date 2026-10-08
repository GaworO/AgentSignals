"""Causal market packets and a model-agnostic, review-only context boundary.

This module does not call an LLM or a broker. Model claims remain inferences;
valid references validate provenance, not the trader's discretionary judgment.
"""
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta
from hashlib import sha256
import json
import math
from tanya_replay import Bar, ET, aggregate, fvg_events, validate
from decision_engine import DecisionEngine, Fact
from setup_recipes import inversion_plan
from context_geometry import window_catalog, level_catalog, measure_smt, measure_range

TIMEFRAMES = (1, 2, 3, 5, 15, 60, 240)
CLAIMS = ('higher_timeframe_context_valid', 'setup_origin_at_range_extreme',
          'liquidity_event_complete', 'confirmation_accepted', 'structure_intact')


def canonical_hash(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def session_aggregate(bars, minutes, as_of):
    """Research H1/H4 convention: buckets anchored at 18:00 America/New_York.

    Exact minute completeness is required. Maintenance gaps cause omission, not
    an invented flat bar. This convention is NOT yet verified against her chart.
    Timestamp arithmetic uses UTC seconds from the ET session boundary.
    """
    if minutes not in (60, 240):
        raise ValueError('Only H1/H4 here')
    groups = defaultdict(list)
    step = minutes * 60
    for b in bars:
        if b.time + 60 > as_of:
            continue
        local = datetime.fromtimestamp(b.time, ET)
        anchor = local.replace(hour=18, minute=0, second=0, microsecond=0)
        if local < anchor:
            anchor -= timedelta(days=1)
        origin = int(anchor.timestamp())
        start = origin + ((b.time - origin) // step) * step
        groups[start].append(b)
    out = []
    for start, group in sorted(groups.items()):
        if [b.time for b in group] != list(range(start, start + step, 60)):
            continue
        out.append(Bar(start, group[0].open, max(b.high for b in group),
                       min(b.low for b in group), group[-1].close))
    return out


def build_packet(markets, as_of, max_bars=80):
    if type(as_of) is not int or type(max_bars) is not int or max_bars < 3:
        raise ValueError('Integer cutoff and at least three bars required')
    if set(markets) != {'ES', 'MNQ'}:
        raise ValueError('Both ES and MNQ required')
    evidence, coverage = {}, {}
    for symbol, all_bars in sorted(markets.items()):
        # Filter before validating or deriving any feature: future records must
        # not affect this packet, including its hash or coverage metadata.
        bars = [b for b in all_bars if b.time + 60 <= as_of]
        validate(bars)
        coverage[symbol] = dict(first_closed_start=bars[0].time if bars else None,
                                last_closed_end=bars[-1].time + 60 if bars else None,
                                closed_minutes=len(bars), omitted_aggregate_policy='require_every_minute',
                                retained_bars_per_timeframe={})
        for m in TIMEFRAMES:
            series = (aggregate(bars, m, as_of) if m < 60 else session_aggregate(bars, m, as_of))[-max_bars:]
            coverage[symbol]['retained_bars_per_timeframe'][str(m)] = len(series)
            for b in series:
                key = f'{symbol}:M{m}:{b.time}'
                evidence[key] = dict(kind='bar', symbol=symbol, timeframe=m,
                                     available_at=b.time + m * 60, **asdict(b))
        for m in (1, 2, 3, 5, 15):
            for event in fvg_events(bars, m, as_of):
                formation = int(event['zone_id'].split(':')[1])
                refs = [f'{symbol}:M{m}:{formation - n*m*60}' for n in (3, 2, 1)]
                if event['kind'] == 'IFVG':
                    refs.append(f'{symbol}:M{m}:{event["available_at"]-m*60}')
                if not all(ref in evidence for ref in refs):
                    continue
                key = f'{symbol}:{event["kind"]}:{event["zone_id"]}'
                evidence[key] = dict(event, symbol=symbol, evidence_ids=list(dict.fromkeys(refs)))
    evidence.update(window_catalog(markets, as_of))
    evidence.update(level_catalog(markets, as_of))
    packet = dict(schema_version=2, as_of=as_of, as_of_et=datetime.fromtimestamp(as_of, ET).isoformat(),
                  input_mode='market_only', coverage=coverage, evidence=evidence,
                  conventions=dict(higher_timeframes='18:00 ET session anchor; unverified against trader chart',
                                   fvg='three-candle wick gap; strict close inversion; local ET day reset',
                                   price_series='ES continuous and MNQ continuous; not interchangeable with NQ chart'),
                  missing_context=['timestamped news/calendar feed', 'verified trader HTF alignment',
                                   'verified contract-roll adjustments', 'full history for ATH and earlier weekly levels',
                                   'automatic SMT anchor selection', 'confirmed trader range/POI selection'],
                  labels_included=False, executable=False)
    packet['packet_id'] = canonical_hash(packet)
    return packet


def validate_response(packet, response):
    """Strict wire contract. A valid packet reference is not semantic proof."""
    unhashed = {k:v for k,v in packet.items() if k != 'packet_id'}
    if canonical_hash(unhashed) != packet.get('packet_id'):
        raise ValueError('Packet changed after creation')
    expected = {'schema_version','packet_id','as_of','bias','decision','setup','requires_smt',
                'selected_trigger_id','claims','missing_context','rationale'}
    version=response.get('schema_version')
    if version == 2:
        expected |= {'smt_selection','range_selection'}
    if set(response) != expected or version not in (1,2):
        raise ValueError('Response schema mismatch')
    if response['packet_id'] != packet['packet_id'] or response['as_of'] != packet['as_of']:
        raise ValueError('Response belongs to a different market snapshot')
    if response['bias'] not in ('long','short','neutral','unknown'):
        raise ValueError('Invalid bias')
    if response['decision'] not in ('wait','candidate','abstain'):
        raise ValueError('No execution intents accepted')
    if response['setup'] not in ('inversion','order_block','breaker','unknown'):
        raise ValueError('Unknown setup type')
    if response['requires_smt'] is not None and type(response['requires_smt']) is not bool:
        raise ValueError('SMT requirement must be explicit true/false/null')
    if not isinstance(response['rationale'], str) or not response['rationale'].strip():
        raise ValueError('Rationale required')
    if not isinstance(response['missing_context'], list) or not all(isinstance(x,str) and x for x in response['missing_context']):
        raise ValueError('Missing context must be a text list')
    evidence = packet['evidence']
    selected = response['selected_trigger_id']
    verified = {}
    if selected is not None:
        if selected not in evidence:
            raise ValueError('Unknown trigger reference')
        event = evidence[selected]
        if event['available_at'] > packet['as_of']:
            raise ValueError('Future trigger')
        if (response['setup'] != 'inversion' or event['kind'] != 'IFVG' or
            event['symbol'] != 'MNQ' or event['timeframe'] not in (1,2,3,5) or
            event['direction'] != response['bias']):
            raise ValueError('Trigger does not support the selected MNQ inversion direction')
        verified['gap_inverted'] = dict(value=True, known_at=event['available_at'], source=selected)
    if response['decision'] == 'candidate' and (selected is None or response['setup'] != 'inversion'):
        raise ValueError('This adapter supports only a referenced inversion candidate')
    if not isinstance(response['claims'], dict) or set(response['claims']) != set(CLAIMS):
        raise ValueError('All five context fields required; use null for unknown')
    for name, claim in response['claims'].items():
        if not isinstance(claim,dict) or set(claim) != {'value','evidence_ids','reason'}:
            raise ValueError('Claim schema mismatch')
        if claim['value'] is not None and type(claim['value']) is not bool:
            raise ValueError('Claim value must be true/false/null')
        refs = claim['evidence_ids']
        if not isinstance(refs,list) or not all(isinstance(x,str) for x in refs):
            raise ValueError('Reference list required')
        if not isinstance(claim['reason'],str) or not claim['reason'].strip():
            raise ValueError('Claim explanation required')
        if claim['value'] is not None and not refs:
            raise ValueError('Non-unknown claims need market references')
        for ref in refs:
            if ref not in evidence or evidence[ref]['available_at'] > packet['as_of']:
                raise ValueError('Unknown or future claim reference')
    geometry={}
    if version == 2 and response['smt_selection'] is not None:
        geometry['smt']=measure_smt(packet,response['smt_selection'],response['bias'])
        smt=geometry['smt']
        verified['smt_intact']=dict(value=smt['intact'],known_at=packet['as_of'],
                                   source=json.dumps(smt,sort_keys=True))
    if version == 2 and response['range_selection'] is not None:
        geometry['range']=measure_range(packet,response['range_selection'])
    return dict(valid=True, executable=False, mechanically_verified=verified,geometry=geometry,
                inferred_claims=response['claims'], semantic_fidelity_verified=False)


def review_response(packet, response, accept_inferences=False):
    """Two reports: conservative default or explicitly conditional shadow review.

    Shadow mode records AI provenance and can review a conditional entry; neither
    mode creates positions, fills or a broker order. Schema v2 checks supplied
    SMT windows; whether these match the trader's choice remains unverified.
    """
    result = validate_response(packet, response)
    if response['decision'] != 'candidate':
        return dict(state='ABSTAIN' if response['decision']=='abstain' else 'WAIT',
                    intent='NONE', executable=False, context_validation=result)
    event = packet['evidence'][response['selected_trigger_id']]
    engine = DecisionEngine()
    plan = inversion_plan('context:'+packet['packet_id'], response['bias'], packet['as_of'],
                          event['timeframe'], response['requires_smt'])
    engine.arm(plan, packet['as_of'])
    for name, item in result['mechanically_verified'].items():
        engine.observe(name, Fact(item['value'], item['known_at'], item['source'], 'market_data'), packet['as_of'], plan.id)
    for name, claim in response['claims'].items():
        source=json.dumps(claim,sort_keys=True)
        engine.observe(name, Fact(claim['value'] if accept_inferences else None,
                                  packet['as_of'], source, 'ai_inference'), packet['as_of'], plan.id)
    result_review = asdict(engine.evaluate(packet['as_of']))
    result_review.update(accept_inferences=accept_inferences, context_validation=result)
    return result_review
