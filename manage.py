# manage.py — sledzenie otwartych tradow i alerty zarzadzania.
# Strategia: BE@1R / TP calosc 2R / pelna pozycja. Po 1R stop -> BE (entry).
# Wyjscia: SL (-1R), BE (0R po uzbrojeniu), TP 2R (+2R), albo wygasniecie po 8h.
# Rdzen (det_new) zamrozony. Wywolywane z agent.py: register() po potwierdzonym alercie,
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
                    r1=round(r1,1), r2=round(r2,1), bos_ms=int(x.get('bos_ms', 0)), done1=False))
    _save(path, lst[-50:])   # trzymaj ostatnie 50

def check(hi, lo, bar_ms, send, path, expire_ms=8*3600*1000):
    """Na nowym barze, kolejnosc ADVERSE-FIRST (najpierw ruch przeciw — konserwatywnie):
       PRZED 1R:  SL trafiony -> -1R, koniec.  Inaczej 1R trafiony -> przesun SL na BE.
       PO 1R:     entry trafiony (BE) -> 0R, koniec.  Inaczej 2R trafiony -> +2R, koniec.
       Stare trady (>8h od BOS) wygasaja. send(msg) wysyla powiadomienie."""
    lst = _load(path)
    if not lst: return
    keep = []; changed = False
    for t in lst:
        bull = t['dir'] == 'LONG'; emoji = '🟢' if bull else '🔴'; drop = False
        e = t['entry']; sl = t['sl']; r1 = t['r1']; r2 = t.get('r2', t.get('r3'))

        if not t['done1']:
            # 1) SL (ruch przeciw) — sprawdzany NAJPIERW
            if (lo <= sl) if bull else (hi >= sl):
                send(f"🛑 SL {emoji} {t['dir']} · {t['cat']} → stop @ {sl}. Trade zamknięty (−1R). Zakończony.")
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
                drop = True; changed = True
            # 4) inaczej: 2R -> TP
            elif (hi >= r2) if bull else (lo <= r2):
                send(f"🎯 2R OSIĄGNIĘTE {emoji} {t['dir']} · {t['cat']} → ZAMKNIJ całość @ {r2}. "
                     f"Trade zakończony (+2R).")
                drop = True; changed = True

        if not drop and t.get('bos_ms') and bar_ms and (bar_ms - t['bos_ms']) > expire_ms:
            drop = True; changed = True

        if not drop: keep.append(t)
    if changed: _save(path, keep)
