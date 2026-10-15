"""Local speech-entry previews and bounded TradersPost TEST delivery. No live sender."""
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.request

from connection_test import NoRedirect, TransportError, URL_PATTERN, TICKER_PATTERN
from transcript_ingestion import encoded
from transcript_trade_interpreter import numeric_fields

ENTRY_ACTIONS = {'MARKET_ORDER': 'market', 'LIMIT_ORDER_PLACED': 'limit'}
MAX_AGE = 15
ORIGIN = 'tanja-transcript-entry-test-v1'


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace('+00:00', 'Z')


def valid_price(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0 and value * 4 == round(value * 4)


def validate_test_payload(payload, now=None):
    """Recheck the network boundary; caller/config cannot enable broker orders."""
    required = {'ticker', 'action', 'sentiment', 'orderType', 'quantity', 'quantityType',
                'signalPrice', 'stopLoss', 'test', 'time', 'expiresAt', 'rejectAfter', 'message', 'extras'}
    optional = {'limitPrice', 'takeProfit'}
    if not isinstance(payload, dict) or not required <= set(payload) or set(payload) - required - optional:
        raise ValueError('INVALID_TEST_PAYLOAD_FIELDS')
    if (payload['test'] is not True or payload['quantity'] != 1 or type(payload['quantity']) is not int
            or payload['quantityType'] != 'fixed_quantity'
            or not re.fullmatch(TICKER_PATTERN, payload['ticker'])
            or payload['action'] not in ('buy', 'sell') or payload['orderType'] not in ('market', 'limit')
            or payload['sentiment'] != ('bullish' if payload['action'] == 'buy' else 'bearish')
            or payload['rejectAfter'] != MAX_AGE
            or payload['message'] != 'TANJA TRANSCRIPT ENTRY TEST ONLY — NO BROKER ORDERS'
            or not isinstance(payload['extras'], dict) or payload['extras'].get('source') != ORIGIN
            or payload['extras'].get('source_kind') not in ('macos_device', 'synthetic_fixture')
            or not re.fullmatch(r'[0-9a-f]{64}', payload['extras'].get('event_id', ''))):
        raise ValueError('ONLY_ONE_CONTRACT_TEST_ENTRIES_SUPPORTED')
    entry = payload['signalPrice']
    stop = payload['stopLoss']
    sign = 1 if payload['action'] == 'buy' else -1
    if not valid_price(entry) or not isinstance(stop, dict) or set(stop) != {'type', 'stopPrice'}:
        raise ValueError('INVALID_ENTRY_OR_STOP')
    if stop['type'] != 'stop' or not valid_price(stop['stopPrice']) or (entry - stop['stopPrice']) * sign <= 0:
        raise ValueError('INVALID_STOP_DIRECTION_OR_TICK')
    if payload['orderType'] == 'limit':
        if not valid_price(payload.get('limitPrice')) or payload['limitPrice'] != entry:
            raise ValueError('EXPLICIT_LIMIT_PRICE_REQUIRED')
    elif 'limitPrice' in payload:
        raise ValueError('MARKET_CANNOT_HAVE_LIMIT_PRICE')
    if 'takeProfit' in payload:
        target = payload['takeProfit']
        if not isinstance(target, dict) or set(target) != {'limitPrice'} or not valid_price(target['limitPrice']) or (target['limitPrice'] - entry) * sign <= 0:
            raise ValueError('INVALID_TARGET_DIRECTION_OR_TICK')
    try:
        generated = datetime.fromisoformat(payload['time'].replace('Z', '+00:00'))
        expires = datetime.fromisoformat(payload['expiresAt'].replace('Z', '+00:00'))
        if generated.tzinfo is None or expires.tzinfo is None or (expires-generated).total_seconds() != MAX_AGE:
            raise ValueError()
        age = (time.time() if now is None else now) - generated.timestamp()
        if not 0 <= age <= MAX_AGE:
            raise ValueError('STALE_OR_FUTURE_TEST_PAYLOAD')
    except (TypeError, AttributeError):
        raise ValueError('INVALID_TEST_TIMESTAMPS') from None


def send_entry_test(url, payload):
    if not re.fullmatch(URL_PATTERN, url):
        raise ValueError('INVALID_TEST_WEBHOOK_URL')
    validate_test_payload(payload)
    request = urllib.request.Request(url, data=encoded(payload).encode(), method='POST',
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise TransportError('RESPONSE_TOO_LARGE')
            result = json.loads(raw)
            if not isinstance(result, dict) or type(result.get('success')) is not bool:
                raise TransportError('UNRECOGNIZED_RESPONSE')
            receipt = {'success': result['success']}
            for key in ('id', 'logId', 'messageCode'):
                value = result.get(key)
                if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
                    receipt[key] = value
            return receipt
    except urllib.error.HTTPError as exc:
        # Keep a sanitized rejection code when the API supplies one; never URL/body text.
        try:
            result = json.loads(exc.read(65536))
            code = result.get('messageCode', '')
            if result.get('success') is False and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', code):
                return dict(success=False, messageCode=code, http_status=exc.code)
        except Exception:
            pass
        raise TransportError('HTTP_' + str(exc.code)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise TransportError('NETWORK_OUTCOME_UNKNOWN') from None
    except (TypeError, ValueError, UnicodeError):
        raise TransportError('UNRECOGNIZED_RESPONSE') from None


class EntryBridge:
    def __init__(self, journal, contract='', webhook=''):
        self.journal, self.store = journal, journal.store
        self.contract, self.webhook = contract, webhook
        self.destination = hashlib.sha256(webhook.encode()).hexdigest()
        with self.store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS transcript_entry_previews (
                  session_id TEXT, event_id TEXT, recorded_at REAL, payload TEXT,
                  PRIMARY KEY(session_id,event_id));
                CREATE TABLE IF NOT EXISTS transcript_entry_test_deliveries (
                  session_id TEXT, event_id TEXT, destination TEXT, started_at REAL,
                  finished_at REAL, status TEXT, error TEXT, receipt TEXT,
                  PRIMARY KEY(session_id,event_id));
            ''')

    def preview(self, session, item, event, captured_at, source_kind, now=None):
        now = time.time() if now is None else now
        event_id = event['event_id']
        with self.store.connect() as db:
            prior = db.execute('SELECT payload FROM transcript_entry_previews WHERE session_id=? AND event_id=?',
                               (session, event_id)).fetchone()
            if prior:
                return dict(json.loads(prior[0]), duplicate=True)
        reasons = []
        fields = numeric_fields(event['quote'])  # Ground fields in the current exact quote.
        entry = event.get('action') in ENTRY_ACTIONS and not event.get('reported_fill')
        if not entry:
            reasons.append('FILL_REPORT_IS_NOT_OUR_ENTRY' if event.get('reported_fill') else 'NOT_AN_ENTRY')
        else:
            if event.get('evidence_status') != 'SELF_REPORTED':
                reasons.append('NOT_CURRENT_REPORTED_ENTRY')
            if re.search(r"\b(?:if|might|would|could|going to|gonna|want to|like to|thinking|debating|i'll|not|never|don't|didn't|haven't|cannot|can't)\b", event['quote'].lower().replace('’', "'")):
                reasons.append('INTENTION_CONDITION_OR_NEGATION')
            if event.get('transition_error'):
                reasons.append(event['transition_error'])
            if event.get('fields') != fields:
                reasons.append('FIELDS_NOT_GROUNDED_IN_CURRENT_QUOTE')
            if source_kind not in ('macos_device', 'synthetic_fixture') or event.get('historical_replay') is not False:
                reasons.append('HISTORICAL_SOURCE_NOT_SENDABLE')
            for key in ('instrument', 'side', 'quantity', 'entry', 'stop'):
                if fields[key] == 'UNKNOWN':
                    reasons.append('MISSING_' + key.upper())
            if fields['instrument'] not in ('MNQ', 'UNKNOWN'):
                reasons.append('INSTRUMENT_NOT_MNQ_NO_AUTOMATIC_CONVERSION')
            if not re.fullmatch(TICKER_PATTERN, self.contract):
                reasons.append('EXPLICIT_MNQ_TEST_CONTRACT_REQUIRED')
            if fields['quantity'] != 'UNKNOWN' and fields['quantity'] != 1:
                reasons.append('TEST_QUANTITY_MUST_BE_ONE_NO_AUTOMATIC_RESIZING')
            if fields['side'] not in ('LONG', 'SHORT', 'UNKNOWN'):
                reasons.append('INVALID_SIDE')
            if type(captured_at) not in (int, float) or not math.isfinite(captured_at) or not 0 <= now-captured_at <= MAX_AGE:
                reasons.append('STALE_FUTURE_OR_UNKNOWN_CAPTURE_TIME')
        payload = None
        if entry and not reasons:
            long = fields['side'] == 'LONG'
            payload = dict(ticker=self.contract, action='buy' if long else 'sell',
                sentiment='bullish' if long else 'bearish', orderType=ENTRY_ACTIONS[event['action']],
                quantity=fields['quantity'], quantityType='fixed_quantity', signalPrice=fields['entry'],
                stopLoss=dict(type='stop', stopPrice=fields['stop']), test=True,
                time=iso(captured_at), expiresAt=iso(captured_at+MAX_AGE), rejectAfter=MAX_AGE,
                message='TANJA TRANSCRIPT ENTRY TEST ONLY — NO BROKER ORDERS',
                extras=dict(source=ORIGIN, source_kind=source_kind, event_id=event_id,
                            session_id=session, evidence_status=event['evidence_status']))
            if payload['orderType'] == 'limit':
                payload['limitPrice'] = fields['entry']
            if fields['target'] != 'UNKNOWN':
                payload['takeProfit'] = dict(limitPrice=fields['target'])
            try:
                validate_test_payload(payload, now)
            except ValueError as exc:
                reasons.append(str(exc)); payload = None
        result = dict(event_id=event_id, session_id=session, action=event['action'], quote=event['quote'],
            source_kind=source_kind, captured_at=captured_at, recorded_at=now,
            status='READY_TEST_PAYLOAD' if payload else 'BLOCKED' if entry else 'OBSERVATION_ONLY',
            reasons=reasons, payload=payload, orders_enabled=False, test_only=True,
            account_sizing_verified=False, broker_fill_verified=False)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO transcript_entry_previews VALUES(?,?,?,?)',
                       (session, event_id, now, encoded(result)))
            result = json.loads(db.execute('SELECT payload FROM transcript_entry_previews WHERE session_id=? AND event_id=?',
                                          (session,event_id)).fetchone()[0])
            row = db.execute('SELECT payload FROM shadow_transcript_observations WHERE session_id=? AND item_id=?', (session,item)).fetchone()
            if not row:
                raise ValueError('EXISTING_SHADOW_OBSERVATION_REQUIRED')
            observation = json.loads(row[0])
            observations = observation.setdefault('entry_bridge', [])
            if not any(x['event_id'] == event_id for x in observations):
                observations.append(result)
            db.execute('UPDATE shadow_transcript_observations SET payload=? WHERE session_id=? AND item_id=?',
                       (encoded(observation), session, item))
        print('[ENTRY BRIDGE %s] %s' % (result['status'], encoded(payload or reasons)), flush=True)
        return result

    def deliver_test(self, session, event_id, transport=send_entry_test, now=None):
        now = time.time() if now is None else now
        if not re.fullmatch(URL_PATTERN, self.webhook):
            raise ValueError('DEDICATED_TEST_WEBHOOK_REQUIRED')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM transcript_entry_test_deliveries WHERE session_id=? AND event_id=?', (session,event_id)).fetchone()
            if old:
                if old['destination'] != self.destination:
                    raise ValueError('TEST_DESTINATION_CONFLICT')
                return self.public(old)  # Any outcome is immutable; no network retry.
            row = db.execute('SELECT payload FROM transcript_entry_previews WHERE session_id=? AND event_id=?', (session,event_id)).fetchone()
            if not row or json.loads(row[0])['status'] != 'READY_TEST_PAYLOAD':
                raise ValueError('VALIDATED_TEST_PREVIEW_REQUIRED')
            payload = json.loads(row[0])['payload']
            validate_test_payload(payload, now)
            attempts = db.execute('SELECT started_at,status FROM transcript_entry_test_deliveries WHERE started_at>?', (now-86400,)).fetchall()
            if any(x['status'] == 'sending' for x in attempts) or any(now-x['started_at'] < 60 for x in attempts) or len(attempts) >= 5:
                raise ValueError('TEST_RATE_LIMIT_OR_UNKNOWN_IN_FLIGHT_DELIVERY')
            db.execute('INSERT INTO transcript_entry_test_deliveries VALUES(?,?,?,?,?,?,?,?)',
                       (session,event_id,self.destination,now,None,'sending',None,None))
        receipt = None; error = None; status = 'unknown'
        try:
            receipt = transport(self.webhook, payload)
            status = 'test_received' if receipt.get('success') is True else 'test_rejected'
        except TransportError as exc:
            error = str(exc) if re.fullmatch(r'[A-Z_0-9]+', str(exc)) else 'TEST_OUTCOME_UNKNOWN'
        except Exception:
            error = 'TEST_OUTCOME_UNKNOWN'
        with self.store.connect() as db:
            db.execute('UPDATE transcript_entry_test_deliveries SET finished_at=?,status=?,error=?,receipt=? WHERE session_id=? AND event_id=?',
                       (time.time(),status,error,encoded(receipt),session,event_id))
            result = self.public(db.execute('SELECT * FROM transcript_entry_test_deliveries WHERE session_id=? AND event_id=?', (session,event_id)).fetchone())
            for shadow in db.execute('SELECT item_id,payload FROM shadow_transcript_observations WHERE session_id=?', (session,)).fetchall():
                observation = json.loads(shadow['payload'])
                for preview in observation.get('entry_bridge', []):
                    if preview['event_id'] == event_id:
                        preview['test_delivery'] = result
                db.execute('UPDATE shadow_transcript_observations SET payload=? WHERE session_id=? AND item_id=?', (encoded(observation),session,shadow['item_id']))
        return result

    @staticmethod
    def public(row):
        result = dict(row)
        result.pop('destination', None)
        result['receipt'] = json.loads(result['receipt'] or 'null')
        result.update(test_only=True, orders_enabled=False, broker_fill_verified=False,
                      account_routing_verified=False)
        return result
