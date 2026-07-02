#!/usr/bin/env python3
"""
model_c_live.py — LIVE signal generator for STRATEGY C (staircase displacement → A/B rejection+BOS).

SAFE BY DESIGN — does NOT touch the live A/B agent (built exactly like strategy_f_live.py):
  * Runs as its OWN process. Imports detcore + live_emit READ-ONLY.
  * Own dedup file (SENT_C_FILE), own Telegram webhook (STRAT_C_WEBHOOK), own TradersPost (EXEC_WEBHOOK_C).
  * Additive: any C setup within ±3 bars of an A/B confirmation is dropped (never double-signals A/B).
  * With STRAT_C_ENABLED unset it does nothing.

WHAT IT DOES each poll (1-min cron or --loop):
  1. Read the agent's bar buffer (STRAT_C_BUF / BUF), build a detcore ctx.
  2. Detect C setups: staircase displacement → (AFTER it completes) retrace into an FVG, body holds CE,
     BOS = close beyond the displacement extreme → v10 limit entry. CAUSAL (entry always after disp end).
  3. Build the order: LIMIT at the v10 entry, SL = FVG CE, TP = 2R. (BE@1R optional via C_NOBE=0.)
  4. INVALIDATION: 1-min body close back through the CE before fill → CANCEL.
  5. Alert (distinct wording) + optionally stage a TradersPost bracket (same schema as agent._exec_order).

ENV (nothing fires unless STRAT_C_ENABLED=1 and a webhook is set):
  STRAT_C_ENABLED=1
  STRAT_C_BUF or BUF            path to the agent bar buffer CSV (read-only)
  STRAT_C_WEBHOOK or WEBHOOK_URL  Telegram /webhook for C alerts
  EXEC_WEBHOOK_C               TradersPost relay for C (own strategy recommended; = EXEC_WEBHOOK to share)
  EXEC_TICKER_C (def EXEC_TICKER/CONTRACT/'MNQ1!') · EXEC_MAX_QTY_C · PRICE_OFFSET
  SENT_C_FILE (def /home/claude/sent_signals_C.json) · C_TRADES_FILE
  STRAT_C_FRESH_MIN (30) · C_FILL_MIN (30) · C_NOBE (0 = BE@1R like eval; 1 = no-BE)
  Detector knobs (frozen v3 defaults): C_MINRUN_ATR=4 C_MAXRUN_ATR=14 C_MINLEN=8 C_MINDOM=0.70
                                       C_WIN=25 C_RETR=0.5 C_DEPTH_MIN=0 C_SESSIONS=NYAM,PREM C_SKIP_COUNTERBIAS=1
  C_TOUCH_TOL=0  FVG-touch tolerance in POINTS (0=exact=current; ~2 = zone-tolerant, tested). Loosens ONLY the
                 rejection wick (price may miss the FVG edge by <=tol); body-CE + BOS unchanged. See README_TOUCH_TOL.md.
"""
import os, sys, json, time, csv, threading, datetime as dt
from zoneinfo import ZoneInfo
HERE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,HERE)
from detcore.config import Config
from detcore.pipeline import detect
from detcore.entries import get_entry_v10
from detcore.confirmation import bias_for, find_setup_v10
from detcore.primitives import fvgs
import live_emit
try: import requests
except Exception: requests=None
ET=ZoneInfo('America/New_York')

BUF        = os.environ.get('STRAT_C_BUF') or os.environ.get('BUF') or os.path.join(HERE,'buffer.csv')
BUF_URL    = os.environ.get('STRAT_C_BUF_URL','')          # if set → pull bars over HTTP from the agent (/archive). No shared volume; A/B untouched.
BUFFER_BARS= int(os.environ.get('BUFFER_BARS','14000'))    # cap the cached buffer (~10 trading days)
WEBHOOK_C  = os.environ.get('STRAT_C_WEBHOOK') or os.environ.get('WEBHOOK_URL','')
EXEC_C     = os.environ.get('EXEC_WEBHOOK_C','')
SENT_C     = os.environ.get('SENT_C_FILE','/home/claude/sent_signals_C.json')
C_TRADES   = os.environ.get('C_TRADES_FILE') or os.path.join(os.path.dirname(SENT_C) or '.','c_trades.json')
C_CAND     = os.environ.get('C_CAND_FILE')   or os.path.join(os.path.dirname(SENT_C) or '.','c_candidates.json')  # pipeline/step log
FRESH_MIN  = int(os.environ.get('STRAT_C_FRESH_MIN','30'))
FILL_MIN   = int(os.environ.get('C_FILL_MIN','30'))
OFFSET     = float(os.environ.get('PRICE_OFFSET','0'))
NOBE       = os.environ.get('C_NOBE','0')=='1'
ENABLED    = os.environ.get('STRAT_C_ENABLED','')=='1'
TEST       = os.environ.get('C_TEST','')=='1'   # SILENT test/demo: detect+log+journal+perf but send NOTHING (no Telegram, no TradersPost)
# frozen v3 detector params
WIN=int(os.environ.get('C_WIN','25')); MINLEN=int(os.environ.get('C_MINLEN','8')); RETR=float(os.environ.get('C_RETR','0.5'))
MINRUN=float(os.environ.get('C_MINRUN_ATR','4.0')); MAXRUN=float(os.environ.get('C_MAXRUN_ATR','14.0'))
MINDOM=float(os.environ.get('C_MINDOM','0.70')); DEPTH_MIN=float(os.environ.get('C_DEPTH_MIN','0.0')); LOOKBACK=20
SESSIONS=tuple(s for s in os.environ.get('C_SESSIONS','NYAM,PREM').split(',') if s)
SKIP_CB=os.environ.get('C_SKIP_COUNTERBIAS','1')=='1'
TOUCH_TOL=float(os.environ.get('C_TOUCH_TOL','0'))   # FVG-touch tolerance in POINTS (0=exact=current; ~2 tested best). FVG=zone: price may miss the gap edge by <=tol & still be a valid rejection.
RR=2.0; MAXR=40.0

