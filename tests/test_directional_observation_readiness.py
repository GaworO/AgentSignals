"""Read-only report must not misstate the production gross-R or AI time gates."""
import pytest

from directional_review_observation import assess


def report(direction='LONG', target_r=2, available=90, expires=110):
    sign=1 if direction=='LONG' else -1
    setup=dict(order_id='recorded-example',direction=direction,contract='MNQZ2026',
               activation_ms=90000,expiry_ms=120000,entry_price=100,
               stop_price=100-sign*10,target_price=100+sign*10*target_r)
    return assess(setup,dict(state='ALLOW',available_at=available,expires_at=expires),
        dict(raw=dict(bid=99.75,ask=100),quote=dict(execution_eligible=False)),
        now=100,config={},parameters=dict(min_rr=2,max_quote_age_ms=1000,
            entry_zone_points=1,max_spread_ticks=2,
            costs=dict(round_trip_usd=1.90,stop_slippage_ticks=1)))


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_two_gross_r_is_not_rejected_for_costs(direction):
    r=report(direction)
    checks={c['gate']:c for c in r['checks']}
    assert r['gross_r']==2 and r['estimated_net_r']<2
    assert checks['gross_rr']['status']=='PASS'
    assert checks['ai_validity']['status']=='PASS'
    assert 'net_rr' not in checks
    assert r['decision']=='BLOCK' and not r['execution_authorized']
    assert r['confirmed_executable_price'] is None


def test_insufficient_gross_rr_is_still_blocked():
    assert 'gross_reward_risk_below_minimum' in report(target_r=1.9)['blocking_reasons']


@pytest.mark.parametrize('available,expires',[(90,100),(101,120),(None,120),(90,None),(float('nan'),120)])
def test_historical_allow_does_not_imply_current_approval(available,expires):
    assert 'ai_review_expired_or_time_unknown' in report(available=available,expires=expires)['blocking_reasons']
