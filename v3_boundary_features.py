"""Frozen causal feature formulas copied for inference; no research imports or fitting.

Provenance: sequence_rl/features, learn118/context_features, bos_sequence/sequence,
sequence_library/engine and failed_retest/engine (2026-10-06).
Sequence/retest events below are expert INPUTS only; they cannot issue exits.
"""
import numpy as np
import pandas as pd
from v3_frozen_exit import bos, displacement, _prior_atr20, gap, evaluate
class EventMemory:
    def __init__(self,b,ms,step=60000,min_created=2,max_zone_age=None):
        self.b=b;self.ms=ms;self.step=step;self.zones=[];self.events=[]
        self.last_bos={1:-100000,-1:-100000};self.last_disp={1:-100000,-1:-100000}
        self.last_break={1:-100000,-1:-100000};self.break_count={1:0,-1:0}
        self.last_exp={1:-100000,-1:-100000};self.run={1:0,-1:0}
        self.min_created=min_created;self.max_zone_age=max_zone_age
    def update(self,j):
        b=self.b;ms=self.ms;c=b[j,3];ev=[]
        for z in (-1,1):
            self.run[z]=self.run[z]+1 if j>0 and ms[j]-ms[j-1]==self.step and z*(c-b[j-1,3])>=.25-1e-9 else 0
            if bos(b,j,z):self.last_bos[z]=j;ev.append(('BOS',z))
            if displacement(b,j,z):self.last_disp[z]=j;ev.append(('DISPLACEMENT',z))
            if self.run[z]==3:self.last_exp[z]=j;ev.append(('THREE_CLOSES',z))
        for q in self.zones:
            if not q['valid']:continue
            z=q['z'];edge=q['lo'] if z==1 else q['hi']
            if z*(c-edge)<-1e-9:
                q['valid']=False;self.last_break[z]=j;self.break_count[z]+=1;ev.append(('FVG_BROKEN',z));continue
            if j==q['created']+1 and ms[j]-ms[j-1]==self.step:
                q['hold']=j;q['hold_close']=c;ev.append(('FVG_H1_HELD',z))
            touched=b[j,2]<=q['hi'] and b[j,1]>=q['lo']
            rejected=z*(c-(q['hi'] if z==1 else q['lo']))>=0
            if j>q['created'] and touched and rejected and not q['retest']:
                q['retest']=j;ev.append(('FVG_RETEST_HELD',z))
            if q['hold']>=0 and j>q['hold']:
                for az in (-1,1):
                    q['runs'][az]=q['runs'][az]+1 if az*(c-b[j-1,3])>=.25-1e-9 else 0
                    q['after_move'][az]=max(q['after_move'][az],az*(c-q['hold_close']))
                    if q['runs'][az]>=3:q['after3'][az]=1
        self.zones=[q for q in self.zones if q['valid'] and (self.max_zone_age is None or j-q['created']<=self.max_zone_age)]
        if j>=self.min_created and ms[j]-ms[j-2]==2*self.step:
            for z in (-1,1):
                bounds=gap(b,j,z)
                if bounds:
                    self.zones.append(dict(z=z,lo=bounds[0],hi=bounds[1],created=j,hold=-1,hold_close=0.,valid=True,retest=0,linked_bos=int(self.last_bos[z]>=j-1),runs={1:0,-1:0},after3={1:0,-1:0},after_move={1:0.,-1:0.}))
                    ev.append(('FVG_CREATED',z))
        self.events.append(ev)
        return ev
    def state(self,j,z,risk):
        c=self.b[j,3];out={}
        for name,az in [('sup',z),('opp',-z)]:
            out[name+'_run']=min(12,self.run[az])
            for key,last in [('bos',self.last_bos),('disp',self.last_disp),('exp3',self.last_exp),('break',self.last_break)]:
                out[name+'_'+key+'_age']=min(120,j-last[az])
            out[name+'_broken_count']=min(20,self.break_count[az])
            zones=[q for q in self.zones if q['valid'] and q['z']==az]
            out[name+'_zones']=min(10,len(zones));q=zones[-1] if zones else None
            vals=dict(present=0,held=0,retested=0,age=120,hold_age=120,within10=0,width_r=0.,edge_r=0.,linked_bos=0,posthold_sup3=0,posthold_opp3=0,posthold_sup_move_r=0.,posthold_opp_move_r=0.,mfe_before_creation_r=0.,mfe_at_hold_r=0.,weak_exp_before_creation=0)
            if q:
                age=j-q['hold'] if q['hold']>=0 else 120
                vals.update(present=1,held=int(q['hold']>=0),retested=int(q['retest']>0),age=min(120,j-q['created']),hold_age=min(120,age),within10=int(q['hold']>=0 and (j-q['hold'])*self.step<=600000),width_r=(q['hi']-q['lo'])/risk,edge_r=z*(c-(q['lo'] if az==1 else q['hi']))/risk,linked_bos=q['linked_bos'],posthold_sup3=q['after3'][z],posthold_opp3=q['after3'][-z],posthold_sup_move_r=q['after_move'][z]/risk,posthold_opp_move_r=q['after_move'][-z]/risk)
                before=q.get('mfe_before_creation_r',0.)
                vals.update(mfe_before_creation_r=before,mfe_at_hold_r=q.get('mfe_at_hold_r',0.),weak_exp_before_creation=int(0<before<.75))
            out.update({name+'_fvg_'+k:v for k,v in vals.items()})
            # Counts retain older still-valid zones when a newer FVG appears.
            out[name+'_held_zones']=sum(q['hold']>=0 for q in zones)
            out[name+'_held_then_opp3']=sum(q['hold']>=0 and q['after3'][-z] for q in zones)
            out[name+'_held_then_sup3']=sum(q['hold']>=0 and q['after3'][z] for q in zones)
        return out

