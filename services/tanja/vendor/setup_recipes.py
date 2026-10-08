"""Evidence-labelled setup templates. Choosing the inputs is still external.

No geometric threshold for 'range extreme' is invented here. The requirement
refers to the origin of the reversal, not a claim that the entry candle must
still touch the range edge after displacement.
"""
from decision_engine import Plan


def inversion_plan(plan_id, direction, known_at, timeframe, requires_smt=None,
                   require_selected_swing_break=False):
    if timeframe not in (1,2,3,5):
        raise ValueError('Observed intraday inversion timeframes only')
    required=['higher_timeframe_context_valid','setup_origin_at_range_extreme',
              'liquidity_event_complete','gap_inverted','confirmation_accepted']
    if require_selected_swing_break:
        required.append('selected_swing_broken')
    return Plan(plan_id,direction,known_at,tuple(required),requires_smt,
                f'M{timeframe} inversion research template; May22/May26 and July17/July30 sources; externally selected range and context')


def reentry_reason_supported(reason):
    """Evidence categories, not automated detection or an exhaustive taxonomy.

    Another identical signal or merely revisiting the old price is insufficient.
    Availability and truth still need explicit sourced facts in DecisionEngine.
    """
    return reason in ('new_timeframe_confirmation','new_catalyst',
                      'additional_liquidity_then_reconfirmation',
                      'new_structural_evidence_after_tactical_exit')
