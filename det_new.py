import pandas as pd, numpy as np, pickle, datetime as dt

# ============ LOADER ============
import os
df=pd.read_csv(os.environ.get('DATA_CSV','/mnt/user-data/uploads/MNQ_databento_1m.csv'))
ts=pd.to_datetime(df.ts_event,utc=True).dt.as_unit('ns')   # pandas3 = us -> wymus ns (T=//1e9)
df=df.assign(ts=ts).sort_values('ts').reset_index(drop=True); ts=df.ts
df['dt']=ts.dt.tz_convert('America/New_York')
o,hi,lo,cl=df.open.values,df.high.values,df.low.values,df.close.values
T=(ts.astype('int64')//10**9).values
H=df.dt.dt.hour.values; Mi=df.dt.dt.minute.values
df['date']=df.dt.dt.date.values; dates=df.date.values; n=len(df); mins=H*60+Mi
days=sorted(df.date.unique()); dayi={d:i for i,d in enumerate(days)}

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
# F.P.FVG : pierwszy FVG sesji NYAM danego dnia
fpfvg={}
for d in days:
    ix=list(df.index[(df.date==d)&(S=='NYAM')])
    for kk in range(2,len(ix)):
        k,k2=ix[kk],ix[kk-2]
        if lo[k]>hi[k2]: fpfvg[d]=(hi[k2],lo[k],k); break
        if hi[k]<lo[k2]: fpfvg[d]=(hi[k],lo[k2],k); break
# PDH/PDL
day_hl={d:(hi[df.index[df.date==d]].max(),lo[df.index[df.date==d]].min()) for d in days}
# NDOG/NWOG -> JEDNA jednostka, poziom = .c (close), 2 / 5 dni, uniewaznienie przy przejsciu
gaplev=[]
for d in days:
    bef=df[(df.date==d)&(H<17)]; aft=df[(df.date==d)&(H>=18)]
    if len(bef) and len(aft) and pd.Timestamp(d).weekday()<4:
        c17=cl[bef.index[-1]]; ta=T[aft.index[0]]
        gaplev.append([c17,ta,'NDOG',d,2])
    if pd.Timestamp(d).weekday()==6:
        sun=df[(df.date==d)&(H>=18)]; frd=[x for x in days if pd.Timestamp(x).weekday()==4 and x<d]
        if len(sun) and frd:
            fri=df[(df.date==frd[-1])&(H<17)]
            if len(fri):
                fc=cl[fri.index[-1]]; ta=T[sun.index[0]]
                gaplev.append([fc,ta,'NWOG',d,5])
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
MAXIMP=3          # max swiec impulsu
RETWIN=20         # okno na retrace do 50% FVG
BOSWIN=30         # okno na BOS po odbiciu
BUF=3.
VIMIN=10.         # min luka body-to-body, by liczyc VI jako katalizator
VIBIG=50.         # min VI, by dzialal jako magnes (TP/bias)
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
    """od bara triggera t szukaj impulsu 1-3 swiec: break struktury + zostawia FVG + sila."""
    bull = dr=='LONG'
    for u in range(t+1,min(t+1+DISPWIN,n)):
        for L in range(1,MAXIMP+1):
            s=u-L+1
            if s<=t: continue
            same = all((cl[x]>o[x]) if bull else (cl[x]<o[x]) for x in range(s,u+1))
            if not same: continue
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
            return dict(s=s,u=u,L=L,body=round(body,1),fvg=(f[0],f[1]),fvg_bar=f[2],
                        swlo=swlo,swhi=swhi,atr5=round(atr5,1))
    return None

def confirm_chain(disp,dr):
    """retrace odbija od 50% (CE) FVG displacementu -> BOS w kierunku = potwierdzenie."""
    bull=dr=='LONG'; fl,fh=disp['fvg']; ce=round((fl+fh)/2,1); u=disp['u']
    # 1) retrace do CE
    bounce=None
    for r in range(u+1,min(u+1+RETWIN,n)):
        if bull and lo[r]<=ce+TOL: bounce=r;break
        if (not bull) and hi[r]>=ce-TOL: bounce=r;break
        # jesli zanim dojdzie do CE zrobi nowy ekstrem -> idzie dalej bez retrace
    if bounce is None: return None
    # odbicie nie moze przebic dalekiej krawedzi FVG (LONG: nie zamknac pod fl)
    if bull and cl[bounce]<fl-TOL: return None
    if (not bull) and cl[bounce]>fh+TOL: return None
    # 2) BOS: po odbiciu przelam ekstrem displacementu w kierunku
    ext=disp['swhi'] if bull else disp['swlo']
    bos=None
    for j in range(bounce+1,min(bounce+1+BOSWIN,n)):
        if bull and hi[j]>ext: bos=j;break
        if (not bull) and lo[j]<ext: bos=j;break
    if bos is None: return None
    return dict(ce=ce,bounce=bounce,bos=bos)

def liq_above(t,px):
    c=[v[3] for v in sessinst if v[3]>px+5 and v[2]<t]
    di=dayi[dates[t]]
    if di>0: c.append(day_hl[days[di-1]][0])
    up,_=vi_draw(t)
    if up is not None: c.append(up)        # duzy VI jako magnes-cel
    c=[x for x in c if x>px+5]; return round(min(c),1) if c else None
def liq_below(t,px):
    c=[v[4] for v in sessinst if v[4]<px-5 and v[2]<t]
    di=dayi[dates[t]]
    if di>0: c.append(day_hl[days[di-1]][1])
    _,dn=vi_draw(t)
    if dn is not None: c.append(dn)        # duzy VI jako magnes-cel
    c=[x for x in c if x<px-5]; return round(max(c),1) if c else None

# ============ BIAS (v0, FLAGA nie filtr) ============
dd=df.set_index(ts).resample('1D').agg(h=('high','max'),l=('low','min'),c=('close','last')).dropna()
dD=dd.index.tz_convert('America/New_York').date
dH,dL,dC=dd.h.values,dd.l.values,dd.c.values
def bias_for(t):
    d=dates[t]; di=dayi[d]
    # zakres tradingowy = ostatnie 5 dni
    j=np.searchsorted(dD,d)
    if j<5: return ('niejasny','-')
    rngH=dH[j-5:j].max(); rngL=dL[j-5:j].min(); eq=(rngH+rngL)/2
    px=cl[t]
    pd_=  'discount' if px<eq else 'premium'
    # struktura D1: HH/HL vs LH/LL z ostatnich 3 dni
    up = dH[j-1]>dH[j-3] and dL[j-1]>dL[j-3]
    dn = dH[j-1]<dH[j-3] and dL[j-1]<dL[j-3]
    if pd_=='discount' and up: b='LONG'
    elif pd_=='premium' and dn: b='SHORT'
    elif pd_=='discount' and not dn: b='LONG?'
    elif pd_=='premium' and not up: b='SHORT?'
    else: b='niejasny'
    # duzy niedomkniety VI = magnes/draw: wzmacnia lub rozstrzyga bias
    vu,vd=vi_draw(t)
    if b=='niejasny':
        if vu and not vd: b='LONG?'
        elif vd and not vu: b='SHORT?'
    elif b=='LONG?' and vu and not vd: b='LONG'
    elif b=='SHORT?' and vd and not vu: b='SHORT'
    return (b,pd_)

# ============ EMISJA SETUPU ============
out=[]
def emit(t,model,name,dr,disp,conf):
    bull=dr=='LONG'; fl,fh=disp['fvg']; ce=conf['ce']
    leg_hi,leg_lo=disp['swhi'],disp['swlo']; rng=leg_hi-leg_lo
    ote62=round(leg_hi-0.62*rng,1) if bull else round(leg_lo+0.62*rng,1)
    ote79=round(leg_hi-0.79*rng,1) if bull else round(leg_lo+0.79*rng,1)
    entry=ce                                   # glowne wejscie = FVG CE
    if bull: sl=round(disp['swlo']-BUF,1); tp=liq_above(conf['bos'],entry)
    else:    sl=round(disp['swhi']+BUF,1); tp=liq_below(conf['bos'],entry)
    trail=[(x[0],x[1],x[2]) for x in fvgs(conf['bos'],min(conf['bos']+40,n),bull)]  # trailing FVG = info (lo,hi,bar)
    b,pdv=bias_for(conf['bos']); align='Y' if b.replace('?','')==dr else ('?' if '?' in b or b=='niejasny' else 'N')
    out.append(dict(date=str(dates[conf['bos']]),model=model,cat=name,dir=dr,
        trig=df.dt[t].strftime('%H:%M'),disp_end=df.dt[disp['u']].strftime('%H:%M'),
        bounce=df.dt[conf['bounce']].strftime('%H:%M'),bos=df.dt[conf['bos']].strftime('%H:%M'),
        entry=entry,ote62=ote62,ote79=ote79,SL=sl,TP=tp,
        fvg_lo=round(fl,1),fvg_hi=round(fh,1),
        bias=b,bias_align=align,trail=trail,
        emit_bar=int(conf['bos']),entry_bar=int(conf['bounce']),
        entry_ms=int(df.dt[conf['bounce']].timestamp()*1000),
        bos_ms=int(df.dt[conf['bos']].timestamp()*1000)))

def try_chain(trigger,dr,model,name):
    """pierwszy POTWIERDZONY displacement po triggerze (jak pierwszy nie potwierdzi, sprobuj nastepny)."""
    cur=trigger
    for _ in range(3):
        d=find_displacement(cur,dr)
        if d is None: return
        c=confirm_chain(d,dr)
        if c: emit(trigger,model,name,dr,d,c); return
        cur=d['u']

def run_level(level,form_t,end_t,name,rev_dir,cont_dir):
    """KATALIZATOR-PULA (F.P.FVG, H/L sesji, BSL/SSL): pierwsza interakcja. rev=sweep(wick), cont=body-break(close)."""
    a0=dayidx_for(form_t); a1=min(dayidx_for(end_t)+1,n)
    win=[i for i in range(a0,a1) if T[i]>form_t]
    if not win: return
    if rev_dir:
        bull=rev_dir=='LONG'
        for i in win:
            if (lo[i]<=level) if bull else (hi[i]>=level):
                try_chain(i,rev_dir,'Reversal',name); break
    if cont_dir:
        bull=cont_dir=='LONG'
        for i in win:
            if (cl[i]>level) if bull else (cl[i]<level):
                try_chain(i,cont_dir,'Cont',name); break

def run_gap(zlo,zhi,form_t,end_t,name):
    """KATALIZATOR-GAP (NDOG/NWOG/VI): cena odskoczyla, trigger = POWROT do strefy (tap), kierunek wg podejscia."""
    a0=dayidx_for(form_t); a1=min(dayidx_for(end_t)+1,n)
    win=[i for i in range(a0,a1) if T[i]>form_t]
    if not win: return
    touch=None
    for i in win:
        if lo[i]<=zhi and hi[i]>=zlo: touch=i; break
    if touch is None or touch==win[0]: return
    mid=(zlo+zhi)/2; from_below = cl[touch-1] < mid
    if from_below:
        try_chain(touch,'SHORT','Reversal',name); try_chain(touch,'LONG','Cont',name)
    else:
        try_chain(touch,'LONG','Reversal',name);  try_chain(touch,'SHORT','Cont',name)

# ---- F.P.FVG (strefa) : reversal oba kierunki + cont oba kierunki ----
for d in days:
    if d not in fpfvg: continue
    a,b,form=fpfvg[d]; ft=T[form]; et=T[df.index[df.date==d][-1]]
    run_level(a,ft,et,'F.P.FVG','LONG',None)   # tap dolnej krawedzi -> rev long
    run_level(b,ft,et,'F.P.FVG','SHORT',None)  # tap gornej krawedzi -> rev short
    run_level(b,ft,et,'F.P.FVG',None,'LONG')   # close nad FVG -> cont long
    run_level(a,ft,et,'F.P.FVG',None,'SHORT')  # close pod FVG -> cont short

# ---- H/L sesji : low->rev LONG / cont SHORT ; high->rev SHORT / cont LONG ----
SH={'ASIA':('AH','AL'),'LO':('LH','LL'),'NYL':('NYLH','NYLL'),'NYPM':('NYPMH','NYPML')}
for sname,s0,eidx,Hh,Ll in sessinst:
    if sname not in SH: continue
    hn,ln=SH[sname]; ft=T[eidx]
    V=1 if sname in ('NYPM','ASIA') else 0
    di=dayi[dates[eidx]]; endd=days[min(di+V,len(days)-1)]; et=T[df.index[df.date==endd][-1]]
    run_level(Hh,ft,et,hn,'SHORT','LONG')   # high sesji
    run_level(Ll,ft,et,ln,'LONG','SHORT')   # low sesji

# ---- NDOG/NWOG (.c, jedna jednostka, GAP) : trigger = powrot do poziomu ----
for pr,ta,nm,fd,md,ct in gaplev:
    et=T[df.index[df.date==days[min(dayi[fd]+md,len(days)-1)]][-1]]
    et=min(et, ct if ct!=float('inf') else et)
    run_gap(pr,pr,ta,et,nm)

# ---- BSL/SSL H1 (2 dni) : BSL high-> rev SHORT/cont LONG ; SSL low-> rev LONG/cont SHORT ----
for P,t0 in eqH:
    sb=dayidx_for(t0); et=T[df.index[df.date==days[min(dayi[dates[sb]]+2,len(days)-1)]][-1]]
    run_level(P,t0,et,'BSL H1','SHORT','LONG')
for P,t0 in eqL:
    sb=dayidx_for(t0); et=T[df.index[df.date==days[min(dayi[dates[sb]]+2,len(days)-1)]][-1]]
    run_level(P,t0,et,'SSL H1','LONG','SHORT')

# ---- VOLUME IMBALANCE (katalizator-GAP, 2 dni) : trigger = powrot do strefy VI ----
for a,b,bar,bull,mag in vis:
    et=T[df.index[df.date==days[min(dayi[dates[bar]]+2,len(days)-1)]][-1]]
    run_gap(a,b,T[bar],et,'VI')

# ============ DEDUP + FILTR 17.05+ ============
from collections import Counter,defaultdict
seen=set(); ded=[]
for x in sorted(out,key=lambda z:z['emit_bar']):
    key=(x['model'],x['cat'],x['dir'],x['emit_bar']//30)
    if key in seen: continue
    seen.add(key); ded.append(x)
import os as _os
_cut=_os.environ.get('CUTOFF','2026-05-17')   # pusty => bez filtra (tryb agenta)
if _cut:
    cut=pd.Timestamp(_cut,tz='America/New_York')
    finals=[x for x in ded if df.dt[x['emit_bar']]>=cut]
else:
    finals=list(ded)
finals=sorted(finals,key=lambda z:z['emit_bar'])
pickle.dump(finals,open(_os.environ.get('OUT_PKL','/home/claude/det_new.pkl'),'wb'))
print('CALOSC:',len(ded),'| Model:',dict(Counter(x['model'] for x in ded)))
print('wynik (po filtrze):',len(finals),'| Model:',dict(Counter(x['model'] for x in finals)))
print('po katalizatorze:',dict(Counter(x['cat'] for x in finals)))
