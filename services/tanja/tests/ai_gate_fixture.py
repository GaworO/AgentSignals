"""Entirely synthetic, schema-valid thesis for gate wiring regression tests."""
import json
from context_v3 import unknown_decision,validate_decision
from context_layer import canonical_hash


def review(source,direction='long'):
    long=direction=='long';base=110 if long else 890
    ev={
      'bar':dict(kind='bar',symbol='MNQ',timeframe=1,time=source-60,available_at=source,open=base,close=base,high=base+2,low=base-2),
      'window':dict(kind='window',symbol='MNQ',timeframe=1,start=source-60,end=source,available_at=source,complete=True,high=base+2,low=base-2),
      'trigger':dict(kind='IFVG',symbol='MNQ',timeframe=1,available_at=source,direction=direction,lower=base-1,upper=base+1),
      'anchor':dict(kind='level',symbol='MNQ',available_at=source,price=90 if long else 920),
      'target':dict(kind='level',symbol='MNQ',available_at=source,price=130 if long else 850)}
    p=dict(input_mode='market_only',labels_included=False,as_of=source,evidence=ev)
    p['packet_id']=canonical_hash(p)
    d=unknown_decision(p)
    c=d['context'];c.update(bias=direction,decision='candidate',setup='inversion',requires_smt=False,selected_trigger_id='trigger',missing_context=[],range_selection=dict(window_id='window',origin_bar_id='bar',side='low' if long else 'high'))
    for v in c['claims'].values():v.update(value=True,evidence_ids=['bar'],reason='Synthetic positive fixture')
    d.update(variant_id='V01',analysis_symbol='MNQ',execution_symbol='MNQ',timeframe_minutes=1,poi_evidence_ids=['window'],require_swing_break=False,entry_mode='immediate_after_confirmation',invalidation=dict(evidence_id='anchor',field='price'),initial_stop=dict(anchor=dict(evidence_id='anchor',field='price'),buffer_ticks=1,price=89.75 if long else 920.25,reason='Synthetic anchor'),initial_quantity=1,missing_fields=[],rationale='Synthetic direction only; not a real AI response')
    d['management'].update(policy='structural_trail',partial_policy='No partial on one contract',targets=[dict(kind='level',anchor=dict(evidence_id='target',field='price'),extreme=None,price=130 if long else 850,quantity=1,reason='Synthetic objective')])
    assert validate_decision(p,d)['context_review_state']=='CONDITIONS_MET'
    return dict(id=direction+'-fixture',packet_id=p['packet_id'],cutoff=source,started=source+2,finished=source+25,status='validated',snapshot=json.dumps(dict(packet=p,frozen_at=source+1,processed_at=source+1)),decision=json.dumps(d))
