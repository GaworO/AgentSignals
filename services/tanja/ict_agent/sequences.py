"""Small, explicitly source-bound retrieval sequences; never execution rules.
Only concepts/order are matched. TF/range/direction/threshold truth is unresolved.
"""
TEMPLATES={
 'full-assistant-kNlySn81dmo-003':dict(topic='2022',stages=[['SWEEP'],['STRUCTURE_BREAK']],
    companion_observations=['DISPLACEMENT','FVG'],note='Displacement with FVG is described as ideal; no fabricated required order between those companion observations.'),
 'full-assistant-Cg0-CFJOJvg-004':dict(topic='OTE',stages=[['STRUCTURE_BREAK'],['RETRACEMENT']],
    companion_observations=[],note='Retracement is a source condition; the current detector does not certify it.'),
}

def match(rule_id,events):
    template=TEMPLATES.get(rule_id)
    if template is None:return dict(status='SOURCE_SEQUENCE_NOT_COMPILED',executable=False)
    cursor=0;matched=[];missing=[]
    for alternatives in template['stages']:
        found=next((i for i in range(cursor,len(events)) if events[i] in alternatives),None)
        if found is None:missing.append(alternatives)
        else:matched.append(dict(stage=alternatives,event_index=found));cursor=found+1
    return dict(status='CONCEPT_ORDER_MATCH_ONLY' if not missing else 'INCOMPLETE_CONCEPT_SEQUENCE',
        matched=matched,missing=missing,source_rule_id=rule_id,note=template['note'],
        companion_observations={k:k in events for k in template['companion_observations']},
        unverified=['same-symbol/contract/timeframe linkage','source-specific swing/range choice','numerical thresholds','all conditions/exceptions'],executable=False)
