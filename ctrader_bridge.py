#!/usr/bin/env python3
"""
ctrader_bridge.py — v1.6 cTrader Open API execution bridge for the forex auto-executor.

WHY THIS EXISTS
    Your guard/executor already POSTs a TradersPost-style bracket to EXEC_WEBHOOK. cTrader's Open API
    is NOT a webhook — it's a persistent Protobuf/TLS socket with a two-stage handshake. This bridge is
    a small Flask service that (a) holds the cTrader connection in a background Twisted thread, and
    (b) exposes /exec accepting the SAME bracket schema the executor sends, translating it into a
    ProtoOANewOrderReq. So NOTHING in the executor changes — you just point EXEC_WEBHOOK at this bridge.

APPROVAL stays UPSTREAM: run the guard in MODE=manual (tap-to-approve) → only approved brackets ever
reach the bridge. There is no TradersPost "pending order" screen with the direct API, so the guard IS
the approval step.

SAFETY RAILS (both ON by default):
    * CT_DRY_RUN=1  -> the bridge LOGS the exact order it would place and returns it, but sends nothing.
    * CT_HOST=demo  -> demo.ctraderapi.com. Prove it on a demo account before you touch live.

ENV
    CT_CLIENT_ID, CT_CLIENT_SECRET   Open API application creds (connect.spotware.com > your app)
    CT_ACCESS_TOKEN                  OAuth access token (trading scope)
    CT_ACCOUNT_ID                    ctidTraderAccountId (numeric). BLANK -> bridge queries + logs it, use that.
    CT_HOST                          'demo' (default) or 'live'
    CT_DRY_RUN                       '1' (default) = don't place, just log. '0' = place for real.
    BRIDGE_SECRET                    shared secret; EXEC_WEBHOOK must be  https://<bridge>/exec?secret=<BRIDGE_SECRET>
    UNITS_PER_LOT (100000)  VOL_SCALE (100)   FX major: 100000 units/lot, API volume = units*100
    SYMBOL_MAP                       optional hardcode e.g. "EURUSD=1,USDJPY=4"; else queried live

STATUS: order-translation + safety rails are unit-tested. The live socket path follows Spotware's
official OpenApiPy example; it MUST be proven with /selftest on a demo account before CT_DRY_RUN=0.
Token refresh, order cancel/exit, and reconnect-under-load are v1.6.x follow-ups (marked TODO below).
"""
import os, json, threading, time

# ---- Open API SDK is optional at import time so the pure logic stays unit-testable without it ----
try:
    from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAApplicationAuthReq, ProtoOAAccountAuthReq,
        ProtoOAGetAccountListByAccessTokenReq, ProtoOASymbolsListReq, ProtoOANewOrderReq)
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderType, ProtoOATradeSide)
    from twisted.internet import reactor
    _SDK = True
except Exception as _e:                      # package not installed (e.g. in a test env)
    _SDK = False
    _SDK_ERR = str(_e)

try:
    from flask import Flask, request, jsonify
except Exception:
    Flask = None


# ========================= PURE TRANSLATION (unit-tested, no network) =========================

def _dec_for(price):
    """Forex price precision — mirror of manage._dec_for / agent._exec_dec (JPY 3dp, else 5dp)."""
    a = abs(float(price))
    return 3 if a >= 10 else 5

def build_order(payload):
    """Translate the executor's TradersPost-style bracket into cTrader order params.
    Input (same schema agent._exec_order sends):
        {ticker, action:'buy'|'sell', orderType:'limit', limitPrice, quantity(lots),
         takeProfit:{limitPrice}, stopLoss:{stopPrice}}
    Returns a plain dict (no SDK types) so it can be tested and logged."""
    sym = str(payload.get('ticker', '')).upper().replace('/', '').replace('.PRO', '').replace('.', '')
    side = 'BUY' if str(payload.get('action', '')).lower() == 'buy' else 'SELL'
    lots = float(payload.get('quantity') or 0)
    units_per_lot = float(os.environ.get('UNITS_PER_LOT', '100000'))
    vol_scale = float(os.environ.get('VOL_SCALE', '100'))
    volume = int(round(lots * units_per_lot * vol_scale))           # cTrader API volume (units*100)
    limit = payload.get('limitPrice')
    d = _dec_for(limit if limit is not None else 1)
    sl = (payload.get('stopLoss') or {}).get('stopPrice')
    tp = (payload.get('takeProfit') or {}).get('limitPrice')
    return dict(
        symbol=sym, side=side, lots=lots, volume=volume,
        order_type='LIMIT' if str(payload.get('orderType', 'limit')).lower() == 'limit' else 'MARKET',
        limitPrice=round(float(limit), d) if limit is not None else None,
        stopLoss=round(float(sl), d) if sl is not None else None,
        takeProfit=round(float(tp), d) if tp is not None else None,
    )


