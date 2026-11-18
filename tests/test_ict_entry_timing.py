"""Resting-entry lifecycle and geometry remain deterministic; no API calls."""
import pytest
import ict_shadow_research as research
from test_ict_shadow_research import sample, T
from test_ict_ifvg import setup as ifvg_sample


@pytest.mark.parametrize('short', [False, True])
@pytest.mark.parametrize('family', ['ICT2022', 'IFVG'])
def test_confirmed_first_retest_plan_does_not_need_future_retest_bar(short, family):
    packet, decision = ifvg_sample(short) if family == 'IFVG' else sample(short)
    cutoff = packet['as_of']
    bars = [e for e in packet['evidence'].values() if e['kind'] == 'bar']
    assert max(b['available_at'] for b in bars) == cutoff
    validation = research.validate(packet, decision, cutoff + 10)
    if family == 'IFVG':
        validation = validation['ifvg_validation']
    assert validation['state'] == 'VALIDATED_SHADOW_PROPOSAL'
    assert validation['execution_authorized'] is False


@pytest.mark.parametrize('short', [False, True])
def test_completed_retest_alone_does_not_invalidate_intact_ifvg(short):
    packet, decision = ifvg_sample(short)
    bar = dict(kind='bar', symbol='MNQ', timeframe=1, time=T+60,
               available_at=T+120, open=102, high=105.5, low=101, close=103)
    if not short:
        bar.update(open=220-bar['open'], high=220-bar['low'],
                   low=220-bar['high'], close=220-bar['close'])
    packet['evidence']['retest'] = bar
    packet['as_of'] = T+120
    entry = decision['ifvg_review']['plan']['entry_price']
    assert bar['low'] <= entry <= bar['high']
    v = research.validate(packet, decision, T+130)['ifvg_validation']
    assert v['state'] == 'VALIDATED_SHADOW_PROPOSAL'
    assert v['execution_authorized'] is False  # A candle touch is not a broker fill.
    # A true reinversion still invalidates, despite the retest guidance.
    packet['evidence']['retest']['close'] = 107 if short else 113
    v = research.validate(packet, decision, T+130)['ifvg_validation']
    assert v['state'] == 'BLOCKED' and v['reason'] == 'IFVG_REINVALIDATED'


@pytest.mark.parametrize('short', [False, True])
@pytest.mark.parametrize('family', ['ICT2022', 'IFVG'])
def test_selected_material_boundary_still_requires_two_r(short, family):
    packet, decision = ifvg_sample(short) if family == 'IFVG' else sample(short)
    plan = (decision['ifvg_review'] if family == 'IFVG' else decision)['plan']
    risk = abs(plan['entry_price'] - plan['stop_price'])
    boundary = plan['entry_price'] + (-1 if short else 1)*risk
    packet['evidence']['target']['price'] = boundary
    plan['target_anchor']['price'] = plan['target_price'] = boundary
    if family == 'IFVG':
        v = research.validate(packet, decision, packet['as_of']+10)['ifvg_validation']
        assert v['state'] == 'BLOCKED' and v['reason'] == 'INSUFFICIENT_GROSS_RR'
    else:
        with pytest.raises(AssertionError, match='INSUFFICIENT_GROSS_RR'):
            research.validate(packet, decision, packet['as_of']+10)
