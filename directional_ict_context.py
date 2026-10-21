"""Evidence index for the existing Directional reviewer, never a trading signal.

No inferred trader dealing range, SMT selection, or executable quote. Every
reference resolves to a timestamped observation; missing selections stay UNKNOWN.
"""
import copy
from datetime import datetime
from zoneinfo import ZoneInfo

VERSION = 'directional-ict-context-v1'
TOPICS = ('htf_narrative', 'dealing_range', 'liquidity_sweeps', 'structure_displacement',
          'fvg_ifvg', 'atr_expansion', 'ny_session', 'es_nq_alignment',
          'opposing_liquidity', 'invalidation_geometry')


def frozen_context(order, payload):
    """Whitelist frozen observations, excluding replay outcomes and future state."""
    snap = payload.get('v3_snapshot') or {}
    cutoff = order['activation_ms']
    result = {}

    def causal(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key.endswith('_ms') and type(item) in (int,float) and item>cutoff:
                    return False
                if not causal(item):return False
        elif isinstance(value,list):
            return all(causal(item) for item in value)
        return True

    def add(name, value, at, path):
        known = type(at) in (int, float) and 0 < at <= cutoff and value is not None and causal(value)
        result[name] = dict(status='RECORDED' if known else 'UNKNOWN',
                            value=copy.deepcopy(value) if known else None,
                            available_at_ms=at if known else None,
                            source=dict(order_id=order['order_id'], path=path),
                            reason=None if known else 'missing_or_future_frozen_evidence')

    stamp = snap.get('selected_at_ms')
    add('source_event', snap.get('source_event'),
        (snap.get('source_event') or {}).get('trigger_ms', cutoff + 1) + 60000,
        'payload.v3_snapshot.source_event')
    # Snapshot selection time, not constituent formation time, is availability.
    for name in ('frozen_dol', 'boundary_context', 'source_contract_identity'):
        add(name, snap.get(name), stamp, 'payload.v3_snapshot.' + name)
    dol = snap.get('dol_tag') or {}
    for name in ('current_dol', 'next_dol_after_delivery', 'opposite_side_liquidity_previously_taken',
                 'narrative', 'current_price'):
        add(name, dol.get(name), dol.get('evaluated_at_ms'), 'payload.v3_snapshot.dol_tag.' + name)
    return result


def attach(packet, setup, fingerprint):
    """Attach a causal evidence map to an already validated market-only packet."""
    p = copy.deepcopy(packet)
    ev = p['evidence']; cutoff = p['as_of']
    activation = setup['activation_ms'] / 1000
    if activation > cutoff:
        raise ValueError('setup_context_before_activation')
    frozen_ids = {}
    for name, node in setup.get('frozen_context', {}).items():
        if node['status'] != 'RECORDED':
            continue
        at = node['available_at_ms'] / 1000
        if at > activation or at > cutoff:
            raise ValueError('future_setup_evidence')
        key = 'DIRECTIONAL:' + setup['order_id'] + ':' + name
        ev[key] = dict(kind='directional_frozen_observation', symbol='MNQ',
                       available_at=int(at), value=node['value'], source=node['source'],
                       convention='Recorded detector observation, not independently verified ICT fact')
        frozen_ids[name] = key
    geometry_id = 'DIRECTIONAL:' + setup['order_id'] + ':geometry'
    ev[geometry_id] = dict(kind='directional_geometry', symbol='MNQ', available_at=int(activation),
        source=dict(order_id=setup['order_id'], path='continuation_orders'),
        value={k:setup[k] for k in ('order_id','direction','contract','entry_price','stop_price',
                                   'target_price','activation_ms','expiry_ms')})
    bos_id = 'DIRECTIONAL:' + setup['order_id'] + ':entry_rules'
    rules = setup.get('entry_rules') or {}
    if rules and rules.get('bos_available_ms', float('inf')) <= cutoff * 1000:
        ev[bos_id] = dict(kind='directional_entry_rules', symbol='MNQ', available_at=int(activation),
                         source=dict(order_id=setup['order_id'],path='payload.entry_rules'), value=rules)

    def refs(predicate):
        return [k for k,e in ev.items() if predicate(e)]

    def field(ids=(), missing=()):
        references = []
        for key in dict.fromkeys(k for k in ids if k in ev):
            references.append(dict(evidence_id=key))
        return dict(status=('PARTIAL' if missing else 'AVAILABLE') if references else 'UNKNOWN',
                    as_of=cutoff, source='evidence[evidence_id].source; otherwise ict_context.sources.market_packet',
                    timestamp='evidence[evidence_id].available_at', references=references,
                    missing=list(missing))

    htf = refs(lambda e:e.get('timeframe') in (60,240))
    windows = refs(lambda e:e.get('kind')=='window')
    liquidity = refs(lambda e:e.get('kind') in ('pivot','level','window','sweep'))
    gaps = refs(lambda e:e.get('kind') in ('FVG','IFVG','HTF_FVG'))
    topics = {
      'htf_narrative':field(htf + [frozen_ids.get('source_event'),frozen_ids.get('current_dol')],
          ['Trader-verified HTF bias is UNKNOWN; read closed H1/H4 and assess reversal as well as continuation.']),
      'dealing_range':field(windows,
          ['Selected ICT dealing-range anchors are UNKNOWN. Session/window midpoint is not automatically the ICT equilibrium.']),
      'liquidity_sweeps':field(liquidity + [frozen_ids.get('source_event'),frozen_ids.get('opposite_side_liquidity_previously_taken')],
          ['Liquidity is OHLC inference, not observed resting orders; verify sweep/reclaim sequence from cited bars.']),
      'structure_displacement':field([bos_id] + refs(lambda e:e.get('kind')=='pivot'),
          ['BOS body fraction is a measurement, not by itself HTF MSS or ICT confirmation.']),
      'fvg_ifvg':field(gaps + [frozen_ids.get('boundary_context'),geometry_id],
          [] if any(e.get('kind')=='IFVG' for e in ev.values()) else ['IFVG evidence UNKNOWN.']),
      'atr_expansion':field(missing=['ATR measurement UNKNOWN until a causal timestamped measurement is supplied.']),
      'ny_session':field(windows),
      'es_nq_alignment':field(refs(lambda e:e.get('symbol') in ('ES','NQ') and e.get('kind') in ('bar','pivot','window')),
          ['Approved paired SMT reference anchors UNKNOWN; same-direction candles alone do not prove SMT.']),
      'opposing_liquidity':field([frozen_ids.get('current_dol'),frozen_ids.get('next_dol_after_delivery')] +
          refs(lambda e:e.get('kind') in ('pivot','level','HTF_FVG')),
          ['Frozen directional DOL is not automatically opposing supply; identify actual obstacles and their symbols.']),
      'invalidation_geometry':field([geometry_id,bos_id,frozen_ids.get('boundary_context')],
          ['AI invalidation anchor not selected yet; original stop/target/expiry cannot be modified.'])}
    # Ancillary measurements need their own availability timestamp. Do not
    # attach an un-timestamped convenience summary as if observed at cutoff.
    for name in ('daily_atr','atr_review_context'):
        value = p.get(name)
        at = value.get('available_at') if isinstance(value,dict) else None
        if type(at) not in (int,float) or not 0 < at <= cutoff:
            p[name] = None
            continue
        key = 'MARKET:' + name
        ev[key] = dict(kind=name, symbol=value.get('symbol','MNQ'), available_at=int(at),
            source=dict(packet_id=packet['source_packet_id'],path=name), value=value)
        topics['atr_expansion'] = field([key] + [r['evidence_id'] for r in topics['atr_expansion']['references']],
            ['ATR is not ICT confirmation; check completeness, definition and session before inference.'])
    local = datetime.fromtimestamp(cutoff,ZoneInfo('America/New_York'))
    session_id='DIRECTIONAL:'+setup['order_id']+':session'
    minute=local.hour*60+local.minute
    ev[session_id]=dict(kind='directional_session',symbol='MNQ',available_at=cutoff,
        source=dict(path='America/New_York calendar; existing AB session windows'),
        value=dict(time_ny=local.isoformat(),session='NYAM' if 570<=minute<660 else 'NYPM' if 810<=minute<960 else 'OUTSIDE_AB_SESSIONS'))
    topics['ny_session']=field(windows+[session_id])
    p.update(setup=setup,setup_fingerprint=fingerprint,
             ict_context=dict(version=VERSION,as_of=cutoff,topics=topics,
               sources=dict(market_packet=dict(packet_id=packet['source_packet_id'],path='evidence[evidence_id]'),
                            **{'evidence.source':'evidence[evidence_id].source'}),
               rule='UNKNOWN is not confirmation. Resolve each reference timestamp and source using its topic timestamp/source paths and evidence_id. Metadata is indexed once, never omitted. No future bars.'))
    return p
