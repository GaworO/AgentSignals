"""Frozen Win/Loss/Boundary inference. This module never submits orders."""
from functools import lru_cache
from pathlib import Path
from hashlib import sha256
import pickle
import numpy as np
import pandas as pd
from v3_boundary_features import build
HASHES = {'WIN_TEMPORAL_CHECK.pkl': '8f2e496b75f657b1cf769e7dbf9b818869ff35f87cee1a081b7706afbd0eda81', 'CHRONO_94_24.pkl': '402b7db1ddfd8daded1168e69c3aeba30ea2b3bf295c0a78297e483df9b11ba6', 'BOUNDARY_MODEL.pkl': 'a8c4bc6a98dae70be8002bb4367ad2bc4bbbbc90a41f2d253607b44acaee9712'}
FEATURES=['win_confidence','loss_confidence','score_difference',
          *[f'{s}_delta_{m}m' for s in ['win','loss'] for m in [1,3,5]],
          *[f'lag_{m}m_available' for m in [1,3,5]],
          'current_r','mfe_r','gb_current_drawdown_r','ne','r_delta_3m',
          'entry_fvg_broken','opp_fvg_held','seq_allowed',
          'rule_fvg_failure','rule_obfh','rule_retest','rule_sequence']
def neighbors(bundle,q):
    x=np.clip(bundle['scaler'].transform(q[bundle['features']]),-5,5);dist,ix=bundle['knn'].kneighbors(x,n_neighbors=min(40,len(bundle['references'])));ref=bundle['references'];rows=[]
    for oid,dd,jj in zip(q.order_id,dist,ix):
        seen={oid};chosen=[]
        for distance,j in zip(dd,jj):
            r=ref.iloc[j]
            if r.order_id in seen:continue
            seen.add(r.order_id);chosen.append(dict(order_id=r.order_id,known_ms=int(r.known_ms),distance=float(distance/np.sqrt(len(bundle['features'])))))
            if len(chosen)==3:break
        if len(chosen)!=3:raise ValueError('Need3 distinct reference winner trades')
        rows.append(chosen)
    means=np.array([np.mean([r['distance'] for r in rows3]) for rows3 in rows]);return means,rows

def score_win(bundle,q):
    distance,refs=neighbors(bundle,q);confidence=np.exp2(-distance/bundle['radius']);return confidence,refs

def score_loss(bundle,q):
    x=q[bundle['features']].to_numpy(np.float32);votes=np.zeros(len(q));qc=bundle['q_close'];qh=bundle['q_hold'];assert len(qc.estimators_)==len(qh.estimators_)
    for a,b in zip(qc.estimators_,qh.estimators_):votes+=a.predict(x)>b.predict(x)
    votes/=len(qc.estimators_);manager=q.manager_rules.fillna('').str.len().to_numpy()>0
    confidence=np.maximum(votes,manager.astype(float));return confidence,votes,qc.predict(x)-qh.predict(x)

def prepare(d):
    d=d.sort_values(['order_id','known_ms']).copy()
    assert not d.duplicated(['order_id','known_ms']).any()
    d['score_difference']=d.win_confidence-d.loss_confidence
    index=pd.MultiIndex.from_frame(d[['order_id','known_ms']])
    for m in [1,3,5]:
        prev=pd.MultiIndex.from_arrays([d.order_id,d.known_ms-m*60000])
        for src,name in [('win_confidence','win'),('loss_confidence','loss'),('current_r','r')]:
            old=pd.Series(d[src].to_numpy(),index=index).reindex(prev).to_numpy()
            if name!='r' or m==3:d[f'{name}_delta_{m}m']=np.where(np.isfinite(old),d[src].to_numpy()-old,0.)
            if name=='win':d[f'lag_{m}m_available']=np.isfinite(old).astype(float)
    families={'rule_fvg_failure':{'F2','B11'},'rule_obfh':{'OBFH_EXPANSION','OBFH_3_CLOSES_10M'},'rule_retest':{'FR_A','FR_B'},'rule_sequence':{'S1','S2','S3','S4'}}
    for key,rules in families.items():d[key]=d.manager_rules.fillna('').map(lambda x:float(bool(set(x.split(';'))&rules)))
    d['eligible']=(d.win_confidence>=.5)&(d.loss_confidence>=.75)
    assert np.isfinite(d[FEATURES].to_numpy(float)).all()
    return d

def score(bundle,d):
    d=d.copy();d['boundary_score']=np.nan;d['bootstrap_lo']=np.nan;d['bootstrap_hi']=np.nan;d['boundary_output']=None
    use=d.eligible
    if use.any():
        x=d.loc[use,FEATURES];p=bundle['model'].predict_proba(x)[:,1]
        b=np.stack([m.predict_proba(x)[:,1] for m in bundle['bootstrap']]);lo,hi=np.quantile(b,[.1,.9],axis=0)
        d.loc[use,'boundary_score']=p;d.loc[use,'bootstrap_lo']=lo;d.loc[use,'bootstrap_hi']=hi
        d.loc[use,'boundary_output']=np.select([(p>=.7)&(lo>.5),(p<=.3)&(hi<.5)],['RECOVERY','FAILURE'],default='AMBIGUOUS')
    return d


@lru_cache(maxsize=1)
def models():
    bundles=[]
    for name,expected in HASHES.items():
        raw=(Path(__file__).resolve().parent/'v3_boundary_models'/name).read_bytes()
        if sha256(raw).hexdigest()!=expected: raise ValueError('boundary_model_hash_mismatch:'+name)
        bundles.append(pickle.loads(raw))
    return tuple(bundles)


def classify(states):
    if states.empty: return []
    win,loss,boundary=models()
    required=set(win['features'])|set(loss['features'])
    if not required <= set(states): raise ValueError('boundary_features_missing')
    if not np.isfinite(states[list(required)].to_numpy(float)).all(): raise ValueError('boundary_features_nonfinite')
    d=states.copy()
    d['win_confidence'],_=score_win(win,d)
    d['loss_confidence'],d['loss_votes'],d['q_advantage']=score_loss(loss,d)
    d=score(boundary,prepare(d))
    records=[]
    for _,r in d.iterrows():
        records.append(dict(known_ms=int(r.known_ms),classification=r.boundary_output if r.eligible else 'NO_BOUNDARY_YET',
                            win_score=float(r.win_confidence),loss_score=float(r.loss_confidence),
                            boundary_score=float(r.boundary_score) if r.eligible else None,
                            bootstrap_lo=float(r.bootstrap_lo) if r.eligible else None,
                            bootstrap_hi=float(r.bootstrap_hi) if r.eligible else None))
    return records


def evaluate(order,position,minutes,known_ms):
    return classify(build(order,position,minutes,known_ms))


def manager(boundary,m3):
    """Only validated M3 can request close. Recovery veto never affects Guard/SL."""
    cls=boundary['classification']
    signals=m3['signals']
    confirmed=signals.get('primary_manager')=='M3' and bool(signals.get('primary_reason'))
    allowed=confirmed and cls!='RECOVERY'
    return dict(boundary=boundary,position_kind='WINNER_LIKE / RECOVERY' if cls=='RECOVERY' else
                'LOSER_LIKELY' if cls=='FAILURE' else 'STANDARD',
                decision='CLOSE' if allowed else 'HOLD',m3_confirmed=confirmed,
                reason=signals['primary_reason'] if allowed else None)
