# manage.py — sledzenie otwartych tradow i alerty zarzadzania (1R -> partial+BE, 3R -> zamknij runner)
# Rdzen (det_new) zamrozony. Ten modul jest wywolywany z agent.py: register() po potwierdzonym alercie,
# check() na kazdym nowym barze. Wszystko opakowane try/except po stronie agenta — nie moze ruszyc intake'u.
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
    """Zarejestruj potwierdzony trade do sledzenia 1R/3R. Idempotentne (po kluczu)."""
    e = float(x['entry']); sl = float(x['SL']); bull = x['dir'] == 'LONG'; R = abs(e - sl)
    if R <= 0: return
    r1 = e + R if bull else e - R
    r3 = e + 3*R if bull else e - 3*R
    key = f"{x['date']}|{x['model']}|{x['cat']}|{x['dir']}|{x['bos']}"
    lst = _load(path)
    if any(t.get('key') == key for t in lst): return
    lst.append(dict(key=key, dir=x['dir'], cat=x['cat'], entry=e, sl=round(sl,1),
                    r1=round(r1,1), r3=round(r3,1), bos_ms=int(x.get('bos_ms', 0)), done1=False))
    _save(path, lst[-50:])   # trzymaj ostatnie 50

def check(hi, lo, bar_ms, send, path, expire_ms=8*3600*1000):
    """Na nowym barze: jesli cena dotknela 1R -> alert (partial+BE); jesli po 1R dotknela 3R -> alert (zamknij).
       send(msg) wysyla powiadomienie. Stare trady (>8h od BOS) wygasaja."""
    lst = _load(path)
    if not lst: return
    keep = []; changed = False
    for t in lst:
        bull = t['dir'] == 'LONG'; emoji = '🟢' if bull else '🔴'; drop = False
        if not t['done1']:
            if (hi >= t['r1']) if bull else (lo <= t['r1']):
                send(f"⚡ 1R OSIĄGNIĘTE {emoji} {t['dir']} · {t['cat']} (entry {t['entry']}) "
                     f"→ ZAMKNIJ 1/3 i przesuń SL na BE ({t['entry']}). Runner 2/3 leci na 3R ({t['r3']}).")
                t['done1'] = True; changed = True
        if t['done1']:
            if (hi >= t['r3']) if bull else (lo <= t['r3']):
                send(f"🎯 3R OSIĄGNIĘTE {emoji} {t['dir']} · {t['cat']} → ZAMKNIJ runner @ {t['r3']}. Trade zakończony.")
                drop = True; changed = True
        if t.get('bos_ms') and bar_ms and (bar_ms - t['bos_ms']) > expire_ms:
            drop = True; changed = True
        if not drop: keep.append(t)
    if changed: _save(path, keep)
