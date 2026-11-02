"""Human-run IFVG scope control. Never invoked by scanner; no broker writes."""
import hashlib,json,os,sys,time
from pathlib import Path
import ict_execution as ict
import execution_validation as validation

PHRASE='ENABLE IFVG ON MY MFFU ACCOUNT'

def require(ok,message):
    if not ok:raise ValueError(message)

def check():
    import directional_account_reconciliation as rec
    import builder_broker_sync as broker
    import ab_v3_live as live
    p=validation.policy('ICT_AI')
    require(ict.status()['blockers']==[], 'ICT route blocked: '+str(ict.status()['blockers']))
    require(live.native_bracket_readiness(p) is None,'Native bracket readiness not satisfied')
    state=rec.state();now=int(time.time()*1000)
    require(state.get('verified') is True,'Account reconciliation: '+str(state.get('missing')))
    control=state['controls'];observed=state['observed']
    require(0<=now-control['observed_at_ms']<=30000,'Stale account reconciliation')
    rules=json.loads(Path(os.environ['MFFU_RISK_EVIDENCE_FILE']).read_text())
    require(now<rules['expires_at_ms'],'Account evidence expired')
    require(state.get('rules_sha256')==hashlib.sha256(broker.encode(rules).encode()).hexdigest(),'Account evidence changed since reconciliation')
    require(observed.get('positions')==0 and observed.get('working_orders')==0,'Account not flat / working orders exist')
    n=int(os.getenv('DAY_LOSS_N','1'));streak=int(os.getenv('LOSS_STREAK_N','3'));hours=float(os.getenv('LOSS_STREAK_COOLDOWN_H','24'))
    require(not n or control['losses_today']<n,'Daily loss stop active')
    if streak and control['consecutive_losses']>=streak:
        last=control.get('last_loss_closed_ms');require(isinstance(last,(int,float)) and now>=last+hours*3600000,'Loss-streak cooldown active or unknown')
    require(os.getenv('V3_EXIT_REAL_EXECUTION','false')=='false' and os.getenv('V3_EXIT_MODE','shadow')=='shadow' and os.getenv('AB_MANAGER_MODE','shadow')=='shadow' and os.getenv('V3_RUNNER_ENABLED','false')=='false','Advanced writes must remain off')
    return dict(commit=os.getenv('RAILWAY_GIT_COMMIT_SHA'),equity=control['equity'],losses_today=control['losses_today'],risk_expires_at_ms=rules['expires_at_ms'],ifvg=ict.status()['ifvg'],broker_round_trip='IFVG_NOT_INDEPENDENTLY_VERIFIED')

def write_scope(enabled):
    require(sys.stdin.isatty(),'Use your interactive Railway SSH terminal')
    path=Path(os.environ['ICT_EXECUTION_POLICY_FILE']);before=path.read_bytes();p=json.loads(before)
    if enabled:
        require((p.get('ifvg_execution') or {}).get('approved') is not True,'IFVG already approved; use check')
        print(json.dumps(check(),indent=2))
        print('This enables future IFVG automatic MFFU Evaluation orders, sharing account exposure with ICT2022 / Directional / Q1. Maximum 0.5% equity risk, structural SL, fixed 2R gross; all tighter limits apply.')
        print('Accept candle-only spread/intrabar uncertainty, late fills/cancel latency, stop slippage and disconnect risk. IFVG broker round trip remains unverified; deterministic tests and historical native brackets are the available evidence. Advanced writes stay disabled.')
        require(input('Type '+PHRASE+': ').strip()==PHRASE,'Cancelled; no changes')
        check()
        scope=dict(approved=True,contract_version=ict.IFVG_VERSION,armed_after_ms=int(time.time()*1000),operator_acceptance_reference='HUMAN_INTERACTIVE_IFVG_'+str(int(time.time()*1000)),broker_round_trip_verified=False)
    else:
        require(input('Type STOP IFVG ENTRIES: ').strip()=='STOP IFVG ENTRIES','Cancelled; no changes')
        scope=dict(p.get('ifvg_execution') or {},approved=False)
    require(path.read_bytes()==before,'Policy changed concurrently; stop')
    backup=path.with_name(path.name+'.before-ifvg-'+str(time.time_ns()));backup.write_bytes(before)
    p['ifvg_execution']=scope;temp=path.with_name(path.name+'.ifvg-'+str(os.getpid()))
    try:
        with temp.open('x') as f:json.dump(p,f,indent=2);f.flush();os.fsync(f.fileno())
        require(path.read_bytes()==before,'Policy changed concurrently; stop')
        os.replace(temp,path)
    finally:
        if temp.exists():temp.unlink()
    print('IFVG OPERATOR APPLIED' if enabled else 'IFVG NEW ENTRIES DISABLED; broker protection/reconciliation retained')
    print('No order forced. Backup: '+str(backup))

if __name__=='__main__':
    try:
        require(len(sys.argv)==2 and sys.argv[1] in ('check','activate','stop'),'Usage: python /app/ict_ifvg_operator.py check|activate|stop')
        if sys.argv[1]=='check':print(json.dumps(check(),indent=2))
        else:write_scope(sys.argv[1]=='activate')
    except Exception as exc:print('STOP:',exc);sys.exit(1)