def aggregate(raw,minutes):
    """Discard incomplete buckets; no backfill, future minutes or cross-contract aggregation."""
    step=minutes*60000;ms=raw[:,0].astype(np.int64);bucket=ms//step
    starts=np.r_[0,np.flatnonzero(np.diff(bucket))+1];ends=np.r_[starts[1:],len(raw)]
    rows=[]
    for a,e in zip(starts,ends):
        if e-a!=minutes or ms[a]%step or ms[e-1]!=ms[a]+step-60000:continue
        if not np.all(np.diff(ms[a:e])==60000):continue
        rows.append([ms[a],raw[a,1],raw[a:e,2].max(),raw[a:e,3].min(),raw[e-1,4]])
    return np.asarray(rows,float).reshape(-1,5)

class Context:
    def __init__(self,raw,minutes):
        self.raw=aggregate(raw,minutes);self.step=minutes*60000
        self.ms=self.raw[:,0].astype(np.int64);self.b=self.raw[:,1:5]
        self.mem=EventMemory(self.b,self.ms,self.step,max_zone_age=120);self.cache=[]
        for j in range(len(self.ms)):
            self.mem.update(j)
            atr=_prior_atr20(self.b,j) or .25
            row={}
            for z in (-1,1):
                a=self.mem.state(j,z,1.)
                keys=['sup_bos_age','opp_bos_age','sup_disp_age','opp_disp_age','sup_run','opp_run','sup_fvg_present','opp_fvg_present','sup_fvg_held','opp_fvg_held','sup_fvg_retested','opp_fvg_retested','sup_fvg_edge_r','opp_fvg_edge_r','sup_fvg_posthold_sup3','opp_fvg_posthold_opp3','sup_break_age','opp_break_age']
                vals={k:a[k] for k in keys}
                vals.update(available=int(j>=21),return3_r=z*(self.b[j,3]-self.b[max(0,j-3),3]),body_r=z*(self.b[j,3]-self.b[j,0]),atr_r=atr,range_atr=(self.b[j,1]-self.b[j,2])/max(atr,.25))
                row[z]=vals
            self.cache.append(row)
    def at(self,known,z,risk):
        k=int(np.searchsorted(self.ms+self.step,known,side='right'))-1
        if k<0:
            keys=list(self.cache[0][z]) if self.cache else []
            return {**dict.fromkeys(keys,0.),'age_minutes':120.},None
        assert int(self.ms[k])+self.step<=known
        vals=dict(self.cache[k][z]);vals['age_minutes']=min(120,(known-int(self.ms[k])-self.step)/60000)
        for key in vals:
            if key.endswith('_r'):vals[key]/=risk
        return vals,int(self.ms[k])+self.step

