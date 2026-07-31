# manage.py — sledzenie otwartych tradow i alerty zarzadzania.
# Strategia: BE@1R / TP calosc 2R / pelna pozycja. Po 1R stop -> BE (entry).
# Wyjscia: SL (-1R), BE (0R po uzbrojeniu), TP 2R (+2R), albo wygasniecie po 8h.
# Rdzen (det_v10) zamrozony. Wywolywane z agent.py: register() po potwierdzonym alercie,
# check() na kazdym nowym barze. Opakowane try/except po stronie agenta — nie moze ruszyc intake'u.
import json, os

def _dec_for(price):
    """Decimal precision for stored SL/TP. Index futures (MNQ, price>=1000) -> 1 (UNCHANGED).
    Forex was silently broken: round(1.1434,1)=1.1 collapsed every FX stop -> fake outcomes.
    PRICE_ROUND env overrides; else auto by magnitude (JPY ~150 -> 3, EUR/GBP ~1.1 -> 5)."""
    env = os.environ.get('PRICE_ROUND', '').strip()
    if env.lstrip('-').isdigit():
        return int(env)
    a = abs(float(price))
    if a >= 1000: return 1        # MNQ / index — identical to the old behaviour
    if a >= 10:   return 3        # USD/JPY
    return 5                      # EUR/USD, GBP/USD

def _load(path):
    try:
        with open(path) as f: return json.load(f)
    except Exception:
        return []

def _save(path, lst):
    try:
        with open(path, 'w') as f: json.dump(lst, f)
    except Exception:
        pass

def _record(opath, t, r, reason, bar_ms):
    """Zapisz wynik zamknietego trade'a (realized R) do trwalego logu — zasila /performance."""
    if not opath: return
    try:
        outs = _load(opath)
        if any(o.get('key') == t['key'] for o in outs): return   # idempotentne po kluczu
        outs.append(dict(key=t['key'], dir=t['dir'], cat=t['cat'], entry=t['entry'], sl=t['sl'],
                         r=r, reason=reason, bos_ms=t.get('bos_ms', 0), closed_ms=int(bar_ms or 0)))
        _save(opath, outs[-500:])
    except Exception:
        pass

def _partial_frac():
    """v30: fraction of the position banked at +1R = PARTIAL_ACCT_PCT / RISK_PCT
    (0.2% of account at 0.5% risk -> 0.4). PARTIAL_AT_1R=0 -> 0.0 (no partial)."""
    try:
        if os.environ.get('PARTIAL_AT_1R', '0') != '1': return 0.0   # v30.1: default OFF
        rp = float(os.environ.get('RISK_PCT', '0.5') or 0.5)
        pp = float(os.environ.get('PARTIAL_ACCT_PCT', '0.2') or 0.2)
        return max(0.0, min(0.9, pp / rp)) if rp > 0 else 0.0
    except Exception:
        return 0.0


def register(x, path):
    """Zarejestruj potwierdzony trade do sledzenia. Idempotentne (po kluczu)."""
    e = float(x['entry']); sl = float(x['SL']); bull = x['dir'] == 'LONG'; R = abs(e - sl)
    if R <= 0: return
    r1 = e + R if bull else e - R
    r2 = e + 2*R if bull else e - 2*R
    # v30: track the REAL emitted target (swing level or 2R fallback), not a hardcoded 2R
    try: tp = float(x.get('TP')) if x.get('TP') is not None else r2
    except Exception: tp = r2
    key = f"{x['date']}|{x['model']}|{x['cat']}|{x['dir']}|{x['bos']}"
    lst = _load(path)
    if any(t.get('key') == key for t in lst): return
    _dec = _dec_for(e)                                    # FX-safe precision (MNQ stays at 1); entry kept RAW as before
    lst.append(dict(key=key, dir=x['dir'], cat=x['cat'], entry=e, sl=round(sl,_dec),
                    r1=round(r1,_dec), r2=round(r2,_dec), tp=round(tp,_dec),
                    tp_r=round(abs(tp - e) / R, 3), tp_src=x.get('tp_src'),
                    bos_ms=int(x.get('bos_ms', 0)),
                    filled=False, done1=False, part=False))
    _save(path, lst[-50:])   # trzymaj ostatnie 50

