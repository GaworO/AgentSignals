# manage.py — sledzenie otwartych tradow i alerty zarzadzania.
# Strategia: BE@1R / TP calosc 2R / pelna pozycja. Po 1R stop -> BE (entry).
# Wyjscia: SL (-1R), BE (0R po uzbrojeniu), TP 2R (+2R), albo wygasniecie po 8h.
# Rdzen (det_v10) zamrozony. Wywolywane z agent.py: register() po potwierdzonym alercie,
# check() na kazdym nowym barze. Opakowane try/except po stronie agenta — nie moze ruszyc intake'u.
import json

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

def register(x, path):
    """Zarejestruj potwierdzony trade do sledzenia. Idempotentne (po kluczu)."""
    e = float(x['entry']); sl = float(x['SL']); bull = x['dir'] == 'LONG'; R = abs(e - sl)
    if R <= 0: return
    r1 = e + R if bull else e - R
    r2 = e + 2*R if bull else e - 2*R
    key = f"{x['date']}|{x['model']}|{x['cat']}|{x['dir']}|{x['bos']}"
    lst = _load(path)
    if any(t.get('key') == key for t in lst): return
    lst.append(dict(key=key, dir=x['dir'], cat=x['cat'], entry=e, sl=round(sl,1),
                    r1=round(r1,1), r2=round(r2,1), bos_ms=int(x.get('bos_ms', 0)),
                    filled=False, done1=False))
    _save(path, lst[-50:])   # trzymaj ostatnie 50

def check(hi, lo, bar_ms, send, path, expire_ms=8*3600*1000, fill_ms=2*3600*1000, outcomes_path=None):
    """Na nowym barze. NAJPIERW brama FILL: wejscie to LIMIT (cofniecie) — nie liczymy zadnych
       celow dopoki limit nie zostanie trafiony. Niewypelniony limit anulujemy po fill_ms (2h).
       Po fillu, kolejnosc ADVERSE-FIRST (najpierw ruch przeciw — konserwatywnie):
       PRZED 1R:  SL trafiony -> -1R, koniec.  Inaczej 1R trafiony -> przesun SL na BE.
       PO 1R:     entry trafiony (BE) -> 0R, koniec.  Inaczej 2R trafiony -> +2R, koniec.
       Stare trady (>8h od BOS) wygasaja. send(msg) wysyla powiadomienie."""
    lst = _load(path)
    if not lst: return
    keep = []; changed = False
    for t in lst:
        bull = t['dir'] == 'LONG'; emoji = '🟢' if bull else '🔴'; drop = False
        e = t['entry']; sl = t['sl']; r1 = t['r1']; r2 = t.get('r2', t.get('r3'))

        # ---- FILL GATE: bez tego leciały fałszywe TP/SL zaraz po wejściu (limit nie był wypełniony) ----
        if not t.get('filled'):
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

        if not t['done1']:
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

        if t['done1'] and not drop:
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
