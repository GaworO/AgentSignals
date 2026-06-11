"""
regime.py — analiza reżimu rynku: typ rynku (choppy/trendowy/cichy) + modelowy rolling PF/EV.
MODUŁ. NIE rusza rdzenia det_new.py (det leci jako subprocess). agent.py tylko importuje i woła.
Nowe funkcje analityczne dokładamy TUTAJ, nie w rdzeniu.
"""
import os, subprocess, pickle, statistics as st


def sim_R(x, hi, lo, cl, n, CAP=480):
    """modelowy wynik w R: BE@1R, TP calosc 2R, pelna pozycja. None = trade nierozstrzygniety."""
    e=x['entry']; sl=x['SL']; bull=x['dir']=='LONG'; R=abs(e-sl)
    if R<=0: return None
    tp=e+2*R if bull else e-2*R; belev=e+R if bull else e-R
    i0=int(x['entry_bar'])+1; i=i0; armed=False
    while i<min(i0+CAP,n):
        h,l=hi[i],lo[i]
        if (l<=sl) if bull else (h>=sl): return -1.0          # stop przed 1R
        if (h>=tp) if bull else (l<=tp): return 2.0           # target przed 1R (przy waskim R)
        if (h>=belev) if bull else (l<=belev): armed=True; i+=1; break
        i+=1
    if not armed: return None
    while i<min(i0+CAP,n):                                     # po 1R: SL na BE, cel 2R
        h,l=hi[i],lo[i]
        if (l<=e) if bull else (h>=e): return 0.0
        if (h>=tp) if bull else (l<=tp): return 2.0
        i+=1
    return None


def market_type(sl_med, supply, setups_total, pf, ev, resolved):
    """charakter rynku (NIE werdykt sizingu) -> (label, opis, kolor)."""
    if setups_total == 0:
        return ('Cichy', 'Brak setupów w oknie — rynek nie daje sygnałów. Czekaj.', 'amber')
    if supply == 0:
        return ('Choppy — szerokie stopy',
                f'Setupy są, ale wszystkie z szerokim SL (mediana {sl_med:.0f} pkt). Brak czystych wybić = rynek piłujący. Twój edge śpi.',
                'red')
    if resolved >= 5 and pf >= 1.6 and ev > 0:
        return ('Trendowy — czyste wybicia',
                f'Wąskie stopy (mediana {sl_med:.0f} pkt), setupy działają (PF {pf:.1f}). Idealne warunki — graj.',
                'green')
    if sl_med > 35:
        return ('Choppy — szerokie stopy',
                f'Mediana SL {sl_med:.0f} pkt — szeroko. Rynek piłujący, ostrożnie.', 'red')
    if resolved < 5:
        return ('Niejasny — mało danych',
                f'Wąskie stopy, ale za mało rozstrzygniętych tradów ({resolved}) by ocenić wynik. Czekaj na próbkę.', 'amber')
    return ('Mieszany', f'Stopy wąskie, ale wynik niejednoznaczny (PF {pf:.1f}).', 'amber')


def regime_stats(buf_path, here, window=20, sl_min=10.0, sl_max=30.0):
    """live z bufora: typ rynku + modelowy rolling PF/EV (R) + mediana SL + podaz setupow -> werdykt sizingu."""
    try: import pandas as pd
    except Exception: return {'ok': False, 'err': 'pandas'}
    out = buf_path + '.regime.pkl'
    try:
        env = dict(os.environ, DATA_CSV=buf_path, OUT_PKL=out, CUTOFF='')
        subprocess.run(['python3', os.path.join(here, 'det_new.py')], env=env, capture_output=True, timeout=120)
        S = pickle.load(open(out, 'rb'))
    except Exception:
        S = []
    try:
        df = pd.read_csv(buf_path); ts = pd.to_datetime(df.ts_event, utc=True)
        df = df.assign(ts=ts).sort_values('ts').reset_index(drop=True)
        hi = df.high.values; lo = df.low.values; cl = df.close.values; n = len(df)
    except Exception:
        return {'ok': False, 'err': 'buffer'}
    outcomes=[]; sls=[]; sl_all=[]
    for x in sorted(S, key=lambda z: int(z.get('entry_bar', 0))):
        R = abs(x['entry']-x['SL']); sl_all.append(R)
        if not (sl_min <= R <= sl_max): continue
        sls.append(R)
        r = sim_R(x, hi, lo, cl, n)
        if r is not None: outcomes.append(r)
    last = outcomes[-window:]
    wins = [r for r in last if r > 0]; losses = [r for r in last if r <= 0]
    pf = (sum(wins)/abs(sum(losses))) if (losses and sum(losses) != 0) else (99.0 if wins else 0.0)
    ev = sum(last)/len(last) if last else 0.0
    slmed = st.median(sl_all) if sl_all else 0
    # werdykt sizingu
    if   len(sls) == 0:        state = 'amber'
    elif len(last) < 5:        state = 'amber'
    elif pf >= 1.6 and ev > 0: state = 'green'
    elif pf >= 1.2:            state = 'amber'
    else:                      state = 'red'
    mt_label, mt_desc, mt_color = market_type(slmed, len(sls), len(S), pf, ev, len(outcomes))
    return {'ok': True, 'state': state, 'pf': round(pf, 2), 'ev_R': round(ev, 2),
            'trades': len(last), 'resolved': len(outcomes), 'supply': len(sls),
            'setups_total': len(S), 'sl_med': round(float(slmed), 1),
            'market_type': mt_label, 'market_desc': mt_desc, 'market_color': mt_color,
            'window': window, 'buffer_bars': n}
