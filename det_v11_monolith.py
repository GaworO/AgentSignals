# det_v11.py — v11 (2026-06): SWEEP/LIQUIDITY invalidation for H/L levels (zamiast CZASU).
# Bazuje na PROD v2 (det_v10). ZMIANA vs v10: poziomy płynności (H/L sesji, PDH/PDL, PWH/PWL,
# BSL/SSL) NIE umierają po czasie — żyją do SWEEPU (zebrania płynności), z backstopem 10 dni.
#   MODE='confirm' (DOMYSLNE): v10 multi-break (re-arm po BUF), poziom umiera po PIERWSZYM setupie.
#   MODE='sweep'           : poziom umiera na pierwszym przebiciu (knot LUB body); 1 strzał na poziom.
# Reszta = v10 bez zmian: F.P.FVG (czas), NDOG/NWOG/VI (gap), bias, displacement, wejście, CUTOFF, pickle.
# Reguła odbicia = BODY trzyma 50% FVG (close>=CE); DIB always-on (+DIB = klasa B). Wejście CE, BE@1R/TP2R.
import pandas as pd, numpy as np, pickle, datetime as dt
from collections import defaultdict, Counter, OrderedDict

# ============ LOADER ============
import os
df=pd.read_csv(os.environ.get('DATA_CSV','/mnt/user-data/uploads/MNQ_databento_1m.csv'))
ts=pd.to_datetime(df.ts_event,utc=True).dt.as_unit('ns')   # pandas3 = us -> wymus ns (T=//1e9)
df=df.assign(ts=ts).sort_values('ts').reset_index(drop=True); ts=df.ts
df['dt']=ts.dt.tz_convert('Etc/GMT+4')   # sztywne UTC-4 (jak TFO), bez DST
o,hi,lo,cl=df.open.values,df.high.values,df.low.values,df.close.values
T=(ts.astype('int64')//10**9).values
H=df.dt.dt.hour.values; Mi=df.dt.dt.minute.values
df['date']=df.dt.dt.date.values; dates=df.date.values; n=len(df); mins=H*60+Mi
days=sorted(df.date.unique()); dayi={d:i for i,d in enumerate(days)}

# indeksy pierwszego/ostatniego bara w dniu (szybkie lookupy, równoważne df.index[df.date==d])
day_first_idx={}; day_last_idx={}; day_idx=defaultdict(list)
for i in range(n):
    d=dates[i]
    if d not in day_first_idx: day_first_idx[d]=i
    day_last_idx[d]=i; day_idx[d].append(i)

# ============ 5m ATR mapped to 1m ============
b5=T//300
g5=df.assign(b5=b5).groupby('b5').agg(h5=('high','max'),l5=('low','min'))
g5['atr']=(g5.h5-g5.l5).rolling(20).mean().shift(1)
ATR=df.assign(b5=b5).merge(g5[['atr']],left_on='b5',right_index=True,how='left')['atr'].values
ATR=np.where(np.isnan(ATR),0.0,ATR)

# ============ SESSIONS (TFO windows, UTC-4) ============
def sess(h,m):
    if h>=20:return'ASIA'
    if 2<=h<5:return'LO'
    if (h==9 and m>=30)or(10<=h<12):return'NYAM'
    if h==12:return'NYL'
    if (h==13 and m>=30)or(14<=h<16):return'NYPM'
    if 16<=h<20:return'PM_AH'
    return'PREM'
S=np.array([sess(h,m) for h,m in zip(H,Mi)])
inst=[];cid=-1;prev=None
for s in S:
    if s!=prev:cid+=1
    inst.append(cid);prev=s
inst=np.array(inst)
sessinst=[]
for c in np.unique(inst):
    ix=np.where(inst==c)[0]
    sessinst.append((S[ix[0]],int(ix[0]),int(ix[-1]),float(hi[ix].max()),float(lo[ix].min())))

# ============ CATALYSTS ============
# F.P.FVG : pierwszy FVG sesji NYAM danego dnia (v10, bez zmian — czas)
nyam_by_day=defaultdict(list)
for i in np.where(S=='NYAM')[0]: nyam_by_day[dates[i]].append(int(i))
fpfvg={}
for d in days:
    ix=nyam_by_day.get(d,[])
    for kk in range(2,len(ix)):
        k,k2=ix[kk],ix[kk-2]
        if lo[k]>hi[k2]: fpfvg[d]=(hi[k2],lo[k],k); break
        if hi[k]<lo[k2]: fpfvg[d]=(hi[k],lo[k2],k); break
# PDH/PDL
gday=df.groupby('date').agg(dh=('high','max'),dl=('low','min'))
day_hl={d:(float(gday.dh[d]),float(gday.dl[d])) for d in days}
# NDOG/NWOG -> JEDNA jednostka, poziom = .c (close), 2 / 5 dni, uniewaznienie przy przejsciu (v10)
gaplev=[]
for d in days:
    di=day_idx[d]; Hd=H[di]
    bef=[di[k] for k in range(len(di)) if Hd[k]<17]
    aft=[di[k] for k in range(len(di)) if Hd[k]>=18]
    wd=pd.Timestamp(d).weekday()
    if bef and aft and wd<4:
        c17=cl[bef[-1]]; ta=T[aft[0]]; gaplev.append([c17,ta,'NDOG',d,2])
    if wd==6:
        sun=aft; frd=[x for x in days if pd.Timestamp(x).weekday()==4 and x<d]
        if sun and frd:
            fdi=day_idx[frd[-1]]; Hf=H[fdi]
            fri=[fdi[k] for k in range(len(fdi)) if Hf[k]<17]
            if fri:
                fc=cl[fri[-1]]; ta=T[sun[0]]; gaplev.append([fc,ta,'NWOG',d,5])
for g in gaplev:
    pr,ta=g[0],g[1]; ct=float('inf')
    idxs=np.where((T>ta)&(lo<=pr)&(hi>=pr))[0]
    if len(idxs): ct=T[idxs[0]]
    g.append(ct)
# BSL/SSL : rowne high/low z H1
g1=df.set_index(ts).resample('1h').agg(h=('high','max'),l=('low','min')).dropna()
H1,L1=g1.h.values,g1.l.values; G1=(g1.index.astype('int64')//10**9).values
swh=[(H1[k],G1[k+2]) for k in range(2,len(g1)-2) if H1[k]>=max(H1[k-1],H1[k-2],H1[k+1],H1[k+2])]
swl=[(L1[k],G1[k+2]) for k in range(2,len(g1)-2) if L1[k]<=min(L1[k-1],L1[k-2],L1[k+1],L1[k+2])]
def equals(sw,tol=4.):
    eq=[]
    for i in range(len(sw)):
        for j in range(i+1,len(sw)):
            if sw[j][1]-sw[i][1]>86400: break
            if abs(sw[i][0]-sw[j][0])<=tol: eq.append((round((sw[i][0]+sw[j][0])/2,1),sw[j][1]))
    return eq
eqH=equals(swh); eqL=equals(swl)

# ============ PARAMS ============
TOL=3.            # tolerancja CE / krawedzi FVG
LOOKBACK=15       # struktura krotkoterminowa do break of structure
ATRMULT=1.5       # sila displacementu: suma cial >= ATRMULT*ATR5m
DISPWIN=10        # ile barow po triggerze szukam impulsu
MAXIMP=3          # max swiec impulsu (uzywane przez DIB / klasa B)
MINIMP=int(os.environ.get('MINIMP','4'))   # V1 (2026-06): min dlugosc ciagu displacementu (>3 swiec)
MAXEXT=40         # V1: bezpiecznik max dlugosci ciagu
RETWIN=20         # okno na retrace do 50% FVG
BOSWIN=30         # okno na BOS po odbiciu
BUF=3.
VIMIN=10.         # min luka body-to-body, by liczyc VI jako katalizator
_cur_break=1      # AB: numer przebicia biezacego triggera
VIBIG=50.         # min VI, by dzialal jako magnes (TP/bias)
# --- v11 ---
MODE=os.environ.get('MODE','confirm')        # 'confirm' (DOMYSLNE) | 'sweep'
CAP_DAYS=int(os.environ.get('CAP_DAYS','10')) # backstop: poziom umiera po N dniach handlowych jesli nie zebrany
def dayidx_for(epoch):
    i=int(np.searchsorted(T,epoch)); return min(max(i,0),n-1)

# ============ VOLUME IMBALANCE (luka body-to-body open[k] vs close[k-1]) ============
vis=[]   # (lo,hi,bar,bull,mag)
for k in range(1,n):
    g=o[k]-cl[k-1]
    if g>=VIMIN and cl[k]>o[k]:  vis.append((round(float(cl[k-1]),2),round(float(o[k]),2),k,True,round(float(g),1)))
    if -g>=VIMIN and cl[k]<o[k]: vis.append((round(float(o[k]),2),round(float(cl[k-1]),2),k,False,round(float(-g),1)))
bigvi=[]   # duze VI + bar domkniecia (magnes wazny dopoki niedomkniety)
for a,b,bar,bull,mag in vis:
    if mag<VIBIG: continue
    fillbar=n
    idx=np.where((lo[bar+1:]<=b)&(hi[bar+1:]>=a))[0]
    if len(idx): fillbar=bar+1+int(idx[0])
    bigvi.append((round((a+b)/2,2),bar,bull,mag,fillbar))
def vi_draw(t):
    """niedomkniety duzy VI najblizej (z 2 dni) -> kierunek magnesu i poziom"""
    up=dn=None
    for ce,bar,bull,mag,fillbar in bigvi:
        if not (bar<t<fillbar): continue
        if (T[t]-T[bar])>2*86400: continue
        if ce>cl[t] and (up is None or ce<up): up=ce
        if ce<cl[t] and (dn is None or ce>dn): dn=ce
    return up,dn

# ============ HELPERS ============
def fvgs(a,b,bull):
    out=[]
    for k in range(max(a,2),min(b,n)):
        if bull and lo[k]>hi[k-2] and lo[k]-hi[k-2]>=TOL: out.append((round(hi[k-2],1),round(lo[k],1),k))
        if not bull and hi[k]<lo[k-2] and lo[k-2]-hi[k]>=TOL: out.append((round(hi[k],1),round(lo[k-2],1),k))
    return out

def find_displacement(t,dr):
    """V1 (2026-06): displacement = CALY nieprzerwany ciag swiec jednego koloru, min MINIMP swiec.
    Start po triggerze t, rozszerz do konca ciagu; break struktury + zostawia FVG + sila (cala noga)."""
    bull = dr=='LONG'
    for s in range(t+1,min(t+1+DISPWIN,n)):
        if not ((cl[s]>o[s]) if bull else (cl[s]<o[s])): continue   # s = pierwsza swieca ciagu
        u=s
        while u+1<min(s+MAXEXT,n) and ((cl[u+1]>o[u+1]) if bull else (cl[u+1]<o[u+1])):
            u+=1                                                     # rozszerz przez caly ciag jednego koloru
        if (u-s+1)<MINIMP: continue                                 # wymagaj min MINIMP swiec (>3)
        body=sum((cl[x]-o[x]) if bull else (o[x]-cl[x]) for x in range(s,u+1))
        if body<=0: continue
        prior = max(hi[max(0,s-LOOKBACK):s]) if bull else min(lo[max(0,s-LOOKBACK):s])
        broke = (cl[u]>prior) if bull else (cl[u]<prior)
        if not broke: continue
        atr5=ATR[u] if ATR[u]>0 else 1e9
        maxbody=max((abs(cl[x]-o[x])) for x in range(max(0,s-10),s)) if s>0 else 0
        if body < ATRMULT*atr5: continue
        if body < maxbody: continue
        fl=fvgs(s,u+2,bull)
        if not fl: continue
        f=fl[-1]                       # FVG displacementu (najswiezszy)
        swlo=float(min(lo[s:u+1])); swhi=float(max(hi[s:u+1]))
        return dict(s=s,u=u,L=u-s+1,body=round(body,1),fvg=(f[0],f[1]),fvg_bar=f[2],
                    swlo=swlo,swhi=swhi,atr5=round(atr5,1))
    return None

def find_displacement_dib(t,dr):
    bull=dr=='LONG'; best=None
    for u in range(max(MAXIMP,t-2),min(t+3,n)):
        for L in range(1,MAXIMP+1):
            s=u-L+1
            if s<1: continue
            same=all((cl[x]>o[x]) if bull else (cl[x]<o[x]) for x in range(s,u+1))
            if not same: continue
            body=sum((cl[x]-o[x]) if bull else (o[x]-cl[x]) for x in range(s,u+1))
            if body<=0: continue
            prior=max(hi[max(0,s-LOOKBACK):s]) if bull else min(lo[max(0,s-LOOKBACK):s])
            broke=(cl[u]>prior) if bull else (cl[u]<prior)
            if not broke: continue
            atr5=ATR[u] if ATR[u]>0 else 1e9
            maxbody=max((abs(cl[x]-o[x])) for x in range(max(0,s-10),s)) if s>0 else 0
            if body<ATRMULT*atr5: continue
            if body<maxbody: continue
            fl=fvgs(s,u+2,bull)
            if not fl: continue
            f=fl[-1]; swlo=float(min(lo[s:u+1])); swhi=float(max(hi[s:u+1]))
            cand=dict(s=s,u=u,L=L,body=round(body,1),fvg=(f[0],f[1]),fvg_bar=f[2],swlo=swlo,swhi=swhi,atr5=round(atr5,1))
            if best is None or body>best['body']: best=cand
    return best

# ============ BIAS (v0, FLAGA nie filtr) ============
dd=df.set_index(ts).resample('1D').agg(h=('high','max'),l=('low','min'),c=('close','last')).dropna()
dD=dd.index.tz_convert('Etc/GMT+4').date
dH,dL,dC=dd.h.values,dd.l.values,dd.c.values
def bias_for(t):
    d=dates[t]; di=dayi[d]
    j=np.searchsorted(dD,d)
    if j<5: return ('niejasny','-')
    rngH=dH[j-5:j].max(); rngL=dL[j-5:j].min(); eq=(rngH+rngL)/2
    px=cl[t]
    pd_=  'discount' if px<eq else 'premium'
    up = dH[j-1]>dH[j-3] and dL[j-1]>dL[j-3]
    dn = dH[j-1]<dH[j-3] and dL[j-1]<dL[j-3]
    if pd_=='discount' and up: b='LONG'
    elif pd_=='premium' and dn: b='SHORT'
    elif pd_=='discount' and not dn: b='LONG?'
    elif pd_=='premium' and not up: b='SHORT?'
    else: b='niejasny'
    vu,vd=vi_draw(t)
    if b=='niejasny':
        if vu and not vd: b='LONG?'
        elif vd and not vu: b='SHORT?'
    elif b=='LONG?' and vu and not vd: b='LONG'
    elif b=='SHORT?' and vd and not vu: b='SHORT'
    return (b,pd_)

# ============ EMISJA SETUPU ============
out=[]
SESSION_BOUNDS=[0,8,13,18]
MAX_STOP_R=float(os.environ.get('MAX_STOP_R','40'))   # cap ryzyka (pkt)
_TRC=[]; _DBG=bool(os.environ.get('DEBUG_TRACE'))
def _trc(trigger,dr,model,name,stage,disp=None):
    if not _DBG: return
    r=dict(cat=name,model=model,dir=dr,trig=df.dt[trigger].strftime('%Y-%m-%d %H:%M'),
           trig_ms=int(df.dt[trigger].timestamp()*1000),stage=stage)
    if disp is not None: r['disp_end']=df.dt[disp['u']].strftime('%H:%M'); r['fvg']=[round(disp['fvg'][0],1),round(disp['fvg'][1],1)]
    _TRC.append(r)
def session_end_bar(b):
    t=df.dt.iloc[b]; h=t.hour
    nb=min(x for x in SESSION_BOUNDS+[24] if x>h)
    bound=t.normalize()+pd.Timedelta(hours=nb); j=b
    while j+1<n and df.dt.iloc[j+1]<bound: j+=1
    return j

def find_rejection_v10(disp,dr):
    bull=dr=='LONG'; fl,fh=disp['fvg']; ce=round((fl+fh)/2,2); fb=disp['fvg_bar']
    origin=None; ob=None; tests=[]; broke=None
    for j in range(fb+1,min(fb+1+RETWIN,n)):
        if (cl[j]>ce) if not bull else (cl[j]<ce): broke=j; break
        wick=(hi[j]>=fl) if not bull else (lo[j]<=fh)
        body=(cl[j]<=ce) if not bull else (cl[j]>=ce)
        if wick and body:
            ext=hi[j] if not bull else lo[j]; tests.append(j)
            if origin is None or (ext>origin if not bull else ext<origin): origin,ob=ext,j
    if broke is not None or origin is None: return None
    return dict(ce=ce,origin=round(origin,2),origin_bar=ob,tests=tests)

def find_setup_v10(disp,dr):
    bull=dr=='LONG'; rej=find_rejection_v10(disp,dr)
    if not rej: return None
    origin=rej['origin']; ob=rej['origin_bar']; ce=rej['ce']; s=disp['s']; u=disp['u']
    struct0=float(max(hi[s:ob])) if bull else float(min(lo[s:ob])); level=struct0
    for j in range(ob+1, min(ob+1+BOSWIN,n)):
        if (cl[j]>ce) if not bull else (cl[j]<ce): return None
        if (cl[j]>level) if bull else (cl[j]<level):
            end=float(max(hi[ob:j+1])) if bull else float(min(lo[ob:j+1]))
            return dict(dr=dr,origin=origin,origin_bar=ob,end=round(end,2),bos_bar=j,ce=ce,
                        fvg=disp['fvg'],fvg_bar=disp['fvg_bar'],s=s,u=u)
        level = max(level,hi[j]) if bull else min(level,lo[j])
    return None

def impulse_end_v10(ob,bb,bull,K=2,cap=40):
    ext=hi[ob] if bull else lo[ob]; eb=ob; stall=0
    for m in range(ob+1, min(n, bb+cap)):
        if (hi[m]>ext) if bull else (lo[m]<ext): ext=hi[m] if bull else lo[m]; eb=m; stall=0
        elif m>bb:
            stall+=1
            if stall>=K: break
    return round(float(ext),2), min(eb+K, n-1)   # FIX look-ahead: bar POTWIERDZENIA szczytu

def find_entry_v10(su):
    bull=su['dr']=='LONG'; ob=su['origin_bar']; bb=su['bos_bar']
    sl=round((su['fvg'][0]+su['fvg'][1])/2,2)
    fl_=fvgs(ob, bb+2, bull); seen=set(); fvl=[]
    for f in fl_:
        if f[2] in seen: continue
        seen.add(f[2]); fvl.append(f)
    if not fvl: return None
    fvg=fvl[-1]; entry=fvg[1] if bull else fvg[0]
    risk=(entry-sl) if bull else (sl-entry)
    if risk<=0: return None
    tp=round(entry+2*risk,2) if bull else round(entry-2*risk,2)
    return dict(entry=round(entry,2),sl=sl,tp=tp,risk=round(risk,2),sfvg_bar=fvg[2])

def find_entry_fibo_v10(su, ote=0.62):
    bull=su['dr']=='LONG'; ob=su['origin_bar']; bb=su['bos_bar']
    sl=round((su['fvg'][0]+su['fvg'][1])/2,2)
    hh,eb=impulse_end_v10(ob,bb,bull); hl=su['origin']
    entry=round(hh+ote*(hl-hh),2); risk=(entry-sl) if bull else (sl-entry)
    if risk<=0: return None
    tp=round(entry+2*risk,2) if bull else round(entry-2*risk,2)
    return dict(entry=entry,sl=sl,tp=tp,risk=round(risk,2),hh_bar=eb,ote=ote)

ENTRY_PRIMARY=os.environ.get('ENTRY_PRIMARY','fvg')
def _ent_fvg(su):
    e=find_entry_v10(su)
    return dict(**e, kind='FVG', start_bar=max(e['sfvg_bar'],su['bos_bar'])+1) if e else None
def _ent_fibo(su):
    e=find_entry_fibo_v10(su)
    return dict(**e, kind='FIBO', start_bar=max(e['hh_bar'],su['bos_bar'])+1) if e else None
def get_entry_v10(su):
    if ENTRY_PRIMARY=='fibo': return _ent_fibo(su) or _ent_fvg(su)
    return _ent_fvg(su) or _ent_fibo(su)

def emit(t,model,name,dr,disp,conf=None):   # wejscie v10 (FVG-edge/OTE), SL=CE disp, TP 2R
    _trc(t,dr,model,name,'displacement OK',disp)
    su=find_setup_v10(disp,dr)
    if su is None: _trc(t,dr,model,name,'brak setupu (odbicie/BOS)',disp); return
    _trc(t,dr,model,name,'setup OK (BOS)',disp)
    e=get_entry_v10(su)
    if e is None: _trc(t,dr,model,name,'brak wejscia',disp); return
    if e['risk']>MAX_STOP_R: _trc(t,dr,model,name,f'odciety cap (R={e["risk"]:.0f}pkt)',disp); return
    _trc(t,dr,model,name,'POTWIERDZONY',disp)
    b,pdv=bias_for(su['bos_bar']); align='Y' if b.replace('?','')==dr else ('?' if '?' in b or b=='niejasny' else 'N')
    out.append(dict(brk=_cur_break,date=str(dates[su['bos_bar']]),model=model,cat=name,dir=dr,
        cls=('B' if '+DIB' in name else 'A'),entry=e['entry'],SL=e['sl'],TP=e['tp'],risk=e['risk'],kind=e['kind'],
        bias=b,bias_align=align,bos=df.dt[su['bos_bar']].strftime('%H:%M'),
        s=int(disp['s']),u=int(disp['u']),fvg_lo=round(disp['fvg'][0],2),fvg_hi=round(disp['fvg'][1],2),
        fvg_bar=int(disp['fvg_bar']),origin_bar=int(su['origin_bar']),bos_bar=int(su['bos_bar']),ce=round(su['ce'],2),
        sfvg_bar=int(e['sfvg_bar']) if e.get('sfvg_bar') is not None else None,
        hh_bar=int(e['hh_bar']) if e.get('hh_bar') is not None else None,
        emit_bar=int(su['bos_bar']),entry_bar=int(e['start_bar']),
        bos_iso=df.dt[su['bos_bar']].strftime('%Y-%m-%dT%H:%M:%SZ'),
        bos_ms=int(df.dt[su['bos_bar']].timestamp()*1000)))

def try_chain(trigger,dr,model,name):   # chain v10 (klasa A); DIB = klasa B z TYM SAMYM wejsciem v10
    cur=trigger
    for _ in range(3):
        d=find_displacement(cur,dr)
        if d is None: break
        before=len(out); emit(trigger,model,name,dr,d)
        if len(out)>before: return
        cur=d['u']
    d2=find_displacement_dib(trigger,dr)
    if d2 is None: return
    emit(trigger,model,name+'+DIB',dr,d2)

# ============ RUSZTOWANIE KATALIZATOROW ============
def run_level(level,form_t,end_t,name,rev_dir,cont_dir):
    """v10 (CZAS): re-arm po cofnieciu o BUF; okno do end_t. UZYWANE TYLKO dla F.P.FVG (poza zakresem v11)."""
    global _cur_break
    a0=dayidx_for(form_t); a1=min(dayidx_for(end_t)+1,n)
    win=[i for i in range(a0,a1) if T[i]>form_t]
    if not win: return
    if rev_dir:
        bull=rev_dir=='LONG'; armed=True; k=0
        for i in win:
            hit=(lo[i]<=level) if bull else (hi[i]>=level)
            if armed and hit:
                k+=1; _cur_break=k; try_chain(i,rev_dir,'Reversal',name); armed=False
            elif (not armed) and ((lo[i]>level+BUF) if bull else (hi[i]<level-BUF)):
                armed=True
    if cont_dir:
        bull=cont_dir=='LONG'; armed=True; k=0
        for i in win:
            hit=(cl[i]>level) if bull else (cl[i]<level)
            if armed and hit:
                k+=1; _cur_break=k; try_chain(i,cont_dir,'Cont',name); armed=False
            elif (not armed) and ((cl[i]<level-BUF) if bull else (cl[i]>level+BUF)):
                armed=True

# ---- v11: poziom plynnosci umiera po SWEEPIE (nie po czasie), backstop CAP_DAYS ----
def _cap_a1(form_t):
    a0=dayidx_for(form_t)
    cap_date=days[min(dayi[dates[a0]]+CAP_DAYS,len(days)-1)]
    return a0, min(day_last_idx[cap_date]+1, n)

def _armed_hit_positions(hit_pos, rearm_pos):
    # hit i rearm wykluczaja sie na danym barze -> prosty merge dwoma wskaznikami
    res=[]; armed=True; hp=hit_pos.tolist(); rp=rearm_pos.tolist()
    ih=ir=0; Hn=len(hp); Rn=len(rp)
    while ih<Hn or ir<Rn:
        nh=hp[ih] if ih<Hn else float('inf')
        nr=rp[ir] if ir<Rn else float('inf')
        if nh<nr:
            if armed: res.append(nh); armed=False
            ih+=1
        else:
            if not armed: armed=True
            ir+=1
    return res

def run_level_liq(level,form_t,name,rev_dir,cont_dir):
    """v11: high -> rev SHORT / cont LONG ; low -> rev LONG / cont SHORT.
       MODE='sweep'   -> poziom umiera na 1. przebiciu (knot/body).
       MODE='confirm' -> v10 multi-break (re-arm po BUF), poziom umiera po 1. emisji.
       Oba: backstop CAP_DAYS jesli nigdy nie zebrany."""
    global _cur_break
    a0,a1=_cap_a1(form_t)
    if a1<=a0: return
    base=np.arange(a0,a1); win=base[T[a0:a1]>form_t]
    if win.size==0: return
    is_high=(rev_dir=='SHORT')
    hiw,low_,clw=hi[win],lo[win],cl[win]

    if MODE=='sweep':
        through=(hiw>=level) if is_high else (low_<=level)
        if not through.any(): return
        i=int(win[int(np.argmax(through))])
        _cur_break=1
        if rev_dir: try_chain(i,rev_dir,'Reversal',name)
        if cont_dir:
            closed=(cl[i]>level) if is_high else (cl[i]<level)
            if closed: try_chain(i,cont_dir,'Cont',name)
        return

    # CONFIRM
    events=[]
    if rev_dir:
        rev_hit=np.flatnonzero((hiw>=level) if is_high else (low_<=level))
        rev_re =np.flatnonzero((hiw<level-BUF) if is_high else (low_>level+BUF))
        for q in _armed_hit_positions(rev_hit,rev_re): events.append((q,'R'))
    if cont_dir:
        cont_hit=np.flatnonzero((clw>level) if is_high else (clw<level))
        cont_re =np.flatnonzero((clw<level-BUF) if is_high else (clw>level+BUF))
        for q in _armed_hit_positions(cont_hit,cont_re): events.append((q,'C'))
    events.sort()
    k=0
    for q,typ in events:
        k+=1; _cur_break=k; i=int(win[q]); before=len(out)
        if typ=='R': try_chain(i,rev_dir,'Reversal',name)
        else:        try_chain(i,cont_dir,'Cont',name)
        if len(out)>before: return   # umiera po pierwszej emisji

def run_gap(zlo,zhi,form_t,end_t,name):
    """v10 (GAP, CZAS): KAZDY tap strefy (re-arm po wyjsciu). NDOG/NWOG/VI (poza zakresem v11)."""
    global _cur_break
    a0=dayidx_for(form_t); a1=min(dayidx_for(end_t)+1,n)
    win=[i for i in range(a0,a1) if T[i]>form_t]
    if not win: return
    mid=(zlo+zhi)/2; armed=False; k=0
    for i in win:
        inzone = lo[i]<=zhi and hi[i]>=zlo
        if armed and inzone:
            k+=1; _cur_break=k
            from_below = cl[i-1] < mid
            if from_below:
                try_chain(i,'SHORT','Reversal',name); try_chain(i,'LONG','Cont',name)
            else:
                try_chain(i,'LONG','Reversal',name);  try_chain(i,'SHORT','Cont',name)
            armed=False
        elif not inzone:
            armed=True

# ---- F.P.FVG (strefa, v10/CZAS) : reversal oba kierunki + cont oba kierunki ----
for d in days:
    if d not in fpfvg: continue
    a,b,form=fpfvg[d]; ft=T[form]; et=T[day_last_idx[d]]
    run_level(a,ft,et,'F.P.FVG','LONG',None)
    run_level(b,ft,et,'F.P.FVG','SHORT',None)
    run_level(b,ft,et,'F.P.FVG',None,'LONG')
    run_level(a,ft,et,'F.P.FVG',None,'SHORT')

# ---- H/L sesji (v11/SWEEP) : low->rev LONG / cont SHORT ; high->rev SHORT / cont LONG ----
SH={'ASIA':('AH','AL'),'LO':('LH','LL'),'NYAM':('NYAMH','NYAML'),'NYL':('NYLH','NYLL'),'NYPM':('NYPMH','NYPML')}
for sname,s0,eidx,Hh,Ll in sessinst:
    if sname not in SH: continue
    hn,ln=SH[sname]; ft=T[eidx]
    run_level_liq(Hh,ft,hn,'SHORT','LONG')   # high sesji
    run_level_liq(Ll,ft,ln,'LONG','SHORT')   # low sesji

# ---- PDH/PDL (v11/SWEEP) ----
for _di in range(1,len(days)):
    d=days[_di]; pdh,pdl=day_hl[days[_di-1]]
    if d not in day_first_idx: continue
    ft=int(T[day_first_idx[d]])-1
    run_level_liq(pdh,ft,'PDH','SHORT','LONG')
    run_level_liq(pdl,ft,'PDL','LONG','SHORT')

# ---- PWH/PWL (v11/SWEEP, poprzedni tydzien ISO) ----
_iso=df.dt.dt.isocalendar()
_wk=list(zip(_iso.year.values,_iso.week.values))
_weeks=OrderedDict()
for _i,_k in enumerate(_wk):
    if _k not in _weeks: _weeks[_k]=[_i,_i,float(hi[_i]),float(lo[_i])]
    else:
        _w=_weeks[_k]; _w[1]=_i
        if hi[_i]>_w[2]: _w[2]=float(hi[_i])
        if lo[_i]<_w[3]: _w[3]=float(lo[_i])
_wkeys=list(_weeks.keys())
for _wi in range(1,len(_wkeys)):
    pwh=_weeks[_wkeys[_wi-1]][2]; pwl=_weeks[_wkeys[_wi-1]][3]
    _s0=_weeks[_wkeys[_wi]][0]
    ft=int(T[_s0])-1
    run_level_liq(pwh,ft,'PWH','SHORT','LONG')
    run_level_liq(pwl,ft,'PWL','LONG','SHORT')

# ---- NDOG/NWOG (v10/GAP) : trigger = powrot do poziomu ----
for pr,ta,nm,fd,md,ct in gaplev:
    et=T[day_last_idx[days[min(dayi[fd]+md,len(days)-1)]]]
    et=min(et, ct if ct!=float('inf') else et)
    run_gap(pr,pr,ta,et,nm)

# ---- BSL/SSL H1 (v11/SWEEP) : BSL high-> rev SHORT/cont LONG ; SSL low-> rev LONG/cont SHORT ----
for P,t0 in eqH:
    run_level_liq(P,t0,'BSL H1','SHORT','LONG')
for P,t0 in eqL:
    run_level_liq(P,t0,'SSL H1','LONG','SHORT')

# ---- VOLUME IMBALANCE (v10/GAP, 2 dni) : trigger = powrot do strefy VI ----
for a,b,bar,bull,mag in vis:
    et=T[day_last_idx[days[min(dayi[dates[bar]]+2,len(days)-1)]]]
    run_gap(a,b,T[bar],et,'VI')

# ============ DEDUP + FILTR CUTOFF ============
seen=set(); ded=[]
for x in sorted(out,key=lambda z:z['emit_bar']):
    key=(x['model'],x['cat'],x['dir'],x['emit_bar']//30)
    if key in seen: continue
    seen.add(key); ded.append(x)
_cut=os.environ.get('CUTOFF','2026-05-17')   # pusty => bez filtra (tryb agenta/backtest)
if _cut:
    cut=pd.Timestamp(_cut,tz='Etc/GMT+4')
    finals=[x for x in ded if df.dt[x['emit_bar']]>=cut]
else:
    finals=list(ded)
finals=sorted(finals,key=lambda z:z['emit_bar'])
pickle.dump(finals,open(os.environ.get('OUT_PKL','/home/claude/det_new.pkl'),'wb'))
if _DBG:
    import json as _json
    _json.dump(_TRC, open(os.environ.get('TRACE_OUT','/home/claude/trace.json'),'w'))
print('MODE:',MODE,'| CAP_DAYS:',CAP_DAYS)
print('CALOSC:',len(ded),'| Model:',dict(Counter(x['model'] for x in ded)))
print('wynik (po filtrze):',len(finals),'| Model:',dict(Counter(x['model'] for x in finals)))
print('po katalizatorze:',dict(Counter(x['cat'] for x in finals)))