def episode(raw,o,end,contexts):
    """Prefix-only state construction; end limits observation, never becomes a feature."""
    ms=raw[:,0].astype(np.int64);b=raw[:,1:5];fi=int(np.searchsorted(ms,int(o['fill_ms'])));z=1 if o['direction']=='LONG' else -1
    entry=float(o['entry']);risk=float(o['risk']);zone=None
    for j in range(max(2,int(o['s'])),fi):
        lo,hi=(b[j-2,1],b[j,2]) if z==1 else (b[j,1],b[j-2,2])
        if hi-lo<.25-1e-9:continue
        edge=lo if z==1 else hi
        if z*(entry-edge)<-1e-9 or np.any(z*(b[j+1:fi,3]-edge)<-1e-9):continue
        zone=(lo,hi)
    mem=EventMemory(b,ms,min_created=fi+2);best=b[fi,3];mfe=mae=0.;ne=0;warning=-1;out=[]
    for j in range(fi+1,end):
        if ms[j]-ms[j-1]!=60000:break
        c=b[j,3];known=int(ms[j])+60000;prior_mfe=mfe;ev=mem.update(j)
        mfe=max(mfe,z*(b[j,1 if z==1 else 2]-entry)/risk);mae=max(mae,-z*(b[j,2 if z==1 else 1]-entry)/risk)
        for q in mem.zones:
            if q['created']==j:q['mfe_before_creation_r']=prior_mfe
            if q['hold']==j:q['mfe_at_hold_r']=mfe
        exp=z*(c-best)>=.25-1e-9 or bos(b,j,z) or displacement(b,j,z)
        ne=0 if exp else ne+1
        if z*(c-best)>0:best=c
        if warning<0 and ne>=3 and known-o['fill_ms']>=180000 and mfe<.75:warning=j;ev.append(('NE_WARNING',z))
        atr=max(.25,_prior_atr20(b,j) or .25);ts=pd.Timestamp(known,unit='ms',tz='UTC').tz_convert('America/New_York');minute=ts.hour*60+ts.minute
        dist=z*(c-(zone[0] if z==1 else zone[1]))/risk if zone else 0.
        state=dict(side=z,current_r=z*(c-entry)/risk,mfe_r=mfe,mae_r=mae,log_elapsed=np.log1p(j-fi),ne=min(120,ne),warning_seen=int(warning>=0),warning_age=min(120,j-warning) if warning>=0 else 120,log_risk=np.log(risk),atr_r=atr/risk,range_atr=(b[j,1]-b[j,2])/atr,return3_r=z*(c-b[max(fi,j-3),3])/risk,entry_fvg_missing=int(zone is None),entry_fvg_broken=int(zone is not None and dist<0),entry_fvg_distance_r=dist,time_sin=np.sin(2*np.pi*minute/1440),time_cos=np.cos(2*np.pi*minute/1440),**mem.state(j,z,risk))
        for lag in range(12):
            k=j-lag
            for col,key in enumerate(['open','high','low','close']):state[f'lag{lag}_{key}_r']=z*(b[k,col]-entry)/risk if k>=0 else 0.
        ends={}
        for tf,context in contexts.items():
            vals,ct=context.at(known,z,risk);state.update({f'm{tf}_'+k:v for k,v in vals.items()});ends[f'm{tf}_closed_ms']=ct
        assert np.isfinite(list(state.values())).all()
        out.append(dict(bar=j,known_ms=known,events=';'.join(('SUP_' if az==z else 'OPP_')+name for name,az in ev),**ends,**state))
    return out

