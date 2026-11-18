"""Entry attainability never overrides geometry. Mocked/offline only, no model calls."""
import pytest
import ict_shadow_research as research
from test_ict_ifvg import setup as ifvg_sample
from test_ict_shadow_research import sample


@pytest.mark.parametrize('family', ['ICT2022', 'IFVG'])
@pytest.mark.parametrize('short', [False, True])
def test_nearest_retest_edge_must_still_have_two_r(family, short):
    packet, decision = ifvg_sample(short) if family == 'IFVG' else sample(short)
    child = decision['ifvg_review'] if family == 'IFVG' else decision
    plan = child['plan']
    first, _, third = [packet['evidence'][k] for k in plan['entry_fvg_bar_ids']]
    # Original bullish gap becomes bearish IFVG; reverse for long.
    bullish_formation = (not short) if family == 'ICT2022' else short
    low, high = (first['high'], third['low']) if bullish_formation else (third['high'], first['low'])
    # SHORT returning from below meets LOW; LONG from above meets HIGH.
    near_entry, near_fraction = (low, 0) if short else (high, 1)
    far_entry, far_fraction = (high, 1) if short else (low, 0)
    stop = plan['stop_price']
    # Set an evidenced boundary that admits only the farther/better-price edge.
    far_risk = abs(far_entry-stop)
    boundary = far_entry + (-1 if short else 1) * 2 * far_risk
    packet['evidence']['target']['price'] = boundary
    plan['target_price'] = plan['target_anchor']['price'] = boundary
    plan.update(entry_fraction=near_fraction, entry_price=near_entry)
    plan['entry_reason'] = 'Synthetic AI preference: nearest edge is more attainable.'
    at = packet['as_of']+10
    if family == 'IFVG':
        v=research.validate(packet, decision, at)['ifvg_validation']
        assert v['state']=='BLOCKED' and v['reason']=='INSUFFICIENT_GROSS_RR'
    else:
        with pytest.raises(AssertionError, match='INSUFFICIENT_GROSS_RR'):
            research.validate(packet, decision, at)
    plan.update(entry_fraction=far_fraction, entry_price=far_entry)
    v=research.validate(packet, decision, at)
    if family == 'IFVG':v=v['ifvg_validation']
    assert v['state']=='VALIDATED_SHADOW_PROPOSAL'
    assert v['execution_authorized'] is False
    assert plan['stop_price']==stop


@pytest.mark.parametrize('short', [False, True])
def test_likely_fill_reason_cannot_revive_expired_ifvg(short):
    packet, decision=ifvg_sample(short)
    plan=decision['ifvg_review']['plan']
    plan['entry_reason']='Synthetic: a first-edge return appears attainable.'
    plan['valid_until']=packet['as_of']+5
    v=research.validate(packet, decision, packet['as_of']+10)['ifvg_validation']
    assert v['state']=='BLOCKED'