def key_c(x): return f"C|{x['date']}|{x['dir']}|{x['bos']}|{round(x['entry'],2)}"   # own namespace -> never merges with A/B

def _long_disp(ctx,s,dr):
    o,hi,lo,cl,ATR,n=ctx.o,ctx.hi,ctx.lo,ctx.cl,ctx.ATR,ctx.n; bull=dr=='LONG'
    if not ((cl[s]>=o[s]) if bull else (cl[s]<=o[s])): return None
    base=float(lo[s]) if bull else float(hi[s]); ext=float(hi[s]) if bull else float(lo[s]); eb=s; j=s
    while j+1<min(s+WIN,n):
        j+=1
        if bull:
            if hi[j]>ext: ext=float(hi[j]); eb=j
            run=ext-base
            if run>0 and j>eb and (ext-lo[j])>RETR*run: break
            if cl[j]<base: break
        else:
            if lo[j]<ext: ext=float(lo[j]); eb=j
            run=base-ext
            if run>0 and j>eb and (hi[j]-ext)>RETR*run: break
            if cl[j]>base: break
    u=eb
    if (u-s+1)<MINLEN: return None
    run=(ext-base) if bull else (base-ext)
    if run<=0: return None
    atr5=float(ATR[u]) if ATR[u]>0 else 1e9
    if not (MINRUN*atr5<=run<=MAXRUN*atr5): return None
    dom=sum(1 for x in range(s,u+1) if ((cl[x]>=o[x]) if bull else (cl[x]<=o[x])))
    if dom/(u-s+1)<MINDOM: return None
    prior=max(hi[max(0,s-LOOKBACK):s]) if bull else min(lo[max(0,s-LOOKBACK):s])
    if not ((ext>prior) if bull else (ext<prior)): return None
    fl=fvgs(ctx,s,u+2,bull)
    if len(fl)<2: return None
    return dict(s=s,u=u,swlo=base if bull else ext,swhi=ext if bull else base,run=round(run,1),atr5=atr5,fvgs=fl)

def find_setup_tol(ctx,disp,dr,tol):
    """A/B's find_setup_v10 with a TOUCH tolerance (points) on the rejection wick ONLY.
    tol<=0 -> delegates to find_setup_v10 (byte-identical, current behaviour).
    tol>0  -> price may miss the FVG near-edge by <=tol and still count as a valid touch
              (FVG = zone, not an exact level). Body-hold-CE and the BOS test are UNCHANGED.
    Returns the same dict shape as find_setup_v10 so get_entry_v10 consumes it unchanged."""
    if tol<=0: return find_setup_v10(ctx,disp,dr)
    hi,lo,cl,n=ctx.hi,ctx.lo,ctx.cl,ctx.n; RETWIN=ctx.cfg.retwin; BOSWIN=ctx.cfg.boswin
    bull=dr=='LONG'; fl,fh=disp['fvg']; ce=round((fl+fh)/2,2); fb=disp['fvg_bar']
    origin=None; ob=None; broke=None
    for j in range(fb+1,min(fb+1+RETWIN,n)):
        if (cl[j]>ce) if not bull else (cl[j]<ce): broke=j; break
        wick=(hi[j]>=fl-tol) if not bull else (lo[j]<=fh+tol)   # <-- ONLY change vs find_rejection_v10
        body=(cl[j]<=ce) if not bull else (cl[j]>=ce)
        if wick and body:
            ext=hi[j] if not bull else lo[j]
            if origin is None or (ext>origin if not bull else ext<origin): origin,ob=ext,j
    if broke is not None or origin is None: return None
    s=disp['s']; u=disp['u']
    struct0=float(max(hi[s:ob])) if bull else float(min(lo[s:ob])); level=struct0
    for j in range(ob+1,min(ob+1+BOSWIN,n)):
        if (cl[j]>ce) if not bull else (cl[j]<ce): return None
        if (cl[j]>level) if bull else (cl[j]<level):
            end=float(max(hi[ob:j+1])) if bull else float(min(lo[ob:j+1]))
            return dict(dr=dr,origin=round(origin,2),origin_bar=ob,end=round(end,2),bos_bar=j,ce=ce,
                        fvg=disp['fvg'],fvg_bar=disp['fvg_bar'],s=s,u=u)
        level=max(level,hi[j]) if bull else min(level,lo[j])
    return None

