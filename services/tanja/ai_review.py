"""Bounded OpenAI context reviews. Never imports a broker or sends orders."""
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent / 'vendor'))
from context_v3 import output_schema, validate_decision, check_packet
from context_api import extract_response

NY = ZoneInfo('America/New_York')
PROMPT = (Path(__file__).resolve().parent/'vendor'/'prompt_v3.txt').read_text()
MAX_REQUEST_BYTES = 500_000


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class APIError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def call_openai(payload, key):
    request = urllib.request.Request('https://api.openai.com/v1/responses',
        data=encoded(payload).encode(), method='POST',
        headers={'Authorization':'Bearer '+key, 'Content-Type':'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
            body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise APIError('RESPONSE_TOO_LARGE')
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        # The upstream error body and request headers may contain sensitive data.
        raise APIError('HTTP_'+str(exc.code)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise APIError('NETWORK_OUTCOME_UNKNOWN') from None
    except (ValueError, UnicodeError):
        raise APIError('INVALID_RESPONSE_BODY') from None


class AIReview:
    def __init__(self, store, config):
        self.store = store
        self.key = config.get('OPENAI_API_KEY', '').strip()
        self.model = config.get('OPENAI_MODEL', '').strip()
        self.enabled = config.get('TANJA_AI_ENABLED', 'false').lower() == 'true'
        self.config_error = False
        try:
            self.daily = int(config.get('TANJA_AI_MAX_CALLS_PER_DAY', '6'))
            self.interval = int(config.get('TANJA_AI_MIN_INTERVAL_MINUTES', '15')) * 60
            self.tokens = int(config.get('TANJA_AI_MAX_OUTPUT_TOKENS', '8000'))
            self.revision = config.get('TANJA_AI_REVISION', '1')
            if not (1 <= self.daily <= 24 and 900 <= self.interval <= 86400 and 2000 <= self.tokens <= 12000):
                raise ValueError()
            if not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', self.revision):
                raise ValueError()
            if self.model and not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', self.model):
                raise ValueError()
        except (ValueError, TypeError):
            self.config_error = True
            self.daily, self.interval, self.tokens, self.revision = 6, 900, 8000, 'invalid'
        self.fingerprint = digest(dict(model=self.model, revision=self.revision,
            prompt=PROMPT, schema=output_schema(), tokens=self.tokens))
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS ai_runs (
                  id TEXT PRIMARY KEY, packet_id TEXT UNIQUE, day TEXT, config TEXT,
                  cutoff INTEGER, started REAL, finished REAL, status TEXT, error TEXT,
                  snapshot TEXT, request TEXT, raw TEXT, decision TEXT, review TEXT);
                CREATE INDEX IF NOT EXISTS ai_runs_day ON ai_runs(day);
            ''')

    def recover(self):
        # A request may have been billed. Never retry an ambiguous interrupted call.
        with self.store.connect() as db:
            db.execute("UPDATE ai_runs SET status='interrupted',error='PROCESS_INTERRUPTED_OUTCOME_UNKNOWN' WHERE status='running'")

    def gate(self, now, context):
        if not self.enabled: return 'DISABLED'
        if self.config_error: return 'CONFIG_ERROR'
        if not self.key: return 'MISSING_API_KEY'
        if not self.model: return 'MISSING_MODEL'
        if not context: return 'WAITING_FOR_PAIRED_DATA'
        p = context['packet']
        if not (0 <= now-p['as_of'] <= 120) or context['frozen_at'] > now or context['processed_at'] > now:
            return 'WAITING_FOR_FRESH_DATA'
        if any(context['coverage'][s]['240'] < 3 or context['coverage'][s]['60'] < 3 for s in ('ES','MNQ')):
            return 'WAITING_FOR_HISTORY'
        for s in ('ES', 'MNQ'):
            recent = self.store.history(s, p['as_of'], context['frozen_at'])[-15:]
            ends = [b['bar_close_ms']//1000 for b in recent]
            if ends != list(range(p['as_of']-14*60, p['as_of']+1, 60)):
                return 'WAITING_FOR_CONTIGUOUS_DATA'
        local = datetime.fromtimestamp(now, NY)
        # Sampling window for this observation pilot, not a reconstructed entry rule.
        if local.weekday() >= 5 or not (570 <= local.hour*60+local.minute < 660):
            return 'OUTSIDE_REVIEW_WINDOW'
        if len(encoded(self.make_payload(p)).encode()) > MAX_REQUEST_BYTES:
            return 'INPUT_TOO_LARGE'
        return 'READY'

    def latest_context(self):
        with self.store.connect() as db:
            row = db.execute('SELECT packet FROM jobs WHERE packet IS NOT NULL AND status=\'done\' ORDER BY cutoff DESC LIMIT 1').fetchone()
        return json.loads(row[0]) if row else None

    def limits(self, db, now, packet_id=None):
        day = datetime.fromtimestamp(now, NY).date().isoformat()
        if db.execute("SELECT 1 FROM ai_runs WHERE status='running'").fetchone(): return 'REVIEW_IN_PROGRESS'
        if db.execute("SELECT 1 FROM ai_runs WHERE config=? AND status IN ('failed','rejected','interrupted')", (self.fingerprint,)).fetchone():
            return 'PAUSED_AFTER_ERROR'
        if db.execute('SELECT COUNT(*) FROM ai_runs WHERE day=?', (day,)).fetchone()[0] >= self.daily:
            return 'DAILY_LIMIT_REACHED'
        last = db.execute('SELECT MAX(started) FROM ai_runs').fetchone()[0]
        if last is not None and now-last < self.interval: return 'WAITING_FOR_INTERVAL'
        if packet_id and db.execute('SELECT 1 FROM ai_runs WHERE packet_id=?', (packet_id,)).fetchone(): return 'ALREADY_REVIEWED'
        return 'READY'

    def reserve(self, context, payload, now):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.limits(db, now, context['packet']['packet_id']) != 'READY': return None
            ident = uuid.uuid4().hex
            db.execute('INSERT INTO ai_runs(id,packet_id,day,config,cutoff,started,status,snapshot,request) VALUES(?,?,?,?,?,?,?,?,?)',
                (ident, context['packet']['packet_id'], datetime.fromtimestamp(now, NY).date().isoformat(),
                 self.fingerprint, context['packet']['as_of'], now, 'running', encoded(context), encoded(payload)))
            return ident

    def make_payload(self, packet):
        return dict(model=self.model, store=False, max_output_tokens=self.tokens,
            reasoning={'effort':'low'},
            input=[dict(role='system',content=PROMPT), dict(role='user',content=encoded(packet))],
            text={'format':dict(type='json_schema', name='tanja_context_v3', strict=True, schema=output_schema())})

    def process_one(self, transport=call_openai, clock=time.time):
        now = clock()
        context = self.latest_context()
        if self.gate(now, context) != 'READY': return False
        p = context['packet']
        check_packet(p)
        payload = self.make_payload(p)
        if len(encoded(payload).encode()) > MAX_REQUEST_BYTES: return False
        ident = self.reserve(context, payload, now)
        if not ident: return False
        raw = decision = review = None
        error = None
        status = 'failed'
        try:
            raw = transport(payload, self.key)
            decision = extract_response(raw)
            review = validate_decision(p, decision)
            status = 'validated'
        except APIError as exc:
            error = str(exc) if re.fullmatch(r'[A-Z_0-9]+', str(exc)) else 'API_ERROR'
        except ValueError as exc:
            # Only an exact, known validator message is exposed. Never forward
            # arbitrary upstream response text or credentials into diagnostics.
            error = ('TRIGGER_BIAS_MISMATCH' if raw is not None and
                str(exc) == 'Trigger does not support the selected MNQ inversion direction'
                else 'RESPONSE_FAILED_EVIDENCE_VALIDATION' if raw is not None
                else 'REQUEST_OUTCOME_UNKNOWN')
            status = 'rejected' if raw is not None else 'failed'
        except Exception:
            error = 'RESPONSE_FAILED_EVIDENCE_VALIDATION' if raw is not None else 'REQUEST_OUTCOME_UNKNOWN'
            status = 'rejected' if raw is not None else 'failed'
        finished = clock()
        with self.store.connect() as db:
            db.execute('UPDATE ai_runs SET finished=?,status=?,error=?,raw=?,decision=?,review=? WHERE id=?',
                (finished, status, error, encoded(raw), encoded(decision), encoded(review), ident))
        return True

    def state(self, now, context):
        with self.store.connect() as db:
            used = db.execute('SELECT COUNT(*) FROM ai_runs WHERE day=?', (datetime.fromtimestamp(now, NY).date().isoformat(),)).fetchone()[0]
            records = [dict(r) for r in db.execute('SELECT id,packet_id,cutoff,started,finished,status,error,raw,decision,review FROM ai_runs ORDER BY started DESC LIMIT 20')]
            status = self.gate(now, context)
            # Surface errors/inflight even outside the review window.
            limits = self.limits(db, now, context['packet']['packet_id'] if context else None)
            if self.enabled and not self.config_error and self.key and self.model and (status == 'READY' or limits in ('PAUSED_AFTER_ERROR','REVIEW_IN_PROGRESS')):
                status = limits
        for r in records:
            raw = json.loads(r.pop('raw') or 'null') or {}
            raw = raw if isinstance(raw, dict) else {}
            r['usage'], r['returned_model'] = raw.get('usage'), raw.get('model')
            for field in ('decision','review'): r[field] = json.loads(r[field] or 'null')
            r.update(executable=False, available_at=r['finished'] if r['status']=='validated' else None)
        return dict(status=status, model=self.model or None, key_configured=bool(self.key),
            connected=any(r['status']=='validated' for r in records),
            calls_today=used, daily_limit=self.daily, min_interval_minutes=self.interval//60,
            review_window='09:30–11:00 New York, weekdays', records=records,
            max_output_tokens=self.tokens, executable=False,
            fidelity='Research interpretation; Tanja 1:1 fidelity is not established')

    def audit(self, ident):
        with self.store.connect() as db:
            r = db.execute('SELECT * FROM ai_runs WHERE id=?', (ident,)).fetchone()
        if not r: return None
        r = dict(r)
        for key in ('snapshot','request','raw','decision','review'): r[key] = json.loads(r[key] or 'null')
        return dict(**r, executable=False, prompt_sha256=digest(r['request']['input'][0]['content']),
            available_at=r['finished'] if r['status']=='validated' else None,
            note='The market cutoff is not the response availability time. No broker action is implemented.')

    def run(self, stop):
        self.recover()
        while not stop.is_set():
            try:
                self.process_one()
            except Exception:
                # Do not print payloads, headers or credentials in logs.
                self.store.note('ai_worker', 'Review worker error; inspect AI status and audit')
            stop.wait(10)