# ============================== LIVE cTrader CLIENT (Twisted) ==============================

class Bridge:
    def __init__(self):
        self.ready = False
        self.account_id = None
        self.symbols = {}                 # 'EURUSD' -> symbolId
        self.last = {}                    # last decision, for /health
        self.client = None

    # ---- helpers ----
    def _host(self):
        live = os.environ.get('CT_HOST', 'demo').lower() == 'live'
        return (EndPoints.PROTOBUF_LIVE_HOST if live else EndPoints.PROTOBUF_DEMO_HOST)

    def _seed_symbol_map(self):
        raw = os.environ.get('SYMBOL_MAP', '').strip()
        for part in raw.split(','):
            if '=' in part:
                k, v = part.split('=', 1)
                try: self.symbols[k.strip().upper()] = int(v)
                except Exception: pass

    # ---- Twisted lifecycle (runs in a background thread) ----
    def start(self):
        if not _SDK:
            print('[ctrader] SDK not installed:', _SDK_ERR, flush=True); return
        self._seed_symbol_map()
        self.client = Client(self._host(), EndPoints.PROTOBUF_PORT, TcpProtocol)
        self.client.setConnectedCallback(self._on_connected)
        self.client.setDisconnectedCallback(self._on_disconnected)
        self.client.setMessageReceivedCallback(self._on_message)
        self.client.startService()
        reactor.run(installSignalHandlers=False)          # we're in a worker thread

    def _on_connected(self, client):
        req = ProtoOAApplicationAuthReq()
        req.clientId = os.environ.get('CT_CLIENT_ID', '')
        req.clientSecret = os.environ.get('CT_CLIENT_SECRET', '')
        d = client.send(req); d.addErrback(self._err)
        print('[ctrader] connected -> app auth sent', flush=True)

    def _on_disconnected(self, client, reason):
        self.ready = False
        print('[ctrader] disconnected:', reason, flush=True)
        # TODO v1.6.x: exponential-backoff reconnect (reactor.callLater) + token refresh on expiry.

    def _account_auth(self):
        acc = os.environ.get('CT_ACCOUNT_ID', '').strip()
        if acc:
            self.account_id = int(acc)
            req = ProtoOAAccountAuthReq()
            req.ctidTraderAccountId = self.account_id
            req.accessToken = os.environ.get('CT_ACCESS_TOKEN', '')
            self.client.send(req).addErrback(self._err)
        else:                                             # discover the account id from the token
            req = ProtoOAGetAccountListByAccessTokenReq()
            req.accessToken = os.environ.get('CT_ACCESS_TOKEN', '')
            self.client.send(req).addErrback(self._err)

    def _on_message(self, client, message):
        try:
            msg = Protobuf.extract(message)
        except Exception:
            return
        name = type(msg).__name__
        if name == 'ProtoOAApplicationAuthRes':
            print('[ctrader] app authed -> account auth', flush=True); self._account_auth()
        elif name == 'ProtoOAGetAccountListByAccessTokenRes':
            ids = [a.ctidTraderAccountId for a in msg.ctidTraderAccount]
            print('[ctrader] accounts for this token:', ids, '(set CT_ACCOUNT_ID to one of these)', flush=True)
            if ids:
                self.account_id = ids[0]
                req = ProtoOAAccountAuthReq()
                req.ctidTraderAccountId = self.account_id
                req.accessToken = os.environ.get('CT_ACCESS_TOKEN', '')
                self.client.send(req).addErrback(self._err)
        elif name == 'ProtoOAAccountAuthRes':
            print('[ctrader] account authed:', self.account_id, '-> loading symbols', flush=True)
            req = ProtoOASymbolsListReq(); req.ctidTraderAccountId = self.account_id
            self.client.send(req).addErrback(self._err)
        elif name == 'ProtoOASymbolsListRes':
            for s in msg.symbol:
                self.symbols[s.symbolName.upper().replace('/', '')] = s.symbolId
            self.ready = True
            print('[ctrader] READY. symbols loaded:', len(self.symbols), flush=True)
        elif name == 'ProtoOAErrorRes':
            print('[ctrader] ERROR:', getattr(msg, 'description', msg), flush=True)

    def _err(self, failure):
        print('[ctrader] send error:', failure, flush=True)

    # ---- order placement ----
    def place(self, payload):
        o = build_order(payload)
        sym_id = self.symbols.get(o['symbol'])
        dry = os.environ.get('CT_DRY_RUN', '1') != '0'
        decision = dict(order=o, symbolId=sym_id, dry_run=dry, ready=self.ready, ts=int(time.time()))
        self.last = decision
        if not o['symbol'] or o['volume'] <= 0:
            decision['result'] = 'reject: bad symbol/volume'; return decision
        if sym_id is None:
            decision['result'] = f"reject: symbol {o['symbol']} not in map (loaded={bool(self.symbols)})"; return decision
        if dry:
            decision['result'] = 'DRY_RUN — not sent'
            print('[ctrader] DRY_RUN order:', json.dumps(decision), flush=True)
            return decision
        if not (self.ready and _SDK):
            decision['result'] = 'reject: bridge not ready'; return decision
        reactor.callFromThread(self._send_order, o, sym_id)       # hop onto the reactor thread
        decision['result'] = 'sent'
        print('[ctrader] SENT order:', json.dumps(decision), flush=True)
        return decision

    def _send_order(self, o, sym_id):
        try:
            req = ProtoOANewOrderReq()
            req.ctidTraderAccountId = self.account_id
            req.symbolId = sym_id
            req.orderType = ProtoOAOrderType.LIMIT if o['order_type'] == 'LIMIT' else ProtoOAOrderType.MARKET
            req.tradeSide = ProtoOATradeSide.BUY if o['side'] == 'BUY' else ProtoOATradeSide.SELL
            req.volume = int(o['volume'])
            if o['order_type'] == 'LIMIT' and o['limitPrice'] is not None:
                req.limitPrice = float(o['limitPrice'])
            if o['stopLoss'] is not None:
                req.stopLoss = float(o['stopLoss'])          # absolute price; verify vs relativeStopLoss on demo
            if o['takeProfit'] is not None:
                req.takeProfit = float(o['takeProfit'])
            self.client.send(req).addErrback(self._err)
        except Exception as ex:
            print('[ctrader] _send_order err:', ex, flush=True)