class BroadContext:
    def __init__(self,raw):
        self.raw=raw;self.tables={};self.h1=Context(raw,60)
        for tf in [5,15,60]:
            a=aggregate(raw,tf);n=len(a);c=a[:,4];hi=a[:,2];lo=a[:,3];f=pd.DataFrame(a,columns=['ms','o','h','l','c'])
            f['ema20']=f.c.ewm(span=20,adjust=False).mean();f['ema50']=f.c.ewm(span=50,adjust=False).mean();f['ema20_slope']=f.ema20.diff(3).fillna(0)
            tr=pd.concat([f.h-f.l,(f.h-f.c.shift()).abs(),(f.l-f.c.shift()).abs()],axis=1).max(axis=1);f['atr']=tr.rolling(20,min_periods=1).mean()
            f['efficiency10']=((f.c-f.c.shift(10)).abs()/f.c.diff().abs().rolling(10).sum().replace(0,np.nan)).fillna(0)
            f['ret3']=f.c.diff(3).fillna(0);f['ret12']=f.c.diff(12).fillna(0);f['hi20']=f.h.rolling(20,min_periods=1).max();f['lo20']=f.l.rolling(20,min_periods=1).min();f['range_position']=((f.c-f.lo20)/(f.hi20-f.lo20).replace(0,np.nan)).fillna(.5)
            activehi=[];activelo=[];last_hi=np.nan;last_lo=np.nan;records=[]
            for j in range(n):
                # Only previously known unswept pivots are eligible for current-bar sweeps.
                activehi=[(v,k) for v,k in activehi if j-k<=120];activelo=[(v,k) for v,k in activelo if j-k<=120]
                sweep_hi=any(hi[j]>=v and c[j]<v for v,k in activehi);sweep_lo=any(lo[j]<=v and c[j]>v for v,k in activelo)
                activehi=[(v,k) for v,k in activehi if hi[j]<v];activelo=[(v,k) for v,k in activelo if lo[j]>v]
                k=j-2
                if k>=2 and a[j,0]-a[k-2,0]==4*tf*60000:
                    if hi[k]>max(hi[k-2:k].max(),hi[k+1:k+3].max()):last_hi=hi[k];activehi.append((hi[k],j))
                    if lo[k]<min(lo[k-2:k].min(),lo[k+1:k+3].min()):last_lo=lo[k];activelo.append((lo[k],j))
                above=[v for v,k in activehi if v>c[j]];below=[v for v,k in activelo if v<c[j]]
                records.append(dict(swing_hi=last_hi,swing_lo=last_lo,liq_hi=min(above) if above else np.nan,liq_lo=max(below) if below else np.nan,liq_hi_count=len(above),liq_lo_count=len(below),sweep_hi=int(sweep_hi),sweep_lo=int(sweep_lo)))
            f=pd.concat([f,pd.DataFrame(records)],axis=1);f['closed_ms']=f.ms+tf*60000;self.tables[tf]=f
        day=(raw[:,0]//86400000).astype('int64');df=pd.DataFrame(dict(day=day,h=raw[:,2],l=raw[:,3]));self.days=df.groupby('day').agg(hi=('h','max'),lo=('l','min'),count=('h','size'))
    def at(self,known,z,risk):
        vals={};meta={};cix=np.searchsorted(self.raw[:,0]+60000,known,side='right')-1;c=self.raw[cix,4]
        for tf,f in self.tables.items():
            k=np.searchsorted(f.closed_ms,known,side='right')-1;prefix=f'ctx{tf}_';x={};row=f.iloc[k] if k>=0 else None
            x['available']=int(k>=20);x['age_minutes']=min(1440,(known-row.closed_ms)/60000) if row is not None else 1440
            for key in ['ema20','ema50','hi20','lo20','swing_hi','swing_lo','liq_hi','liq_lo']:
                present=row is not None and pd.notna(row[key]);x[key+'_present']=int(present);x[key+'_distance_r']=z*(row[key]-c)/risk if present else 0.
            for key in ['ema20_slope','ret3','ret12']:x[key+'_r']=z*row[key]/risk if row is not None else 0.
            x['ema_spread_r']=z*(row.ema20-row.ema50)/risk if row is not None else 0.;x['trend_aligned']=int(z*(row.ema20-row.ema50)>0) if row is not None else 0
            for key in ['efficiency10','liq_hi_count','liq_lo_count','sweep_hi','sweep_lo']:x[key]=float(row[key]) if row is not None else 0.
            x['atr_r']=row.atr/risk if row is not None else 0.;x['range_position_aligned']=(row.range_position if z==1 else 1-row.range_position) if row is not None else .5
            vals.update({prefix+k:v for k,v in x.items()});meta[prefix+'closed_ms']=int(row.closed_ms) if row is not None else None
        h,ht=self.h1.at(known,z,risk);vals.update({'h1_'+k:v for k,v in h.items()});meta['h1_closed_ms']=ht
        day=int(known-1)//86400000;prev=self.days.loc[day-1] if day-1 in self.days.index else None
        vals['prev_utc_day_available']=int(prev is not None);vals['prev_utc_day_bars']=int(prev['count']) if prev is not None else 0
        for key in ['hi','lo']:vals['prev_utc_day_'+key+'_distance_r']=z*(prev[key]-c)/risk if prev is not None else 0.
        start=np.searchsorted(self.raw[:,0],day*86400000);part=self.raw[start:cix+1]
        vals['utc_day_hi_distance_r']=z*(part[:,2].max()-c)/risk if len(part) else 0.;vals['utc_day_lo_distance_r']=z*(part[:,3].min()-c)/risk if len(part) else 0.
        assert np.isfinite(list(vals.values())).all();return vals,meta

def sequence_stream(raw,o,end):
    b=raw[:,1:5];ms=raw[:,0].astype(np.int64)
    fi=int(np.searchsorted(ms,int(o['fill_ms'])))
    z=-1 if o['direction']=='LONG' else 1
    risk=float(o['risk']);latest=None;broken=set();breaks={};zones=[];rows=[]
    last_bos=-100000;invalid=0
    for j in range(max(4,fi-120),end):
        if j>fi and ms[j]-ms[j-1]!=60000:break
        k=j-2;col=2 if z==-1 else 1;level=b[k,col]
        others=np.r_[b[k-2:k,col],b[k+1:k+3,col]]
        if ((level<others).all() if z==-1 else (level>others).all()):latest=(k,float(level))
        event=None
        if latest and latest[0] not in broken and z*(b[j,3]-latest[1])>=.25-1e-9:
            broken.add(latest[0]);event=dict(bar=j,pivot=latest[0],level=latest[1],known_ms=int(ms[j])+60000)
            breaks[j]=event;last_bos=j
        if j<=fi:continue
        hits=[]
        for q in zones:
            if not q['valid']:continue
            edge=q['hi'] if z==-1 else q['lo']
            if z*(b[j,3]-edge)<-1e-9:q['valid']=False;invalid+=1;continue
            if j==q['created']+1:q['hold']=j
            elif q['hold'] is not None and (j-q['hold'])*60000<=600000:
                expansion=z*(b[j,3]-b[j,0])>0 and z*(b[j,3]-q['best_close'])>=.25-1e-9
                if event is not None and expansion:
                    hits.append(dict(bar=j,known_ms=int(ms[j])+60000,first_bos=q['first_bos'],created=q['created'],hold=q['hold'],second_bos=event,lo=q['lo'],hi=q['hi']))
            if z*(b[j,3]-q['best_close'])>0:q['best_close']=float(b[j,3])
        if j>=fi+2:
            bounds=gap(b,j,z)
            source=j-1 if j-1 in breaks and j-1>fi else j if j in breaks else None
            if bounds and source is not None:
                zones.append(dict(created=j,first_bos=breaks[source],lo=bounds[0],hi=bounds[1],hold=None,valid=True,best_close=float(b[j,3])))
        active=[q for q in zones if q['valid']]
        held=[q for q in active if q['hold'] is not None and j-q['hold']<=10]
        q=held[-1] if held else active[-1] if active else None
        rows.append(dict(bar=j,known_ms=int(ms[j])+60000,seq_allowed=int(bool(hits)),seq_bos_now=int(event is not None),seq_bos_age=min(120,j-last_bos),seq_pivot_distance_r=z*(b[j,3]-latest[1])/risk if latest else 0.,seq_pivot_age=min(120,j-latest[0]) if latest else 120,seq_active=min(10,len(active)),seq_held_window=min(10,len(held)),seq_invalidated=min(20,invalid),seq_zone_age=min(120,j-q['created']) if q else 120,seq_hold_age=min(120,j-q['hold']) if q and q['hold'] is not None else 120,seq_width_r=(q['hi']-q['lo'])/risk if q else 0.,seq_edge_r=z*(b[j,3]-(q['hi'] if z==-1 else q['lo']))/risk if q else 0.,sequence_event=hits[0] if hits else None))
    return rows

def library_stream(raw,o,context):
    b=raw[:,1:5];ms=raw[:,0].astype(np.int64);fi=int(np.searchsorted(ms,int(o['fill_ms'])))
    z=1 if o['direction']=='LONG' else -1;end=int(context[-1]['bar'])+1 if context else fi
    lookup={int(s['bar']):s for s in context};latest={};broken={1:set(),-1:set()};breaks={1:{},-1:{}}
    observations=[];support=[];opposing=[];warn={r:None for r in ('S4','S5','S7')};last_sup=-100000;out=[]
    def signal(rule,j,stages,**meta):return dict(rule=rule,bar=j,known_ms=int(ms[j])+60000,stages=stages,**meta)
    def stage(name,j,price):return dict(name=name,known_ms=int(ms[j])+60000,bar=j,price=float(price))
    for j in range(max(4,fi-120),end):
        if j>fi and ms[j]-ms[j-1]!=60000:break
        events={}
        for az in (-1,1):
            k=j-2;col=2 if az==-1 else 1;v=b[k,col];others=np.r_[b[k-2:k,col],b[k+1:k+3,col]]
            if ((v<others).all() if az==-1 else (v>others).all()):latest[az]=(k,float(v))
            pivot=latest.get(az)
            if pivot and pivot[0] not in broken[az] and az*(b[j,3]-pivot[1])>=.25-1e-9:
                broken[az].add(pivot[0]);events[az]=dict(bar=j,level=pivot[1]);breaks[az][j]=events[az]
                if az==z and j>fi:last_sup=j
        if j<=fi:continue
        s=lookup[j];c=float(b[j,3]);adverse=-z*(c-b[j-1,3])>=.25-1e-9;body=-z*(c-b[j,0])>0;opp=events.get(-z);hits=[]
        # S2: a frozen broken pivot, failed reclaim, then a NEW structural break.
        for q in observations:
            if not q['valid']:continue
            if j-q['start']>10 or z*(c-q['level'])>=.25-1e-9:q['valid']=False;continue
            if q['retest'] is None:
                if j>q['start'] and z*(b[j,1 if z==1 else 2]-q['level'])>=-1e-9 and -z*(c-q['level'])>=.25-1e-9:
                    q['retest']=j;q['stages'].append(stage('Nieudany powrót',j,c))
            elif j>q['retest'] and opp and adverse and body:
                hits.append(signal('S2',j,q['stages']+[stage('Kolejny BOS',j,c)],level=q['level']));q['valid']=False
        if opp:observations.append(dict(start=j,level=opp['level'],retest=None,valid=True,stages=[stage('Przeciwny BOS',j,c)]))
        # S3: supportive held gap must have delivered before failing.
        for q in support:
            if not q['valid']:continue
            far=q['lo'] if z==1 else q['hi'];near=q['hi'] if z==1 else q['lo']
            failed=-z*(c-far)>=.25-1e-9
            if q['broken'] is not None:
                if j-q['broken']>10 or not failed:q['valid']=False;continue
                if q['retest'] is None:
                    if j>q['broken'] and z*(b[j,1 if z==1 else 2]-far)>=-1e-9:
                        q['retest']=j;q['stages'].append(stage('Retest nie utrzymał',j,c))
                elif j>q['retest'] and opp and adverse and body:
                    hits.append(signal('S3',j,q['stages']+[stage('Przeciwny BOS',j,c)],lo=q['lo'],hi=q['hi']));q['valid']=False
                continue
            if failed:
                if q['delivered'] is not None:q['broken']=j;q['stages'].append(stage('Utrata FVG',j,c))
                else:q['valid']=False
                continue
            if j==q['created']+1:q['hold']=j;q['stages'].append(stage('FVG utrzymane',j,c))
            if q['hold'] is not None and j>q['hold'] and z*(c-near)>=.25-1e-9 and q['delivered'] is None:
                q['delivered']=j;q['stages'].append(stage('Ruch zgodny',j,c))
        # Warning-driven distinct families. Runs count ONLY after the warning/retest.
        for rule,q in warn.items():
            if q is None:continue
            limit=.25 if rule=='S5' else .75
            if j-q['start']>10 or s['mfe_r']>=limit or s['ne']==0:warn[rule]=None;continue
            q['run']=q['run']+1 if adverse else 0
            if rule=='S4':
                failed=-z*(c-q['level'])>=.25-1e-9
                if q['broken'] is None:
                    if failed:q['broken']=j;q['stages'].append(stage('Wybicie zakresu',j,c))
                elif not failed:warn[rule]=None
                elif q['retest'] is None:
                    if j>q['broken'] and z*(b[j,1 if z==1 else 2]-q['level'])>=-1e-9:q['retest']=j;q['run']=0;q['stages'].append(stage('Nieudany retest',j,c))
                elif j>q['retest'] and q['run']>=3:
                    hits.append(signal(rule,j,q['stages']+[stage('3 przeciwne zamknięcia',j,c)],level=q['level']));warn[rule]=None
            elif rule=='S5':
                htf=s['m5_closed_ms']
                if q['m5'] is None and np.isfinite(htf) and htf>ms[q['start']]+60000 and s['m5_opp_bos_age']==0:
                    q['m5']=j;q['stages'].append(stage('Przeciwny BOS M5',j,c))
                elif q['m5'] is not None and j>q['m5'] and opp and q['run']>=3:
                    hits.append(signal(rule,j,q['stages']+[stage('BOS M1 + 3 zamknięcia',j,c)]));warn[rule]=None
            elif opp and q['run']>=3:
                hits.append(signal(rule,j,q['stages']+[stage('Przeciwny BOS + 3 zamknięcia',j,c)]));warn[rule]=None
        if s['ne']==3:
            for rule in ('S4','S5','S7'):
                if warn[rule] is not None:continue
                eligible=(.25<=s['mfe_r']<.75) if rule=='S4' else (s['mfe_r']<.25) if rule=='S5' else (s['mfe_r']<.75 and 0<j-last_sup<=10)
                if eligible:
                    stages=([stage('BOS zgodny z pozycją',last_sup,b[last_sup,3])] if rule=='S7' else [])+[stage('3 świece zastoju',j,c)]
                    warn[rule]=dict(start=j,run=0,broken=None,retest=None,m5=None,level=float(b[j-2:j+1,2].min() if z==1 else b[j-2:j+1,1].max()),stages=stages)
        # S6 explicitly excludes the initial BOS that defines S1.
        for q in opposing:
            if not q['valid']:continue
            edge=q['hi'] if z==1 else q['lo']
            if z*(c-edge)>1e-9:q['valid']=False;continue
            if j==q['created']+1:q['hold']=j;q['stages'].append(stage('Przeciwne FVG utrzymane',j,c))
            elif q['hold'] is not None:
                if j-q['hold']>10:q['valid']=False;continue
                q['run']=q['run']+1 if adverse else 0
                if opp and q['run']>=3:
                    hits.append(signal('S6',j,q['stages']+[stage('3 zamknięcia + BOS',j,c)],lo=q['lo'],hi=q['hi']));q['valid']=False
        if j>=fi+2:
            for az in (z,-z):
                lo,hi=(b[j-2,1],b[j,2]) if az==1 else (b[j,1],b[j-2,2])
                if hi-lo<3-1e-9:continue
                if az==-z and (j in breaks[-z] or j-1 in breaks[-z]):continue
                q=dict(created=j,lo=float(lo),hi=float(hi),hold=None,delivered=None,broken=None,retest=None,run=0,valid=True,stages=[stage('FVG powstało',j,c)])
                (support if az==z else opposing).append(q)
        out.append(dict(bar=j,known_ms=int(ms[j])+60000,signals=hits))
    return out

def zone_at(b,ms,j,z,size):
    if j<2:return None
    lo,hi=(b[j-2,1],b[j,2]) if z==1 else (b[j,1],b[j-2,2])
    if hi-lo<size-1e-9:return None
    return dict(j=j,created_ms=int(ms[j])+60000,lo=float(lo),hi=float(hi),valid=True,held=False,delivered=False)

def detect(b,ms,fill_ms,entry,risk,direction,setup_bar,min_gap):
    z=1 if direction=='LONG' else -1;fi=int(np.searchsorted(ms,fill_ms));assert ms[fi]==fill_ms
    assert risk>0 and np.isfinite(b).all()
    best=float(b[fi,3]);mfe=0.;ne=0;post=[];entry_zone=None
    for j in range(max(2,setup_bar),fi):
        q=zone_at(b,ms,j,z,min_gap)
        if q is None:continue
        edge=q['lo'] if z==1 else q['hi']
        if z*(entry-edge)<-1e-9 or np.any(z*(b[j+1:fi,3]-edge)<-1e-9):continue
        entry_zone=q
    states={r:None for r in ('A','B')};done={r:False for r in states};events={r:None for r in states}
    flags={r:dict(zone_available=bool(entry_zone) if r=='A' else False,warning=False,failure=False,retest=False,recovery=False,expiry=False,gap=False) for r in states}
    barred={r:set() for r in states}
    if entry_zone is not None and z*(float(b[fi,3])-(entry_zone['lo'] if z==1 else entry_zone['hi'])) < -1e-9:
        entry_zone['valid']=False
    for j in range(fi+1,len(ms)):
        if ms[j]-ms[j-1]!=60000:
            for r in states:flags[r]['gap']=True
            break
        known=int(ms[j])+60000;close=float(b[j,3]);prev=float(b[j-1,3])
        mfe=max(mfe,z*(float(b[j,1 if z==1 else 2])-entry)/risk)
        exp=z*(close-best)>=.25-1e-9 or displacement(b,j,z) or bos(b,j,z)
        if z*(close-best)>0:best=close
        ne=0 if exp else ne+1
        # Every observation follows its own previously chosen zone.
        for rule,q in states.items():
            if done[rule] or q is None:continue
            far=q['lo'] if z==1 else q['hi'];near=q['hi'] if z==1 else q['lo']
            failed=z*(close-far)<-1e-9
            if (q.get('failure_ms') is None and exp) or (q.get('failure_ms') is not None and z*(close-near)>=.25-1e-9 and exp):
                flags[rule]['recovery']=True;barred[rule].add(q['j']);states[rule]=None;continue
            if q.get('failure_ms') is None:
                if failed:
                    q.update(failure_ms=known,failure_bar=j,retest_bar=None,run=0)
                    flags[rule]['failure']=True
                continue
            if known>q['failure_ms']+600000:
                flags[rule]['expiry']=True;barred[rule].add(q['j']);states[rule]=None;continue
            if not failed:
                q.update(retest_bar=None,run=0,needs_new_break=True);continue
            if q.pop('needs_new_break',False):
                q['failure_bar']=j;continue
            if q['retest_bar'] is None:
                wick=float(b[j,1] if z==1 else b[j,2])
                if j>q['failure_bar'] and z*(wick-far)>=-1e-9:
                    q['retest_bar']=j;q['retest_ms']=known;q['run']=0;flags[rule]['retest']=True
                continue
            q['run']=q['run']+1 if -z*(close-prev)>=.25-1e-9 else 0
            if q['run']>=3:
                events[rule]=dict(rule=rule,bar=j,known_ms=known,zone_created_ms=q['created_ms'],lower=q['lo'],upper=q['hi'],warning_ms=q['warning_ms'],failure_ms=q['failure_ms'],retest_ms=q['retest_ms'],close=close,close_run=q['run'])
                done[rule]=True
        # Update causal validity and delivery of entry / postfill zones.
        for q in ([entry_zone] if entry_zone else [])+post:
            if not q['valid']:continue
            far=q['lo'] if z==1 else q['hi'];near=q['hi'] if z==1 else q['lo']
            if z*(close-far)<-1e-9:q['valid']=False;continue
            if j==q['j']+1:q['held']=True
            if j>q['j']+1 and q['held'] and exp and z*(close-near)>=.25-1e-9:q['delivered']=True
        if ne>=3:
            for rule in states:
                if done[rule] or states[rule] is not None:continue
                if rule=='A':
                    candidates=[entry_zone] if mfe<.25 and entry_zone and entry_zone['valid'] else []
                else:
                    candidates=[q for q in post if q['valid'] and q['held'] and q['delivered']] if .25<=mfe<.75 else []
                candidates=[q for q in candidates if q['j'] not in barred[rule]]
                if candidates:
                    q=dict(candidates[-1],warning_ms=known)
                    states[rule]=q;flags[rule]['warning']=True
        q=zone_at(b,ms,j,z,min_gap)
        if q is not None and j-1>fi:
            post.append(q);flags['B']['zone_available']=True
    return events,flags


def build(order, position, minutes, known_ms):
    """Use completed same-contract M1 only; reject missing metadata, never impute it."""
    times = np.array([int(t.value//1000000) for t in minutes.index], dtype=np.int64)
    use = times+60000 <= known_ms
    raw = np.column_stack([times[use], minutes.loc[use, ['open','high','low','close']].to_numpy(float)])
    meta = order['boundary_context']
    fill = int(position['fill_ms'])//60000*60000
    fi = int(np.searchsorted(raw[:,0], fill))
    if fi >= len(raw) or raw[fi,0] != fill: raise ValueError('boundary_fill_bar_missing')
    if len(raw)-fi < 2: return pd.DataFrame()
    if not np.all(np.diff(raw[fi:,0]) == 60000): raise ValueError('boundary_postfill_m1_gap')
    setup = int(np.searchsorted(raw[:,0], meta['setup_ms']))
    if setup >= len(raw) or raw[setup,0] != meta['setup_ms']: raise ValueError('boundary_setup_bar_missing')
    if raw[0,0] > meta['context_start_ms']: raise ValueError('boundary_warmup_missing')
    if any(len(aggregate(raw,t)) == 0 for t in (5,15,60)): raise ValueError('boundary_htf_warmup_missing')
    o = dict(order_id=order['order_id'], direction=order['direction'], fill_ms=fill,
             entry=position['entry_price'], risk=order['risk_points'], s=setup)
    context = {tf:Context(raw,tf) for tf in (5,15)}
    rows = episode(raw,o,len(raw),context)
    seq = sequence_stream(raw,o,len(raw)); broad=BroadContext(raw)
    z=1 if o['direction']=='LONG' else -1; events={}
    def event(rule,at):
        if at is not None: events.setdefault(int(at),set()).add(rule)
    for st,sq in zip(rows,seq):
        st.update({k:v for k,v in sq.items() if k.startswith('seq_')})
        known=st['known_ms'];vals,metadata=broad.at(known,z,o['risk'])
        st.update(vals);st.update(metadata)
        close=o['entry']+z*st['current_r']*o['risk']
        st.update(entry_source_distance_r=z*(meta['source_price']-close)/o['risk'],
                  entry_source_age_days=min(30,(known-meta['source_formed_ms'])/86400000),
                  setup_fvg_lo_distance_r=z*(meta['fvg_lo']-close)/o['risk'],
                  setup_fvg_hi_distance_r=z*(meta['fvg_hi']-close)/o['risk'])
        if st['seq_allowed']: event('S1',known)
    for record in library_stream(raw,o,rows):
        for e in record['signals']:
            if e['rule'] in ('S2','S3','S4'): event(e['rule'],e['known_ms'])
    retests,_=detect(raw[:,1:],raw[:,0].astype(np.int64),fill,o['entry'],o['risk'],o['direction'],setup,.25)
    for rule,e in retests.items():
        if e: event('FR_'+rule,e['known_ms'])
    # Frozen expert registry includes M1/M2 observations, although only M3 executes.
    fm=minutes.loc[use]
    frozen=evaluate(order,dict(position,fill_ms=fill),fm,known_ms,
                    enabled={'M1':True,'M2':True,'M3':True})
    for rule,at in frozen['first_events'].items(): event(rule,at)
    peak=-np.inf;last_peak=0;best=0.
    for k,st in enumerate(rows):
        if st['mfe_r']>peak+1e-9:last_peak=k;peak=st['mfe_r']
        best=max(best,st['current_r'])
        st.update(gb_current_drawdown_r=st['mfe_r']-st['current_r'],gb_best_close_r=best,
                  gb_close_drawdown_r=best-st['current_r'],gb_peak_age=min(120,k-last_peak),
                  order_id=order['order_id'],manager_rules=';'.join(sorted(events.get(st['known_ms'],set()))))
    return pd.DataFrame(rows)