def check(hi, lo, bar_ms, send, path, expire_ms=8*3600*1000, fill_ms=None, outcomes_path=None):
    """Na nowym barze. NAJPIERW brama FILL: wejscie to LIMIT (cofniecie) — nie liczymy zadnych
       celow dopoki limit nie zostanie trafiony. Niewypelniony limit anulujemy po fill_ms (2h).
       Po fillu, kolejnosc ADVERSE-FIRST (najpierw ruch przeciw — konserwatywnie):
       PRZED 1R:  SL trafiony -> -1R, koniec.  Inaczej 1R trafiony -> przesun SL na BE.
       PO 1R:     entry trafiony (BE) -> 0R, koniec.  Inaczej 2R trafiony -> +2R, koniec.
       Stare trady (>8h od BOS) wygasaja. send(msg) wysyla powiadomienie."""
    if fill_ms is None:      # v28: same clock as shadow's FILL_WIN_MIN (was a hardcoded 2 h)
        try: fill_ms = max(1, int(float(os.environ.get('FILL_WIN_MIN', '10')))) * 60000
        except Exception: fill_ms = 10 * 60000
    lst = _load(path)
    if not lst: return
    keep = []; changed = False
    for t in lst:
        bull = t['dir'] == 'LONG'; emoji = '🟢' if bull else '🔴'; drop = False
        e = t['entry']; sl = t['sl']; r1 = t['r1']; r2 = t.get('r2', t.get('r3'))

        # ---- FILL GATE: bez tego leciały fałszywe TP/SL zaraz po wejściu (limit nie był wypełniony) ----
        if not t.get('filled'):
            if t.get('bos_ms') and bar_ms and bar_ms <= t['bos_ms']:
                keep.append(t); continue          # order does not exist yet on this bar -> no fill possible
            hit_entry = (lo <= e) if bull else (hi >= e)
            if hit_entry:
                t['filled'] = True; changed = True
                send(f"✅ FILL {emoji} {t['dir']} · {t['cat']} → wejście @ {e} aktywne (SL {sl} · cel 2R {r2}).")
                keep.append(t); continue          # celów nie sprawdzamy na barze wypełnienia — czekamy na kolejny
            if t.get('bos_ms') and bar_ms and (bar_ms - t['bos_ms']) > fill_ms:
                send(f"⌛ {emoji} {t['dir']} · {t['cat']} → limit @ {e} niewypełniony w {fill_ms//3600000}h — anulowany.")
                drop = True; changed = True
            if not drop: keep.append(t)
            continue

        _fixed = os.environ.get('MANAGE_FIXED', '1') == '1'   # auto trades run a FIXED bracket:
        # no BE. MANAGE_FIXED=0 restores the old BE@1R advisory narration (manual-trading style).
        tp  = t.get('tp', r2)                 # v30: real target (swing / 2R); legacy rows fall back to r2
        tpr = t.get('tp_r', 2.0) or 2.0
        fr  = _partial_frac()                 # v30: 0.4 by default; 0.0 = v29 whole-position maths
        if _fixed:
            if (lo <= sl) if bull else (hi >= sl):
                if t.get('part'):             # runner stopped AFTER the 1R partial was banked
                    net = round(2 * fr - 1.0, 3)
                    send(f"🛑 SL {emoji} {t['dir']} · {t['cat']} → stop @ {sl} po partialu 1R. Netto {net:+}R. Zakończony.")
                    _record(outcomes_path, t, net, 'SL_after_partial', bar_ms)
                else:
                    send(f"🛑 SL {emoji} {t['dir']} · {t['cat']} → stop @ {sl}. Trade zamknięty (−1R). Zakończony.")
                    _record(outcomes_path, t, -1.0, 'SL', bar_ms)
                drop = True; changed = True
            elif fr > 0 and not t.get('part') and ((hi >= r1) if bull else (lo <= r1)):
                t['part'] = True; changed = True   # bank the 1R leg; runner keeps going (target checked next bar)
                send(f"💰 PARTIAL {emoji} {t['dir']} · {t['cat']} → +1R @ {r1}: zdjęte {int(round(fr*100))}% pozycji "
                     f"(+{round(fr,2)}R w kieszeni). Runner {int(round((1-fr)*100))}% → cel {tp} ({t.get('tp_src') or '2R'}).")
            elif (hi >= tp) if bull else (lo <= tp):
                net = round((fr * 1.0 + (1 - fr) * tpr) if t.get('part') else tpr, 3)
                send(f"🎯 TP {emoji} {t['dir']} · {t['cat']} → cel @ {tp} ({t.get('tp_src') or '2R'}). Netto {net:+}R. Zakończony.")
                _record(outcomes_path, t, net, 'TP', bar_ms); drop = True; changed = True
        elif not t['done1']:
            # 1) SL (ruch przeciw) — sprawdzany NAJPIERW
            if (lo <= sl) if bull else (hi >= sl):
                send(f"🛑 SL {emoji} {t['dir']} · {t['cat']} → stop @ {sl}. Trade zamknięty (−1R). Zakończony.")
                _record(outcomes_path, t, -1.0, 'SL', bar_ms)
                drop = True; changed = True
            # 2) inaczej: 1R -> uzbrojenie BE
            elif (hi >= r1) if bull else (lo <= r1):
                send(f"⚡ 1R OSIĄGNIĘTE {emoji} {t['dir']} · {t['cat']} (entry {e}) "
                     f"→ przesuń SL na BE ({e}). TRZYMAJ całość, cel 2R ({r2}).")
                t['done1'] = True; changed = True

        if (not _fixed) and t['done1'] and not drop:
            # 3) po 1R stop = entry (BE). Powrot na entry (ruch przeciw) — sprawdzany NAJPIERW
            if (lo <= e) if bull else (hi >= e):
                send(f"➖ BE {emoji} {t['dir']} · {t['cat']} → cena wróciła na entry ({e}). "
                     f"Trade zamknięty (0R). Zakończony.")
                _record(outcomes_path, t, 0.0, 'BE', bar_ms)
                drop = True; changed = True
            # 4) inaczej: 2R -> TP
            elif (hi >= r2) if bull else (lo <= r2):
                send(f"🎯 2R OSIĄGNIĘTE {emoji} {t['dir']} · {t['cat']} → ZAMKNIJ całość @ {r2}. "
                     f"Trade zakończony (+2R).")
                _record(outcomes_path, t, 2.0, 'TP', bar_ms)
                drop = True; changed = True

        if not drop and t.get('bos_ms') and bar_ms and (bar_ms - t['bos_ms']) > expire_ms:
            _record(outcomes_path, t, None, 'timeout', bar_ms)
            drop = True; changed = True

        if not drop: keep.append(t)
    if changed: _save(path, keep)