BRIDGE = Bridge()


# ================================== FLASK (webhook + ops) ==================================

def create_app():
    app = Flask(__name__)

    @app.route('/exec', methods=['POST'])
    def _exec():
        if request.args.get('secret', '') != os.environ.get('BRIDGE_SECRET', '__unset__'):
            return jsonify(error='bad or missing ?secret='), 401
        payload = request.get_json(force=True, silent=True) or {}
        return jsonify(BRIDGE.place(payload))

    @app.route('/selftest')
    def _selftest():
        """Prove the connection WITHOUT trading: are we authed + symbols loaded?"""
        return jsonify(sdk_installed=_SDK, ready=BRIDGE.ready, account_id=BRIDGE.account_id,
                       symbols_loaded=len(BRIDGE.symbols),
                       host=os.environ.get('CT_HOST', 'demo'), dry_run=os.environ.get('CT_DRY_RUN', '1') != '0',
                       hint='ready:true + symbols_loaded>0 = connection good. Then dry-run an order, then CT_DRY_RUN=0.')

    @app.route('/health')
    def _health():
        return jsonify(ok=True, sdk=_SDK, ready=BRIDGE.ready, dry_run=os.environ.get('CT_DRY_RUN', '1') != '0',
                       host=os.environ.get('CT_HOST', 'demo'), last=BRIDGE.last)
    return app


if _SDK and Flask is not None:
    threading.Thread(target=BRIDGE.start, daemon=True).start()   # cTrader socket in the background
    app = create_app()                                           # gunicorn entrypoint:  ctrader_bridge:app
elif Flask is not None:
    app = create_app()                                           # runs, /selftest reports sdk_installed:false


if __name__ == '__main__':
    # local smoke test of the pure translation (no network, no SDK needed)
    demo = dict(ticker='EURUSD', action='buy', orderType='limit', limitPrice=1.08505,
                quantity=0.5, takeProfit={'limitPrice': 1.08805}, stopLoss={'stopPrice': 1.08355})
    print(json.dumps(build_order(demo), indent=2))
