"""Operator preparation only: writes an UNAPPROVED draft, never arms or trades."""
import copy,json,os
from pathlib import Path
import execution_validation
import ab_v3_live
import ict_execution


def draft():
    base=execution_validation.policy()
    if base.get('execution_model')!='RESTING_LIMIT_CLOSED_M1_V1':raise ValueError('existing_resting_limit_policy_required')
    why=ab_v3_live.native_bracket_readiness(base)
    if why:raise ValueError(why)
    value=copy.deepcopy(base)
    value.update(strategy='ICT_AI',approved=False,target_rule='FIXED_2R_GROSS',fixed_gross_rr=2,risk_percent=.5,
        thesis_rule_version=ict_execution.VERSION,validation_reference='ICT_AUTO_ROUTE_TESTS_V1',
        residual_risk_acceptance_reference=None)
    proof=value['broker_workflow_verification']
    proof['execution_strategies']=sorted(set(proof.get('execution_strategies',[]))|{'ICT_AI'})
    proof['ict_operator_acceptance_reference']=None
    proof['current_ict_broker_run_verified']=False
    value['operator_note']='UNAPPROVED draft. Historical broker facts retained; ICT lifecycle tests are simulated. Review current risk evidence and explicitly accept residual and broker-workflow limitations before activation.'
    return value


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--draft-output',required=True)
    args=parser.parse_args()
    if os.environ.get('ICT_EXECUTION_ENABLED','false')=='true':raise SystemExit('Disable ICT before editing execution policy.')
    value=draft()
    # Do not overwrite a prior operator-reviewed policy or any live permission.
    with Path(args.draft_output).open('x') as f:json.dump(value,f,indent=2)
    print(json.dumps({'written':args.draft_output,'approved':False,'execution_enabled':False}))
