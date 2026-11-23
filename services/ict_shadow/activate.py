"""FUTURE APPROVED DEPLOYMENT ONLY. Create a fresh passive activation receipt.
No deployment, broker/provider access, credential storage or changes to existing data.
"""
import argparse,hashlib,json,os,time
from pathlib import Path
from .worker import verify_release,check_environment
from services.tanja.ict_agent.common import digest,encode
from services.tanja.ict_agent.shadow_transport import PrivateReader

def exclusive(p,v):
    with p.open('x') as f:f.write(encode(v));f.flush();os.fsync(f.fileno())

def activate(config,approval,*,reader=None,now=None):
    cfg=json.loads(Path(config).read_text());root=Path(config).resolve().parent
    authorization=json.loads(Path(approval).read_text());now=time.time() if now is None else now
    release=Path(cfg['release_manifest']);verify_release(release)
    if authorization.get('approved') is not True or authorization.get('scope')!='AGENT50K_PASSIVE_SHADOW_NO_ORDERS_NO_LLM' or not authorization['valid_from']<=now<authorization['expires_at']:raise ValueError('EXPLICIT_CURRENT_DEPLOYMENT_APPROVAL_REQUIRED')
    if authorization['configuration_sha256']!=digest(cfg) or authorization['assets_manifest_sha256']!=hashlib.sha256(release.read_bytes()).hexdigest():raise ValueError('APPROVAL_ARTIFACT_MISMATCH')
    code=Path('/app/code-manifest.json')
    if not code.is_file() or authorization['code_manifest_sha256']!=hashlib.sha256(code.read_bytes()).hexdigest():raise ValueError('APPROVAL_CODE_MISMATCH')
    verify_release(code)
    calpath=root/'calendar/calendar.json';cal=json.loads(calpath.read_text());intervals=cal['exchange_open_intervals_utc'];end=min(authorization['expires_at'],max(hi for lo,hi in intervals))
    if now>=end:raise ValueError('PINNED_CALENDAR_EXPIRED')
    # Guard before any read; repeated activation cannot reuse an approval.
    attempt=root/'activation-attempt.json';exclusive(attempt,{'at':now,'approval_sha256':hashlib.sha256(Path(approval).read_bytes()).hexdigest()})
    read=reader or PrivateReader(cfg['feed_origin'],os.environ['ICT_SHADOW_FEED_TOKEN'])
    raw,_=read.get('/internal/ict-shadow/candles?after='+str(int(now//60)*60-300));receipt=json.loads(raw)
    expected={'NQ':'CME_MINI:NQ1!','MNQ':'CME_MINI:MNQ1!','ES':'CME_MINI:ES1!'}
    if receipt.get('read_only') is not True or receipt.get('bindings')!=expected or abs(receipt['captured_at']-now)>30:raise ValueError('CURRENT_BINDINGS_NOT_VERIFIED')
    exclusive(root/'binding-receipt.json',receipt)
    proof={'verified':True,'valid_from':now,'valid_until':end,'evidence_file':str(root/'binding-receipt.json'),'sha256':hashlib.sha256((root/'binding-receipt.json').read_bytes()).hexdigest(),'tickers':expected,'scope':'Continuous symbol only; rechecked on every poll; not a dated execution contract'}
    report={'classification':'FEED_UNVERIFIED','passive_unverified_authorized':True,'authorization_scope':'Separately approved server passive observer; no paid LLM or orders','symbols':list(expected),'entitlement':{'verified':False},'instrument_binding':proof,'exchange_calendar':{'verified':True,'valid_from':now,'valid_until':end,'evidence_file':str(calpath),'sha256':hashlib.sha256(calpath.read_bytes()).hexdigest(),'open_intervals_utc':intervals}}
    exclusive(Path(cfg['feed_report']),report)
    exclusive(Path(cfg['activation']),dict(passive_only=True,orders_enabled=False,paid_calls_enabled=False,configuration_sha256=digest(cfg),assets_manifest_sha256=authorization['assets_manifest_sha256'],code_manifest_sha256=authorization['code_manifest_sha256'],valid_from=now,expires_at=end,approval_sha256=hashlib.sha256(Path(approval).read_bytes()).hexdigest()))
    return {'activated':True,'feed':'FEED_UNVERIFIED','llm':'PENDING_LLM','expires_at':end,'orders':False}

if __name__=='__main__':
    check_environment(os.environ)
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--approval',required=True);a=p.parse_args()
    print(json.dumps(activate(a.config,a.approval)))
