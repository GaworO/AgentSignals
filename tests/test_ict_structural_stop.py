"""V9 stop explanation/provenance, legacy compatibility and unchanged geometry.

All decisions are synthetic; no provider or broker calls.
"""
import copy
import pytest
import ict_shadow_research as research
from test_ict_shadow_research import sample, T
from test_ict_ifvg import setup as ifvg_sample


def current(family='ICT2022', short=False):
    p,d=ifvg_sample(short) if family=='IFVG' else sample(short)
    p['experiment']=research.VERSION
    decisions=[d,d['ifvg_review']] if family=='IFVG' else [d]
    for item in decisions:
        plan=item.get('plan')
        item['assessments']['structural_stop']=dict(
            reasoning='Synthetic: the selected leg extreme invalidates this thesis.' if plan else
                      'Synthetic WAIT: no confirmed thesis; no stop can be selected.',
            evidence_ids=[plan['stop_anchor']['evidence_id']] if plan else [],missing=[])
    return p,d


def selected(d,family):return d if family=='ICT2022' else d['ifvg_review']


def outcome(p,d,family):
    if family=='IFVG':
        v=research.validate(p,d,p['as_of']+10)['ifvg_validation']
        return v.get('reason',v['state'])
    try:return research.validate(p,d,p['as_of']+10)['state']
    except AssertionError as exc:return str(exc)


def test_both_family_schemas_require_assessment_even_without_plan():
    schema=research.schema('bound',include_ifvg=True)
    for s in [schema,schema['properties']['ifvg_review']]:
        assert 'structural_stop' in s['properties']['assessments']['required']
        a=s['properties']['assessments']['properties']['structural_stop']
        assert set(a['required'])=={'reasoning','evidence_ids','missing'}
        assert {'type':'null'} in s['properties']['plan']['anyOf']


@pytest.mark.parametrize('family',['ICT2022','IFVG'])
@pytest.mark.parametrize('short',[False,True])
def test_supported_first_candle_or_inversion_stop_keeps_geometry(family,short):
    p,d=current(family,short)
    old=copy.deepcopy(p);old['experiment']='ict-2022-ifvg-cloud-v8-model-led-entry'
    assert research.validate(p,d,p['as_of']+10)==research.validate(old,d,p['as_of']+10)
    assert outcome(p,d,family)=='VALIDATED_SHADOW_PROPOSAL'


@pytest.mark.parametrize('family',['ICT2022','IFVG'])
@pytest.mark.parametrize('verdict',['WAIT','NO_TRADE'])
def test_no_thesis_wait_needs_explanation_not_fabricated_anchors(family,verdict):
    p,d=current(family);a=selected(d,family)
    a.update(decision=verdict,plan=None)
    a['assessments']['structural_stop']=dict(reasoning='No confirmed displacement; no structural stop can yet be selected.',evidence_ids=[],missing=['Displacement'])
    assert outcome(p,d,family)==verdict
    a['assessments']['structural_stop']['reasoning']='  '
    assert outcome(p,d,family)=='STOP_SELECTION_ASSESSMENT_MISSING'


@pytest.mark.parametrize('family',['ICT2022','IFVG'])
@pytest.mark.parametrize('mutation,expected',[
    ('missing','STOP_SELECTION_ASSESSMENT_MISSING'),
    ('invented','INVENTED_EVIDENCE'),
    ('future','FUTURE_STOP_SELECTION_EVIDENCE'),
    ('uncited','STOP_SELECTION_ANCHOR_NOT_CITED'),
    ('moved_stop','GEOMETRY_NOT_ANCHORED'),
    ('rr','INSUFFICIENT_GROSS_RR')])
def test_a_comparison_cannot_bypass_price_evidence_or_two_r(family,mutation,expected):
    p,d=current(family);a=selected(d,family);s=a['assessments']['structural_stop'];plan=a['plan']
    if mutation=='missing':del a['assessments']['structural_stop']
    if mutation=='invented':s['evidence_ids'].append('absent')
    if mutation=='future':
        p['evidence']['future']=dict(p['evidence'][plan['stop_anchor']['evidence_id']],available_at=p['as_of']+60)
        s['evidence_ids'].append('future')
    if mutation=='uncited':s['evidence_ids']=[]
    if mutation=='moved_stop':plan['stop_price']+=.25
    if mutation=='rr':
        target=plan['entry_price']+(1 if plan['direction']=='LONG' else -1)*abs(plan['entry_price']-plan['stop_price'])
        p['evidence']['target']['price']=plan['target_price']=plan['target_anchor']['price']=target
    assert outcome(p,d,family)==expected


@pytest.mark.parametrize('old_version',[None,'ict-2022-ifvg-cloud-v8-model-led-entry'])
def test_existing_approvals_do_not_require_new_explanation(old_version):
    p,d=sample();p['experiment']=old_version
    assert research.validate(p,d,T+10)['state']=='VALIDATED_SHADOW_PROPOSAL'
    d.update(decision='WAIT',plan=None)
    assert research.validate(p,d,T+10)['state']=='WAIT'


def test_no_automatic_preference_for_tighter_anchor():
    p,d=current()
    # Two authentic synthetic lows, both unbreached and supported by geometry.
    # The validator cannot choose semantic invalidation or silently minimize SL.
    p['evidence']['wide']=dict(p['evidence']['0'],time=T-240,available_at=T-180,low=98)
    p['evidence']['target']['price']=120;plan=d['plan']
    plan.update(target_price=120,target_anchor=dict(evidence_id='target',field='price',price=120))
    d['assessments']['structural_stop']['evidence_ids']=['0','wide']
    for eid,price in [('0',100),('wide',98)]:
        plan.update(stop_anchor=dict(evidence_id=eid,field='low',price=price),stop_price=price-.25)
        v=research.validate(p,d,T+10)
        assert v['state']=='VALIDATED_SHADOW_PROPOSAL' and v['stop']==price-.25
