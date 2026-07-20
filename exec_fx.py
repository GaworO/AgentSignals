#!/usr/bin/env python3
"""
exec_fx.py — FX execution adapter: guard-approved orders -> MetaApi (cloud MT5) -> prop-firm account.
Isolated add-on, same pattern as shadow/guardrails. agent.py delegates to this when EXEC_FX=1 —
the TradersPost futures path is untouched. Returns the same result dict shape _exec_order produces,
so the guard's sent-check (2xx + sent:true) works unchanged.

Env (per FX service):
  EXEC_FX=1                    enable this adapter (unset = normal TradersPost path)
  METAAPI_TOKEN                MetaApi auth token (metaapi.cloud account)
  METAAPI_ACCOUNT_ID           the MetaApi account id bound to the prop firm's MT5 login
  METAAPI_REGION=london        MetaApi region of the account
  FX_SYMBOL=EURUSD             MT5 symbol
  FX_PIP_SIZE=0.0001           EURUSD 0.0001 · USDJPY 0.01
  FX_PIP_VALUE=10.0            USD value of 1 pip per 1.0 lot (EURUSD ~10; USDJPY ~1000/price ≈ 6.2)
  ACCOUNT / RISK_PCT           risk sizing (same convention as futures: RISK_PCT% of ACCOUNT per R)
  FX_MAX_LOTS=2.0              hard cap
  FX_MIN_LOTS=0.01             broker minimum
  FX_ENTRY_TTL_MIN=240         pending entry expires after N minutes (matches the model's 4h fill
                               window — no orphan limits, the MT5 server enforces it)
  FX_LOT_OVERRIDE              set to a number to force lots (the ramp: 0.01)

Smoke-test route (wire in agent like /exectest): exec_fx.place(sample, None) with FX_LOT_OVERRIDE=0.01.
NOTE: written against MetaApi's documented REST trade API; the demo week exists to prove it —
do not point at a paid challenge account before the FTMO free-trial run is clean.
"""
import os, json

try:
    import requests
except Exception:
    requests = None


def _env(k, d=None):
    v = os.environ.get(k)
    return v if v not in (None, '') else d


def lots_for(entry, sl):
    """Risk-based lot size: RISK_PCT% of ACCOUNT ÷ (stop-pips × pip value/lot). Rounded DOWN to 0.01."""
    try:
        ov = _env('FX_LOT_OVERRIDE')
        if ov: return max(float(_env('FX_MIN_LOTS', '0.01')), float(ov))
        acct = float(_env('ACCOUNT', '100000')); riskp = float(_env('RISK_PCT', '0.5'))
        pip = float(_env('FX_PIP_SIZE', '0.0001')); pv = float(_env('FX_PIP_VALUE', '10'))
        risk_usd = acct * riskp / 100.0
        stop_pips = abs(float(entry) - float(sl)) / pip
        if stop_pips <= 0 or pv <= 0: return None
        lots = int(risk_usd / (stop_pips * pv) * 100) / 100.0          # round DOWN to 0.01
        lots = max(float(_env('FX_MIN_LOTS', '0.01')), min(lots, float(_env('FX_MAX_LOTS', '2'))))
        return lots
    except Exception:
        return None


def place(x, text=None):
    """Guard-approved setup dict (dir/entry/SL[/TP]) -> MT5 pending limit with SL/TP via MetaApi.
    Returns {'sent': bool, 'status': int, 'resp': str, 'qty': lots} — same shape as _exec_order."""
    if requests is None:
        return {"sent": False, "reason": "requests missing"}
    tok = _env('METAAPI_TOKEN'); acc = _env('METAAPI_ACCOUNT_ID')
    if not tok or not acc:
        return {"sent": False, "reason": "METAAPI_TOKEN / METAAPI_ACCOUNT_ID not set"}
    try:
        bull = x['dir'] == 'LONG'
        e = float(x['entry']); sl = float(x['SL']); R = abs(e - sl)
        tp = x.get('TP')
        tp = float(tp) if tp else (e + 2 * R if bull else e - 2 * R)
        lots = lots_for(e, sl)
        if lots is None:
            return {"sent": False, "reason": "lot sizing failed"}
        if x.get('_exec_qty_override') is not None:                    # guard ramp -> minimum size
            lots = float(_env('FX_MIN_LOTS', '0.01'))
        dp = 3 if float(_env('FX_PIP_SIZE', '0.0001')) >= 0.01 else 5  # JPY 3dp, majors 5dp
        body = {
            "actionType": "ORDER_TYPE_BUY_LIMIT" if bull else "ORDER_TYPE_SELL_LIMIT",
            "symbol": _env('FX_SYMBOL', 'EURUSD'),
            "volume": lots,
            "openPrice": round(e, dp),
            "stopLoss": round(sl, dp),
            "takeProfit": round(tp, dp),
            "comment": "AB-auto",
        }
        ttl = int(float(_env('FX_ENTRY_TTL_MIN', '240')))
        if ttl > 0:
            import datetime as dt
            exp = dt.datetime.utcnow() + dt.timedelta(minutes=ttl)
            body["expiration"] = {"type": "ORDER_TIME_SPECIFIED",
                                  "time": exp.strftime('%Y-%m-%dT%H:%M:%S.000Z')}
        region = _env('METAAPI_REGION', 'london')
        url = f"https://mt-client-api-v1.{region}.agiliumtrade.ai/users/current/accounts/{acc}/trade"
        r = requests.post(url, json=body, headers={"auth-token": tok}, timeout=15)
        st = getattr(r, 'status_code', None)
        rb = ''
        try: rb = (r.text or '')[:200]
        except Exception: pass
        ok = bool(st) and 200 <= int(st) < 300
        print('EXEC_FX', st, json.dumps(body), flush=True)
        return {"sent": ok, "status": st, "resp": rb, "qty": lots}
    except Exception as ex:
        print('EXEC_FX err', ex, flush=True)
        return {"sent": False, "error": str(ex)}


def flatten(reason=''):
    """Close all positions + cancel pending orders for FX_SYMBOL (guard flatten hook)."""
    tok = _env('METAAPI_TOKEN'); acc = _env('METAAPI_ACCOUNT_ID')
    if not tok or not acc or requests is None: return False
    region = _env('METAAPI_REGION', 'london')
    base = f"https://mt-client-api-v1.{region}.agiliumtrade.ai/users/current/accounts/{acc}"
    ok = False
    try:
        body = {"actionType": "POSITIONS_CLOSE_SYMBOL", "symbol": _env('FX_SYMBOL', 'EURUSD')}
        r = requests.post(base + "/trade", json=body, headers={"auth-token": tok}, timeout=15)
        ok = 200 <= getattr(r, 'status_code', 0) < 300
        print('EXEC_FX flatten', reason, getattr(r, 'status_code', '?'), flush=True)
    except Exception as e:
        print('EXEC_FX flatten err', e, flush=True)
    return ok