def c_signals(buf=BUF):
    """Fresh Strategy-C setups on the buffer (causal; additive to A/B)."""
    cfg=Config(disp_mode='chain',dispwin=30,minimp=3,cutoff='',data_csv=buf,max_stop_r=40.0)
    finals,ded,ctx=detect(cfg)
    ab=set((f['dir'],int(f['entry_bar'])) for f in finals)
    o,hi,lo,cl,n,S,ATR,dtc,dates=ctx.o,ctx.hi,ctx.lo,ctx.cl,ctx.n,ctx.S,ctx.ATR,ctx.df.dt,ctx.dates
    last_ms=int(dtc.iloc[n-1].timestamp()*1000)
    drives=[]; seen=set()
    for s in range(1,n-MINLEN):
        if S[s] not in SESSIONS: continue
        for dr in ('LONG','SHORT'):
            d=_long_disp(ctx,s,dr)
            if d is None: continue
            k=(dr,d['u']//20)
            if k in seen: continue
            seen.add(k); d['dir']=dr; drives.append(d)
    out=[]; seenk=set()
    for d in drives:
        dr=d['dir']; bull=dr=='LONG'; leg=d['swhi']-d['swlo']
        if leg<=0: continue
        for f in d['fvgs']:
            fmid=(f[0]+f[1])/2; depth=(d['swhi']-fmid)/leg if bull else (fmid-d['swlo'])/leg
            if depth<DEPTH_MIN: continue
            disp_c=dict(fvg=(float(f[0]),float(f[1])),fvg_bar=max(int(f[2]),int(d['u'])),s=int(d['s']),u=int(d['u']))  # CAUSAL
            try: su=find_setup_tol(ctx,disp_c,dr,TOUCH_TOL)   # C_TOUCH_TOL=0 -> identical to find_setup_v10
            except Exception: su=None
            if su is None: continue
            try: e=get_entry_v10(ctx,su)
            except Exception: e=None
            if e is None: continue
            eb=int(e['start_bar'])
            if any((dr,db) in ab for db in range(eb-3,eb+4)): continue     # additive
            try: b,_=bias_for(ctx,int(su['bos_bar']))
            except Exception: b='niejasny'
            align='Y' if b.replace('?','')==dr else ('?' if ('?' in b or b=='niejasny') else 'N')
            if SKIP_CB and align=='N': continue
            entry=round(float(e['entry']),2); sl=round(float(e['sl']),2); risk=abs(entry-sl)
            if not (0<risk<=MAXR): continue
            tp=round(entry+RR*risk if bull else entry-RR*risk,2)
            bos_ts=dtc.iloc[int(su['bos_bar'])]; bos_ms=int(bos_ts.timestamp()*1000)
            if (last_ms-bos_ms) > FRESH_MIN*60*1000: continue              # only FRESH confirmations
            atr=ATR[d['u']] if ATR[d['u']]>0 else 1e9
            k="C|%s|%.2f|%s"%(dr,entry,bos_ts.strftime('%Y%m%dT%H%M'))
            if k in seenk: continue
            seenk.add(k)
            out.append(dict(date=str(dates[int(su['bos_bar'])]),dir=dr,sess=str(S[eb]),entry=entry,SL=sl,TP=tp,
                risk=round(risk,2),depth=round(depth,2),disp_pts=round(float(d['run']),1),run_atr=round(float(d['run'])/atr,2),
                fvg_lo=round(float(f[0]),2),fvg_hi=round(float(f[1]),2),ce=round(fmid,2),bias=b,bias_align=align,
                bos=bos_ts.strftime('%H:%M'),bos_ms=bos_ms,disp_end=dtc.iloc[int(d['u'])].strftime('%H:%M'),
                disp_end_ms=int(dtc.iloc[int(d['u'])].timestamp()*1000),status='live',strategy='C'))
    return out

def to_alert_c(x):
    isL=x['dir']=='LONG'; side='BUY' if isL else 'SELL'; emoji='🟢' if isL else '🔴'; rp=round(x['risk'],1)
    be_line = "" if NOBE else f" · BE po +{rp} (1R)"
    base=(f"🅲 STRATEGY C · staircase displacement (schodkowy) · {emoji} {x['dir']} · {x['sess']}"
          f"\n📋 {side} LIMIT {round(x['entry']+OFFSET,1)} — POSTAW TERAZ (fill na dotknięciu wejścia)"
          f"\n🛑 SL {round(x['SL']+OFFSET,1)} · ryzyko {rp} pkt ({rp*4:.0f} ticks){be_line}"
          f"\n🎯 TP {round(x['TP']+OFFSET,1)} · +{round(2*x['risk'],1)} pkt (2R)"
          f"\n🧩 displacement {x['disp_pts']:.0f} pkt ({x['run_atr']:.1f}×ATR) · retrace depth {x['depth']:.2f} · FVG {x['fvg_lo']}–{x['fvg_hi']}"
          f"\n📐 BOS {x['bos']} (po zakończeniu displacementu {x['disp_end']}) · bias {x['bias_align']}"
          f"\n⛔ UNIEWAŻNIENIE: świeca ZAMKNIE ciałem {'nad' if isL else 'pod'} CE {round(x['ce']+OFFSET,1)} → ANULUJ limit")
    s=live_emit.size_for(x['entry'],x['SL'])
    if s:
        qty,slpts,perc,real,pct=s; base+=f"\n📏 {qty} kontr. (SL {slpts} pkt = ${perc}/kontr · ${real} ≈ {pct}%)"
    base+="\n⚠ Strategy C — OSOBNY strumień, NIE myl z A/B ani F."
    return base

def _td_payload(x, action='enter'):
    """Mirror agent._exec_order schema EXACTLY so it works with the current TradersPost relay."""
    isL=x['dir']=='LONG'; e=float(x['entry']); sl=float(x['SL']); R=abs(e-sl); tp=(e+2*R) if isL else (e-2*R)
    _sf=live_emit.size_for(e,sl); qty=int(_sf[0]) if _sf else 1
    cap=os.environ.get('EXEC_MAX_QTY_C','').strip()
    if cap.isdigit() and int(cap)>0: qty=min(qty,int(cap))
    qty=max(1,qty)
    return {"ticker":os.environ.get('EXEC_TICKER_C',os.environ.get('EXEC_TICKER',os.environ.get('CONTRACT','MNQ1!'))),
            "action":("buy" if isL else "sell") if action=='enter' else "exit",
            "orderType":"limit","limitPrice":round(e+OFFSET,2),"quantity":qty,
            "takeProfit":{"limitPrice":round(tp+OFFSET,2)},
            "stopLoss":{"type":"stop","stopPrice":round(sl+OFFSET,2)},
            "timeInForce":"gtc","strategy":"STRATEGY_C"}

def exec_c(x, text=None, action='enter'):
    if not EXEC_C or requests is None: return 'no-exec'
    p=_td_payload(x,action)
    if text: p['text']=text
    try:
        r=requests.post(EXEC_C,json=p,timeout=10); print('EXEC_C',getattr(r,'status_code',None),flush=True); return 'exec'
    except Exception as ex:
        print('EXEC_C err',ex,flush=True); return f'ERR {ex}'

def _ld(p):
    try: return json.load(open(p))
    except Exception: return {}
def _sv(p,d):
    dd=os.path.dirname(p)
    if dd: os.makedirs(dd,exist_ok=True)
    json.dump(d,open(p,'w'))

# ───────────────────────── PIPELINE (candidates / step tracking) ─────────────────────────
STEP_RANK={'displacement':0,'rejection':1,'bos':2}
def net_R(res,g,risk): return (g-(1.5+(0.0 if res=='win' else 1.5)*0.5)/(risk*2.0)) if risk>0 else g

def _c_step(ctx,disp,dr,tol):
    """Furthest step reached by ONE drive-FVG: 'displacement' → 'rejection' → 'bos'. Mirrors find_setup_tol exactly."""
    hi,lo,cl,n=ctx.hi,ctx.lo,ctx.cl,ctx.n; RETWIN=ctx.cfg.retwin; BOSWIN=ctx.cfg.boswin
    bull=dr=='LONG'; fl,fh=disp['fvg']; ce=round((fl+fh)/2,2); fb=disp['fvg_bar']
    origin=ob=broke=None
    for j in range(fb+1,min(fb+1+RETWIN,n)):
        if (cl[j]>ce) if not bull else (cl[j]<ce): broke=j; break
        wick=(hi[j]>=fl-tol) if not bull else (lo[j]<=fh+tol)
        body=(cl[j]<=ce) if not bull else (cl[j]>=ce)
        if wick and body:
            ext=hi[j] if not bull else lo[j]
            if origin is None or (ext>origin if not bull else ext<origin): origin,ob=ext,j
    if ob is None:
        return ('displacement', {'note':('unieważniony: zamknięcie za CE' if broke is not None else 'czeka na retrace do FVG')})
    s=disp['s']; u=disp['u']
    struct0=float(max(hi[s:ob])) if bull else float(min(lo[s:ob])); level=struct0
    for j in range(ob+1,min(ob+1+BOSWIN,n)):
        if (cl[j]>ce) if not bull else (cl[j]<ce):
            return ('rejection', {'rej_bar':ob,'note':'unieważniony po rejection (zamknięcie za CE)'})
        if (cl[j]>level) if bull else (cl[j]<level):
            return ('bos', {'rej_bar':ob,'bos_bar':j})
        level=max(level,hi[j]) if bull else min(level,lo[j])
    return ('rejection', {'rej_bar':ob,'note':'czeka na BOS'})

def c_candidates(buf=BUF):
    """Every staircase drive with its furthest step (the pipeline of POSSIBLE trades)."""
    cfg=Config(disp_mode='chain',dispwin=30,minimp=3,cutoff='',data_csv=buf,max_stop_r=40.0)
    finals,ded,ctx=detect(cfg)
    ab=set((f['dir'],int(f['entry_bar'])) for f in finals)
    o,hi,lo,cl,n,S,ATR,dtc=ctx.o,ctx.hi,ctx.lo,ctx.cl,ctx.n,ctx.S,ctx.ATR,ctx.df.dt
    cands=[]; seen=set()
    for s in range(1,n-MINLEN):
        if S[s] not in SESSIONS: continue
        for dr in ('LONG','SHORT'):
            d=_long_disp(ctx,s,dr)
            if d is None: continue
            k=(dr,d['u']//20)
            if k in seen: continue
            seen.add(k)
            best=None
            for f in d['fvgs']:
                dc=dict(fvg=(float(f[0]),float(f[1])),fvg_bar=max(int(f[2]),int(d['u'])),s=int(d['s']),u=int(d['u']))
                step,info=_c_step(ctx,dc,dr,TOUCH_TOL)
                if best is None or STEP_RANK[step]>STEP_RANK[best[0]]: best=(step,info,dc,f)
            step,info,dc,f=best
            rec=dict(dir=dr,disp_start=dtc.iloc[int(d['s'])].strftime('%m-%d %H:%M'),disp_end=dtc.iloc[int(d['u'])].strftime('%H:%M'),
                     disp_pts=round(float(d['run']),1),fvg_lo=round(float(f[0]),2),fvg_hi=round(float(f[1]),2),
                     step=step,note=info.get('note',''))
            if step=='bos':
                try: su=find_setup_tol(ctx,dc,dr,TOUCH_TOL); e=get_entry_v10(ctx,su) if su else None
                except Exception: su=e=None
                if e is not None:
                    eb=int(e['start_bar']); entry=round(float(e['entry']),2); sl=round(float(e['sl']),2)
                    isab=any((dr,db) in ab for db in range(eb-3,eb+4))
                    filled=any(lo[i]<=entry<=hi[i] for i in range(eb,min(eb+FILL_MIN,n)))
                    rec.update(entry=entry,sl=sl,bos=dtc.iloc[int(su['bos_bar'])].strftime('%H:%M'))
                    rec['step']='dropped_ab' if isab else ('filled' if filled else 'entry_pending')
                    rec['note']='pokrywa się z A/B (pominięty)' if isab else ('limit wypełniony' if filled else 'limit czeka na fill')
                else:
                    rec['note']='BOS OK, brak wejścia v10'
            cands.append(rec)
    return cands,ctx

def cand_summary(cands):
    from collections import Counter
    c=Counter(x['step'] for x in cands)
    order=[('displacement','displacement — czeka na retrace/rejection'),('rejection','rejection OK — czeka na BOS'),
           ('bos','BOS OK — bez wejścia v10'),('entry_pending','BOS+wejście — LIMIT czeka na fill'),
           ('filled','wypełniony'),('dropped_ab','pominięty (pokrywa się z A/B)')]
    lines=[f"🅲 PIPELINE Model C (C_TOUCH_TOL={TOUCH_TOL:g}) — {len(cands)} drive(ów) w oknie bufora"]
    for st,lbl in order:
        if c.get(st): lines.append(f"  {c[st]}× {lbl}")
    live=[x for x in cands if x['step'] in ('rejection','entry_pending','filled') and 'unieważniony' not in x.get('note','')]
    if live:
        lines.append("— aktywne (mogą jeszcze odpalić):")
        for x in live[-10:]:
            ex=f" · entry {x['entry']}" if x.get('entry') else ''
            lines.append(f"  {'🟢' if x['dir']=='LONG' else '🔴'} {x['dir']} {x['disp_start']}→{x['disp_end']} · disp {x['disp_pts']:.0f}pkt · FVG {x['fvg_lo']}–{x['fvg_hi']} · [{x['step']}]{ex} · {x['note']}")
    else:
        lines.append("— brak aktywnych kandydatów (nic nie czeka na BOS/fill).")
    return "\n".join(lines)

def log_candidates(buf=BUF):
    try:
        cands,_=c_candidates(buf)
        snap=dict(ts=dt.datetime.utcnow().isoformat(timespec='seconds'),touch_tol=TOUCH_TOL,
                  counts={k:sum(1 for x in cands if x['step']==k) for k in set(x['step'] for x in cands)},cands=cands)
        _sv(C_CAND,snap); return cands
    except Exception as ex:
        print('log_candidates err',ex,flush=True); return []

# ───────────────────────── PERFORMANCE (resolve fired trades) ─────────────────────────
def resolve_perf(buf=BUF):
    """Resolve journaled C trades to win/loss/BE using later bars in the buffer; write R back; return running stats."""
    j=_ld(C_TRADES)
    if not j: return j,dict(n=0)
    cfg=Config(disp_mode='chain',dispwin=30,minimp=3,cutoff='',data_csv=buf,max_stop_r=40.0)
    _,_,ctx=detect(cfg); hi,lo,cl,dtc,n=ctx.hi,ctx.lo,ctx.cl,ctx.df.dt,ctx.n
    ts=[int(dtc.iloc[i].timestamp()*1000) for i in range(n)]
    t0=ts[0] if ts else 0
    def bar_at(ms):
        for i in range(n):
            if ts[i]>=ms: return i
        return None
    changed=False
    for k,x in j.items():
        if x.get('status') in ('win','loss','be','aged_out'): continue
        bos_ms=int(x.get('bos_ms',0))
        if bos_ms and bos_ms<t0: x['status']='aged_out'; changed=True; continue   # older than buffer -> can't resolve here
        eb=bar_at(bos_ms)
        if eb is None: continue
        entry=float(x['entry']); sl=float(x['SL']); dr=x['dir']; bull=dr=='LONG'; risk=abs(entry-sl)
        if not (0<risk<=MAXR): continue
        fb=None
        for i in range(eb,min(eb+FILL_MIN,n)):
            if lo[i]<=entry<=hi[i]: fb=i; break
        if fb is None:
            if n-eb>=FILL_MIN: x['status']='no_fill'; changed=True   # window passed, never filled
            continue
        tp=entry+2*risk if bull else entry-2*risk; be=False; oneR=entry+risk if bull else entry-risk; res=None
        for i in range(fb,min(fb+2880,n)):
            cur=entry if be else sl
            if (lo[i]<=cur) if bull else (hi[i]>=cur): res=('be' if be else 'loss'); break
            if i==fb: continue
            if (hi[i]>=tp) if bull else (lo[i]<=tp): res='win'; break
            if (not be) and ((hi[i]>=oneR) if bull else (lo[i]<=oneR)): be=True
        if res is None:
            if (n-fb)>=2880: x['status']='timeout'; changed=True   # open past MAXHOLD (48h) and buffer moved on
            continue
        g=2.0 if res=='win' else (0.0 if res=='be' else -1.0)
        x['status']=res; x['R_gross']=g; x['R']=round(net_R(res,g,risk),3); x['fill_ts']=int(ts[fb]); changed=True
    if changed: _sv(C_TRADES,j)
    done=[x for x in j.values() if x.get('status') in ('win','loss','be')]
    n_=len(done); w=sum(1 for x in done if x['status']=='win'); b=sum(1 for x in done if x['status']=='be'); l=sum(1 for x in done if x['status']=='loss')
    tot=sum(x.get('R',0) for x in done)
    return j,dict(n=n_,win=w,be=b,loss=l,winpct=(100*w/n_ if n_ else 0.0),winpct_exbe=(100*w/(w+l) if (w+l) else 0.0),
                  exp=(tot/n_ if n_ else 0.0),totR=tot,
                  pending=sum(1 for x in j.values() if x.get('status') in ('alerted',None)),
                  no_fill=sum(1 for x in j.values() if x.get('status')=='no_fill'))

def perf_summary(st):
    if st.get('n',0)==0:
        return f"🅲 PERFORMANCE Model C — 0 rozstrzygniętych tradów (pending {st.get('pending',0)}, no-fill {st.get('no_fill',0)})."
    gate='✅ ≥ +0.15R (Gate 0)' if st['exp']>=0.15 else '⏳ < +0.15R (jeszcze nie)'
    return (f"🅲 PERFORMANCE Model C — {st['n']} tradów\n"
            f"  W {st['win']} · BE {st['be']} · L {st['loss']}  |  win {st['winpct']:.0f}% (bez BE {st['winpct_exbe']:.0f}%)\n"
            f"  exp {st['exp']:+.3f}R · suma {st['totR']:+.1f}R  |  {gate}\n"
            f"  pending {st['pending']} · no-fill {st['no_fill']}")

COLS=['ts_event','open','high','low','close','volume']
def _append_bar_c(b):
    """Append ONE forwarded bar to C's own buffer (mirrors agent._append_bar), trim to BUFFER_BARS.
    This is the F-style intake: the agent POSTs each closed bar to C's /bars, C builds its own buffer."""
    ts=str(b['ts_event']).strip()
    if '+' not in ts and 'Z' not in ts: ts=ts+'+00:00'
    row=[ts,b['open'],b['high'],b['low'],b['close'],b.get('volume',0)]
    new=not os.path.exists(BUF)
    with open(BUF,'a',newline='') as f:
        w=csv.writer(f)
        if new: w.writerow(COLS)
        w.writerow(row)
    with open(BUF) as f: rows=f.readlines()
    if len(rows)>BUFFER_BARS+1:
        with open(BUF,'w') as f: f.write(rows[0]+''.join(rows[-BUFFER_BARS:]))

_poll_lock=threading.Lock()
def _poll_safe():
    """Run one detection cycle, never overlapping (fire-and-forget from /bars or the timer)."""
    if not _poll_lock.acquire(blocking=False): return
    try:
        if ENABLED: poll()
    except Exception as ex: print('[model C] poll err',ex,flush=True)
    finally:
        try: _poll_lock.release()
        except Exception: pass

def _fetch_buffer():
    """If STRAT_C_BUF_URL is set, pull the bar CSV from the agent over HTTP and cache it locally
    (bounded to BUFFER_BARS). No shared volume needed → the A/B agent is NEVER touched.
    Falls back to the last cache, then to the local file path."""
    if not BUF_URL or requests is None: return BUF
    lb=os.path.join(os.path.dirname(C_TRADES) or '.','c_buffer.csv')
    try:
        r=requests.get(BUF_URL,timeout=25)
        if getattr(r,'status_code',0)==200 and r.text:
            lines=r.text.splitlines()
            if len(lines)>2:
                out=[lines[0]]+lines[1:][-BUFFER_BARS:]
                with open(lb,'w') as f: f.write("\n".join(out)+"\n")
                return lb
        else: print('buf fetch status',getattr(r,'status_code',None),flush=True)
    except Exception as ex: print('buf fetch err',ex,flush=True)
    return lb if os.path.exists(lb) else BUF

def poll():
    if not ENABLED:
        print('STRAT_C_ENABLED != 1 -> idle'); return []
    buf=_fetch_buffer()
    sigs=c_signals(buf); sent=_ld(SENT_C); fired=[]
    for x in sigs:
        k=key_c(x)
        if k in sent: continue
        txt=to_alert_c(x)
        if TEST:
            print('[C_TEST] wykryty setup (NIC nie wysłano):\n'+txt,flush=True)   # silent: verify detection only
        elif EXEC_C: exec_c(x,text=txt,action='enter')   # relay sends alert+approve-buttons → skip plain post
        elif WEBHOOK_C: live_emit.post_webhook(txt,WEBHOOK_C)
        j=_ld(C_TRADES); j[k]=dict(x,be=False,status='alerted',mode=('test' if TEST else 'live'),alert_ts=dt.datetime.utcnow().isoformat(timespec='seconds')); _sv(C_TRADES,j)
        sent[k]=dt.datetime.utcnow().isoformat(timespec='seconds'); fired.append(x)
    _sv(SENT_C,sent)
    cands=log_candidates(buf)                         # refresh pipeline/step log
    _,st=resolve_perf(buf)                            # resolve outcomes + running stats
    print(f"[model_c_live] mode={'TEST(silent)' if TEST else 'LIVE'} signals={len(sigs)} new_fired={len(fired)} cands={len(cands)} perf_n={st.get('n',0)} exp={st.get('exp',0):+.3f}R",flush=True)
    return fired

def _maybe_push(txt):
    """Push an on-demand summary to Telegram if a webhook is available (relay text-only or plain webhook)."""
    if '--push' not in sys.argv: return
    try:
        if WEBHOOK_C: live_emit.post_webhook(txt,WEBHOOK_C)
        elif EXEC_C and requests is not None: requests.post(EXEC_C,json={'text':txt},timeout=10)
    except Exception as ex: print('push err',ex,flush=True)

# ───────────────────────── WEB DASHBOARD (own page for Strategy C) ─────────────────────────
def _stats_from_journal(j):
    """Running stats from the ALREADY-resolved journal (no detect) — for fast web reads."""
    done=[x for x in j.values() if x.get('status') in ('win','loss','be')]
    n_=len(done); w=sum(1 for x in done if x['status']=='win'); b=sum(1 for x in done if x['status']=='be'); l=sum(1 for x in done if x['status']=='loss')
    tot=sum(x.get('R',0) for x in done)
    return dict(n=n_,win=w,be=b,loss=l,winpct=(100*w/n_ if n_ else 0.0),winpct_exbe=(100*w/(w+l) if (w+l) else 0.0),
                exp=(tot/n_ if n_ else 0.0),totR=tot,
                pending=sum(1 for x in j.values() if x.get('status') in ('alerted',None)),
                no_fill=sum(1 for x in j.values() if x.get('status')=='no_fill'))

DASH_HTML = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Strategy C</title><style>
body{background:#0b0e14;color:#e6e9ef;font-family:system-ui,Segoe UI,Roboto,sans-serif;margin:0;padding:16px}
h1{font-size:18px;margin:0 0 2px}.mut{color:#9aa3b5;font-size:12px}
.card{background:#0e1320;border:1px solid #1b2230;border-radius:12px;padding:14px;margin:12px 0}
.big{font-size:34px;font-weight:700}.row{display:flex;gap:16px;flex-wrap:wrap;align-items:baseline}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-weight:700;font-size:13px}
.g{background:#123524;color:#4ade80}.a{background:#3a2f13;color:#f5b301}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid #1b2230}
.step{padding:2px 8px;border-radius:6px;font-size:12px;font-weight:600}
.s_rejection{background:#3a2f13;color:#f5b301}.s_entry_pending{background:#12294a;color:#3b82f6}.s_filled{background:#123524;color:#4ade80}
.s_displacement{background:#1b2230;color:#9aa3b5}.s_dropped_ab{background:#1b2230;color:#6b7280}.s_bos{background:#2a1f3a;color:#a78bfa}
.long{color:#4ade80}.short{color:#f87171}
</style></head><body>
<h1>&#127474; Strategy C <span id=mode class=mut></span></h1><div class=mut id=upd>&#322;adowanie&#8230;</div>
<div class=card><div class=mut>PERFORMANCE &#183; Gate 0 = +0.15R</div>
<div class=row><div class=big id=exp>&#8211;</div><div id=gate class=badge></div></div><div class=mut id=perfline></div></div>
<div class=card><div class=mut>PIPELINE &#183; mo&#380;liwe trady i ich krok</div>
<div id=counts class=row style="margin:8px 0"></div>
<table id=cand><thead><tr><th>kier.</th><th>displacement</th><th>FVG</th><th>krok</th><th>uwaga</th></tr></thead><tbody></tbody></table></div>
<div class=card><div class=mut>OSTATNIE TRADY</div>
<table id=jr><thead><tr><th>data</th><th>kier.</th><th>entry</th><th>status</th><th>R</th></tr></thead><tbody></tbody></table></div>
<script>
const B=location.pathname.endsWith('/')?location.pathname:location.pathname+'/';
async function J(u){return (await fetch(B+u)).json()}
async function load(){try{
 const h=await J('health');document.getElementById('mode').textContent='&#183; '+h.mode+(h.enabled?'':' (idle)');
 const p=await J('performance');
 document.getElementById('exp').textContent=p.n?((p.exp>=0?'+':'')+p.exp.toFixed(3)+'R'):'brak';
 const g=document.getElementById('gate');
 if(!p.n){g.className='badge a';g.textContent='0 tradów'}
 else if(p.exp>=0.15){g.className='badge g';g.textContent='✅ Gate 0'}
 else{g.className='badge a';g.textContent='⏳ < +0.15R'}
 document.getElementById('perfline').textContent=p.n+' tradów · W '+p.win+' · BE '+p.be+' · L '+p.loss+' · win '+p.winpct.toFixed(0)+'% (bez BE '+p.winpct_exbe.toFixed(0)+'%) · suma '+(p.totR>=0?'+':'')+p.totR.toFixed(1)+'R · pending '+p.pending;
 const c=await J('candidates');const cs=c.cands||[];const cc=c.counts||{};
 document.getElementById('counts').innerHTML=Object.keys(cc).map(k=>'<span class="step s_'+k+'">'+cc[k]+'× '+k+'</span>').join(' ');
 document.querySelector('#cand tbody').innerHTML=cs.map(x=>'<tr><td class="'+(x.dir=='LONG'?'long':'short')+'">'+x.dir+'</td><td>'+x.disp_start+'→'+x.disp_end+'</td><td>'+x.fvg_lo+'–'+x.fvg_hi+'</td><td><span class="step s_'+x.step+'">'+x.step+'</span></td><td class=mut>'+(x.note||'')+'</td></tr>').join('')||'<tr><td colspan=5 class=mut>brak</td></tr>';
 const jj=await J('journal');const rows=Object.values(jj).sort((a,b)=>(b.bos_ms||0)-(a.bos_ms||0)).slice(0,15);
 document.querySelector('#jr tbody').innerHTML=rows.map(x=>'<tr><td>'+(x.date||'')+' '+(x.bos||'')+'</td><td class="'+(x.dir=='LONG'?'long':'short')+'">'+x.dir+'</td><td>'+x.entry+'</td><td>'+(x.status||'')+'</td><td>'+(x.R!=null?((x.R>=0?'+':'')+Number(x.R).toFixed(2)):'')+'</td></tr>').join('')||'<tr><td colspan=5 class=mut>brak</td></tr>';
 document.getElementById('upd').textContent='odświeżono '+new Date().toLocaleTimeString();
}catch(e){document.getElementById('upd').textContent='błąd: '+e}}
load();setInterval(load,30000);
</script></body></html>"""

def register_routes(app, prefix='/c'):
    """Mount the Strategy-C dashboard + JSON endpoints onto an EXISTING Flask app (e.g. the A/B agent).
    prefix='/c' → page at /c, data at /c/performance //candidates //journal //health. prefix='' → root."""
    from flask import jsonify
    p=prefix.rstrip('/')
    def _home(): return DASH_HTML
    def _h(): return jsonify(ok=True,mode='TEST' if TEST else 'LIVE',enabled=ENABLED,touch_tol=TOUCH_TOL)
    def _perf(): return jsonify(_stats_from_journal(_ld(C_TRADES)))
    def _cand(): return jsonify(_ld(C_CAND) or {'cands':[],'counts':{}})
    def _jr(): return jsonify(_ld(C_TRADES))
    def _bars():                                     # F-style intake: agent forwards each bar here
        from flask import request as _rq
        b=_rq.get_json(force=True,silent=True) or {}
        if 'close' not in b: return jsonify(error='brak OHLC'),400
        try: _append_bar_c(b)
        except Exception as ex: print('[model C] /bars append err',ex,flush=True)
        threading.Thread(target=_poll_safe,daemon=True).start()   # detect on the fresh bar (non-overlapping)
        return jsonify(ok=True)
    app.add_url_rule(p or '/','c_home',_home)
    app.add_url_rule(p+'/health','c_health',_h)
    app.add_url_rule(p+'/performance','c_perf',_perf)
    app.add_url_rule(p+'/candidates','c_cand',_cand)
    app.add_url_rule(p+'/journal','c_journal',_jr)
    app.add_url_rule(p+'/bars','c_bars',_bars,methods=['POST'])
    return app

_bg_started=False
def start_background():
    """Start the C detection loop in a daemon thread (idempotent). Only polls when STRAT_C_ENABLED=1.
    Wrapped so a C failure can NEVER propagate into the host process (A/B stays untouched)."""
    global _bg_started
    if _bg_started: return
    _bg_started=True
    def _bg():
        while True:
            _poll_safe()                                          # safety net if no bars are being forwarded
            time.sleep(int(os.environ.get('STRAT_C_POLL_SEC','90')))
    threading.Thread(target=_bg,daemon=True).start()

app = None
def _ensure_app():
    """Create the module-level Flask `app` (routes + detection thread). Mirrors agent:app so Railway/
    railpack auto-detects Flask and `gunicorn model_c_live:app --bind 0.0.0.0:$PORT` works like the other services."""
    global app
    if app is not None: return app
    try:
        from flask import Flask
        _a = Flask(__name__); register_routes(_a, ''); start_background(); app = _a
    except Exception as _e:
        print('[model C] web app init skipped:', _e, flush=True)
    return app

def _run_web():
    """Local dev: python3 model_c_live.py --serve (Flask dev server)."""
    _ensure_app()
    if app is not None: app.run(host='0.0.0.0', port=int(os.environ.get('PORT','8080')))

# Build `app` at import so `gunicorn model_c_live:app` serves it (skip for one-shot CLI commands).
if not any(_a in sys.argv for _a in ('--candidates','--perf','--loop')):
    _ensure_app()

if __name__=='__main__':
    if '--serve' in sys.argv:                 # web dashboard (own URL) + poll loop in background
        _run_web()
    elif '--candidates' in sys.argv:          # on-demand pipeline: which step is each possible trade on
        b=_fetch_buffer(); cands=log_candidates(b); txt=cand_summary(cands); print('\n'+txt); _maybe_push(txt)
    elif '--perf' in sys.argv:                # on-demand performance: running win%/exp-R (Gate-0 gauge)
        b=_fetch_buffer(); _,st=resolve_perf(b); txt=perf_summary(st); print('\n'+txt); _maybe_push(txt)
    elif '--loop' in sys.argv:
        while True:
            try: poll()
            except Exception as ex: print('poll err',ex,flush=True)
            time.sleep(int(os.environ.get('STRAT_C_POLL_SEC','60')))
    else:
        # one-shot; if STRAT_C_ENABLED unset, still PRINT what it WOULD alert (dry test) + the pipeline
        if ENABLED: poll()
        else:
            b=_fetch_buffer()
            for x in c_signals(b): print('\n'+to_alert_c(x))
            try: print('\n'+cand_summary(c_candidates(b)[0]))
            except Exception as ex: print('cand err',ex,flush=True)
