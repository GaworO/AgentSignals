"""
regime_gate.py — czy filtr EOD (v12) ma byc ON/OFF wg rezimu rynku, + powiadomienie Telegram.

ON  = rynek CHOPPY (regime.py: market_color == 'red' lub market_type zaczyna sie od 'Choppy')
      -> w choppy tniemy stale sygnaly sesyjne (session H/L tapniete bez potwierdzenia gasna po koncu dnia).
OFF = trend / spokoj -> pelny upside v11 (poziomy zyja do sweepu / cap 10 dni).

Powiadomienie Telegram leci TYLKO przy ZMIANIE stanu (nie spamuje co bar). Stan w json w DATA_DIR.
Importowane przez agent.py; rdzen detektora (detcore) nietkniety — gating to decyzja LIVE.
"""
import os
import json


def decide(reg):
    """reg = dict z regime.regime_stats(). Zwraca (eod_on: bool, label: 'ON'/'OFF', reason: str)."""
    if not reg or not reg.get('ok'):
        return (False, 'OFF', 'brak danych rezimu — domyslnie OFF (pelny v11)')
    mt = str(reg.get('market_type', ''))
    color = reg.get('market_color') or reg.get('state')
    choppy = (color == 'red') or mt.startswith('Choppy')
    if choppy:
        return (True, 'ON',
                f"rezim: {mt or 'choppy'} (PF {reg.get('pf')}, SL med {reg.get('sl_med')} pkt) "
                f"— tne stale sygnaly sesyjne (EOD)")
    return (False, 'OFF',
            f"rezim: {mt or 'trend/spokoj'} (PF {reg.get('pf')}) — pelny upside v11")


def _statefile(data_dir):
    return os.path.join(data_dir, 'regime_gate_state.json')


def notify_if_changed(reg, webhook_url, data_dir, post_fn):
    """Wyslij Telegram TYLKO gdy stan EOD sie zmienil.
    post_fn(text, url) -> kod (uzyj live_emit.post_webhook). Zwraca (eod_on, label, code/'unchanged')."""
    eod_on, label, reason = decide(reg)
    sf = _statefile(data_dir)
    try:
        prev = json.load(open(sf)).get('label')
    except Exception:
        prev = None
    if label != prev:
        emoji = '🌀' if eod_on else '📈'
        msg = (f"{emoji} Filtr EOD (v12): {label}\n{reason}\n"
               f"(ON = w choppy sesyjne high/low gasna po koncu dnia jesli tapniete bez potwierdzenia; "
               f"OFF = pelny v11)")
        code = post_fn(msg, webhook_url) if webhook_url else 'no-url'
        try:
            json.dump({'label': label, 'eod_on': eod_on, 'reason': reason}, open(sf, 'w'))
        except Exception:
            pass
        return eod_on, label, str(code)
    return eod_on, label, 'unchanged'
