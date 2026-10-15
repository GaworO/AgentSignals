"""Offline, deterministic speech-decision interpreter. No broker/API dependencies.

PaperLifecycle requires compiled strategy approvals; spoken decisions do not have
those approvals. PaperCopy below is a separate observation-only simulation.
"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time

from risk_sizing import risk_budget, contracts_for_stop
from store import Store
from transcript_ingestion import TranscriptJournal

UNKNOWN = 'UNKNOWN'
ROOT = Path(__file__).resolve().parent
VOCAB = ROOT / 'live_transcription/tanja_vocabulary.json'


def seconds(stamp):
    parts = [int(x) for x in stamp.split(':')]
    if len(parts) != 3 or any(x < 0 for x in parts) or parts[1] > 59 or parts[2] > 59:
        raise ValueError('INVALID_SOURCE_TIMESTAMP')
    return parts[0]*3600 + parts[1]*60 + parts[2]


def statements(path, start=0, end=float('inf')):
    """Timestamped NoteGPT blocks, sentence order; never invent word timestamps."""
    stamp = None
    previous = -1
    for line_no, raw in enumerate(Path(path).read_text().splitlines(), 1):
        if re.fullmatch(r'\d{2}:\d{2}:\d{2}', raw.strip()):
            stamp = raw.strip()
            current = seconds(stamp)
            if current < previous:
                raise ValueError('NON_CHRONOLOGICAL_TRANSCRIPT')
            previous = current
            continue
        if not raw.strip():
            continue
        if stamp is None:
            raise ValueError('TIMESTAMPED_TRANSCRIPT_REQUIRED')
        if not start <= seconds(stamp) <= end:
            continue
        for index, match in enumerate(re.finditer(r'.+?(?:[.!?](?=\s|$)|$)', raw)):
            quote = match.group().strip()
            if quote:
                yield dict(source_timestamp=stamp, source_seconds=seconds(stamp),
                           source_line=line_no, sentence_index=index, quote=quote)


def numeric_fields(text):
    """Only explicit numeric field labels. Reject ranges, approximations, conflicts."""
    fields = {k: UNKNOWN for k in ('instrument', 'side', 'quantity', 'entry', 'stop', 'target')}
    instruments = set(re.findall(r'\b(?:MNQ|NQ|MES|ES)\b', text, re.I))
    if len(instruments) == 1:
        fields['instrument'] = instruments.pop().upper()
    sides = set()
    if re.search(r'\b(?:short(?:ed|s)?|sell)\b', text, re.I): sides.add('SHORT')
    if re.search(r'\b(?:long(?:s)?|buy)\b', text, re.I): sides.add('LONG')
    if len(sides) == 1: fields['side'] = sides.pop()
    patterns = {
        'quantity': r'\b(?:quantity\s*(?:is|of|=)?|(?:buy|sell|long|short))\s+(\d+)\b',
        'entry': r'\b(?:entry|limit(?:\s+price)?|market(?:\s+order)?)\s*(?:is|at|to|=)\s*(\d+(?:\.\d+)?)',
        'stop': r'\b(?:stop(?:[- ]loss)?|SL)\s*(?:is|at|to|=|to\s+be\s+at)\s*(\d+(?:\.\d+)?)',
        'target': r'\b(?:target|TP|take[- ]profit)\s*(?:is|at|to|=)\s*(\d+(?:\.\d+)?)',
    }
    for name, pattern in patterns.items():
        hits = list(re.finditer(pattern, text, re.I))
        values = set()
        ambiguous = bool(re.search(r'\b(?:about|around|roughly|maybe|approximately|or)\b', text, re.I))
        for hit in hits:
            suffix = text[hit.end():]
            if re.match(r'(?:\s*(?:-|to)\s*\d|s\b|\s*%|:\d)', suffix): ambiguous = True
            value = float(hit.group(1))
            if math.isfinite(value) and value > 0: values.add(value)
        if len(values) == 1 and not ambiguous:
            value = values.pop()
            fields[name] = int(value) if name == 'quantity' else value
    qty = re.findall(r'\b(\d+)\s+contracts?\b', text, re.I)
    if len(set(qty)) == 1 and not re.search(r'\b(?:about|around|maybe|roughly)\b', text, re.I):
        fields['quantity'] = int(qty[0]) if int(qty[0]) > 0 else UNKNOWN
    return fields


class Interpreter:
    def __init__(self, vocabulary=VOCAB):
        self.vocabulary = json.loads(Path(vocabulary).read_text())
        self.vocab_hash = hashlib.sha256(Path(vocabulary).read_bytes()).hexdigest()
        self.state = 'NO_POSITION'
        self.order_type = UNKNOWN
        self.fields = {k: UNKNOWN for k in numeric_fields('')}
        self.episode = 0

    def interpret(self, statement, source_hash):
        quote = statement['quote']
        text = re.sub(r'\s+', ' ', quote).lower().replace('’', "'")
        # Reports about other days, demonstrations and hypothetical fills are observations only.
        retrospective = bool(re.search(r'\b(?:yesterday|last week|for my first trade|example|demo)\b', text))
        intended = bool(re.search(r"\b(?:if|might|would|could|going to|gonna|want to|like to|thinking|debating|i'll)\b", text))
        negated = bool(re.search(r"\b(?:not|never|don't|didn't|haven't|hasn't|cannot|can't)\b", text))
        found = []
        def add(kind, reported=False):
            if kind not in [x[0] for x in found]: found.append((kind, reported))
        limit_place = bool(re.search(r'\b(?:i have my limit in|i (?:placed|put|submitted) (?:a |my |the )?limit(?: order)?)\b', text))
        if limit_place:
            add('LIMIT_ORDER_PLACED', not retrospective and not negated)
        elif re.search(r'\blimit\b', text) and re.search(r'\b(?:place|put|submit|have)\b', text):
            add('TRADE_PLAN')
        if re.search(r'\bi (?:clicked|submitted|placed) (?:a |the )?market(?: order)?\b', text):
            add('MARKET_ORDER', not intended and not negated and not retrospective)
        if re.search(r'\b(?:cancel(?:led|ed)?|cancelling)\b.*\blimit\b|\blimit\b.*\bcancel(?:led|ed)?\b', text):
            add('LIMIT_ORDER_CANCELLED', not intended and not negated and not retrospective)
        if re.search(r'\b(?:mov(?:ed|ing)|chang(?:ed|ing)|rais(?:ed|ing)|lower(?:ed|ing))\b.*\blimit\b', text):
            add('LIMIT_ORDER_MODIFIED', not intended and not negated and not retrospective)
        fill = bool(re.search(r'\bi (?:(?:just|have) )?(?:got filled|was filled|entered|shorted|went long)\b|\bmy limit (?:just )?filled\b', text))
        if fill and not negated and not intended and not retrospective:
            add('LIMIT_ORDER_FILLED' if self.order_type == 'LIMIT' else 'MARKET_ORDER' if self.order_type == 'MARKET' else 'TRADE_PLAN', True)
        if re.search(r'\bstop(?:[- ]loss)?\b', text) and re.search(r'\b(?:put|set|placed|moved|moving|lowered|raised|leave)\b', text):
            moved = bool(re.search(r'\b(?:moved|moving|lowered|raised|last swing)\b', text))
            add('STOP_MOVED' if moved else 'STOP_SET', not intended and not negated and not retrospective)
        if re.search(r'\b(?:target|take profit|tp)\b', text) and re.search(r'\b(?:put|set|placed|moved)\b', text):
            add('TARGET_SET', not intended and not negated and not retrospective)
        if re.search(r'\b(?:partial(?:s)?|half off the table)\b', text):
            reported = bool(re.search(r'\bi (?:took|closed|sold)|\bpartial (?:just )?filled', text))
            add('PARTIAL_EXIT', reported and not intended and not negated and not retrospective)
        if re.search(r'\b(?:i (?:closed|exited) (?:the |my )?(?:whole |entire |full )?(?:position|trade)|i am flat|i\x27m flat|i closed all)\b', text):
            add('FULL_EXIT', not intended and not negated and not retrospective)
        elif re.search(r'\b(?:close (?:the |my )?(?:position|trade)|take it off|exit (?:the |my )?trade)\b', text):
            add('FULL_EXIT')
        if re.search(r'\b(?:holding|hold)\s+(?:my |the |one |a )?(?:position|trade|runner|contracts?)\b', text):
            add('HOLD', not intended and not negated and not retrospective)
        if not found and re.search(r'\b(?:get in shorts|enter long|enter short|buy|sell|short)\b', text) and re.search(r"\b(?:i|i'm|i'd)\b", text):
            add('TRADE_PLAN')
        return self.validate(statement, source_hash, found)

    def validate(self, statement, source_hash, found):
        """Shared deterministic transitions and explicit-field extraction.

        Optional AI candidates must pass grounding before calling this method.
        """
        quote = statement['quote']
        text = re.sub(r'\s+', ' ', quote).lower().replace('’', "'")
        retrospective = bool(re.search(r'\b(?:yesterday|last week|for my first trade|example|demo)\b', text))
        fill = bool(re.search(r'\bi (?:(?:just|have) )?(?:got filled|was filled|entered|shorted|went long)\b|\bmy limit (?:just )?filled\b', text))
        events = []
        for kind, reported in found:
            fields = numeric_fields(quote)
            before = self.state
            transition_error = None
            if reported and self.state in ('PENDING_ORDER', 'OPEN_POSITION', 'PARTIAL'):
                for key in ('instrument', 'side'):
                    if fields[key] != UNKNOWN and self.fields[key] != UNKNOWN and fields[key] != self.fields[key]:
                        transition_error = 'CONTRADICTORY_'+key.upper()
            if kind in ('LIMIT_ORDER_PLACED', 'MARKET_ORDER') and reported and not fill:
                if self.state in ('OPEN_POSITION', 'PARTIAL', 'PENDING_ORDER'):
                    transition_error = 'UNSUPPORTED_ADDITION_OR_SECOND_ORDER'
                else:
                    self.episode += 1
                    self.fields = {k: UNKNOWN for k in self.fields}
                    self.order_type = 'LIMIT' if kind == 'LIMIT_ORDER_PLACED' else 'MARKET'
                    self.state = 'PENDING_ORDER'
            if reported and not transition_error:
                if kind == 'LIMIT_ORDER_FILLED' or kind == 'MARKET_ORDER' and fill:
                    if self.state not in ('PENDING_ORDER', 'OPEN_POSITION', 'PARTIAL'):
                        transition_error = 'UNLINKED_REPORTED_FILL'
                    elif self.state == 'PENDING_ORDER': self.state = 'OPEN_POSITION'
                elif kind == 'LIMIT_ORDER_CANCELLED':
                    if self.state != 'PENDING_ORDER': transition_error = 'NO_PENDING_ORDER'
                    else: self.state = 'NO_POSITION'
                elif kind == 'LIMIT_ORDER_MODIFIED' and self.state != 'PENDING_ORDER':
                    transition_error = 'NO_PENDING_ORDER'
                elif kind in ('PARTIAL_EXIT', 'FULL_EXIT', 'STOP_MOVED', 'HOLD'):
                    if self.state not in ('OPEN_POSITION', 'PARTIAL'): transition_error = 'NO_REPORTED_POSITION'
                    elif kind == 'PARTIAL_EXIT': self.state = 'PARTIAL'
                    elif kind == 'FULL_EXIT': self.state = 'CLOSED'
                if not transition_error and kind != 'TRADE_PLAN':
                    # Only current quoted fields; plans never become execution parameters.
                    self.fields.update({k: v for k, v in fields.items() if v != UNKNOWN})
            eid = hashlib.sha256(json.dumps([source_hash, statement, kind], sort_keys=True).encode()).hexdigest()
            event = dict(statement, event_id=eid, action=kind, fields=fields,
                         reported_state_before=before, reported_state_after=self.state,
                         reported_order_type=self.order_type, episode_id=self.episode,
                         reported_fill=fill and reported,
                         reported_position=deepcopy(self.fields), transition_error=transition_error,
                         evidence_status='RETROSPECTIVE_OR_EXAMPLE' if retrospective else 'SELF_REPORTED' if reported else 'INTENTION_OR_AMBIGUOUS',
                         timestamp_basis='TRANSCRIPT_BLOCK_START_NOT_WORD_TIME',
                         source_sha256=source_hash, vocabulary_sha256=self.vocab_hash,
                         historical_replay=True, orders_enabled=False)
            # Vocabulary contributes terminology matches, never historical labels or numbers.
            event['vocabulary_matches'] = sorted({x['term'] for items in self.vocabulary.get('categories', {}).values()
                                                  for x in items if x.get('term', '').lower() in text})
            events.append(event)
        return events


class PaperCopy:
    """Independent fills must be acknowledged explicitly; reported fills never fill us."""
    def __init__(self, account=None, max_age=15):
        self.account = account
        self.max_age = max_age
        self.seen = set()
        self.state = 'NO_POSITION'
        self.order = None
        self.position = None
        self.fill_ids = set()

    def apply(self, event, now):
        reasons = []
        action = event['action']
        eid = event['event_id']
        if eid in self.seen: reasons.append('DUPLICATE_EVENT')
        self.seen.add(eid)
        age = now - event['source_seconds']
        if age < 0 or age > self.max_age: reasons.append('STALE_OR_FUTURE_EVENT')
        if event['evidence_status'] != 'SELF_REPORTED': reasons.append('NOT_REPORTED_EXECUTION')
        if event.get('transition_error'): reasons.append(event['transition_error'])
        disposition = 'OBSERVATION_ONLY'
        fields = event['fields']
        if action in ('LIMIT_ORDER_PLACED', 'MARKET_ORDER') and event.get('reported_fill'):
            disposition = 'TANJA_REPORTED_FILL_OUR_FILL_UNCHANGED'
        elif action in ('LIMIT_ORDER_PLACED', 'MARKET_ORDER'):
            for key in ('instrument', 'side', 'quantity', 'entry', 'stop'):
                if fields[key] == UNKNOWN: reasons.append('MISSING_'+key.upper())
            if fields['instrument'] != UNKNOWN and fields['instrument'] != 'MNQ':
                reasons.append('UNSUPPORTED_INSTRUMENT_MNQ_SIZING_ONLY')
            if not self.account: reasons.append('PAPER_ACCOUNT_AND_PROP_LIMITS_REQUIRED')
            if self.state in ('PENDING_ORDER', 'OPEN_POSITION', 'PARTIAL'): reasons.append('EXISTING_PAPER_EXPOSURE')
            if not reasons:
                try:
                    a = self.account
                    if any(type(a[k]) not in (int, float) or not math.isfinite(a[k]) or a[k] <= 0
                           for k in ('balance', 'minimum_balance', 'daily_loss_remaining')):
                        raise ValueError('INVALID_PAPER_ACCOUNT')
                    committed = a['committed_risk']
                    if type(committed) not in (int, float) or not math.isfinite(committed) or committed < 0:
                        raise ValueError('INVALID_COMMITTED_RISK')
                    budget = min(risk_budget(a['balance'])-committed, a['balance']-a['minimum_balance']-committed-.01,
                                 a['daily_loss_remaining']-committed-.01)
                    qty = contracts_for_stop(fields['entry'], fields['stop'], fields['side'].lower(), budget, fields['quantity'])
                    if qty == 0: raise ValueError('RISK_LIMIT_NO_CONTRACT_FITS')
                    target = fields['target']
                    if target != UNKNOWN and (target*4 != round(target*4) or (target-fields['entry'])*(1 if fields['side']=='LONG' else -1) <= 0):
                        raise ValueError('INVALID_TARGET')
                    self.order = dict(fields, quantity=qty, source_quantity=fields['quantity'],
                                      type='LIMIT' if action == 'LIMIT_ORDER_PLACED' else 'MARKET',
                                      id=eid, risk_budget=budget, source_seconds=event['source_seconds'])
                    self.state = 'PENDING_ORDER'
                    disposition = 'PAPER_ORDER_SUBMITTED_NO_FILL'
                except (ValueError, KeyError, TypeError) as exc: reasons.append(str(exc))
        elif action == 'LIMIT_ORDER_FILLED':
            disposition = 'TANJA_REPORTED_FILL_OUR_FILL_UNCHANGED'
        elif action == 'LIMIT_ORDER_CANCELLED':
            if not reasons:
                if not self.order or self.state != 'PENDING_ORDER': reasons.append('NO_PAPER_PENDING_ORDER')
                else: self.order = None; self.state = 'NO_POSITION'; disposition = 'PAPER_ORDER_CANCELLED'
        elif action == 'LIMIT_ORDER_MODIFIED':
            if not self.order or self.state != 'PENDING_ORDER' or self.order['type'] != 'LIMIT':
                reasons.append('NO_PAPER_PENDING_LIMIT')
            for key in ('entry', 'stop'):
                if fields[key] == UNKNOWN: reasons.append('REPLACEMENT_MISSING_'+key.upper())
            if not reasons:
                old = self.order
                try:
                    qty = contracts_for_stop(fields['entry'], fields['stop'], old['side'].lower(), old['risk_budget'], old['quantity'])
                    if not qty: raise ValueError('RISK_LIMIT_NO_CONTRACT_FITS')
                    if fields['side'] not in (UNKNOWN, old['side']) or fields['instrument'] not in (UNKNOWN, old['instrument']):
                        raise ValueError('CONTRADICTORY_REPLACEMENT')
                    old.update(entry=fields['entry'], stop=fields['stop'], quantity=qty,
                               source_seconds=event['source_seconds'])
                    disposition = 'PAPER_LIMIT_REPLACED_NO_FILL'
                except ValueError as exc: reasons.append(str(exc))
        elif action in ('STOP_SET', 'STOP_MOVED', 'TARGET_SET', 'PARTIAL_EXIT', 'FULL_EXIT'):
            if not self.position: reasons.append('NO_INDEPENDENT_PAPER_FILL')
            if not reasons:
                p = self.position
                if action in ('STOP_SET', 'STOP_MOVED', 'TARGET_SET'):
                    key = 'target' if action == 'TARGET_SET' else 'stop'
                    value = fields[key]
                    if value == UNKNOWN: reasons.append('MISSING_'+key.upper())
                    elif value*4 != round(value*4): reasons.append('OFF_TICK_PRICE')
                    elif key == 'stop' and (value-p['stop'])*(1 if p['side']=='LONG' else -1) < 0:
                        reasons.append('STOP_WIDENING_REJECTED')
                    elif key == 'target' and (value-p['entry'])*(1 if p['side']=='LONG' else -1) <= 0:
                        reasons.append('INVALID_TARGET')
                    else: p[key] = value; disposition = 'PAPER_BRACKET_UPDATED'
                elif fields['entry'] == UNKNOWN:
                    reasons.append('INDEPENDENT_EXIT_PRICE_REQUIRED')
                elif action == 'PARTIAL_EXIT' and (fields['quantity'] == UNKNOWN or fields['quantity'] >= p['quantity']):
                    reasons.append('EXPLICIT_PARTIAL_QUANTITY_REQUIRED')
                else:
                    # A request is logged; no invented execution price or assumed exit fill.
                    disposition = 'PAPER_EXIT_REQUEST_NO_FILL'
        if reasons: disposition = 'REJECTED'
        return dict(event_id=eid, action=action, source_timestamp=event['source_timestamp'],
                    disposition=disposition, reasons=reasons, paper_state=self.state,
                    paper_order=deepcopy(self.order), paper_position=deepcopy(self.position),
                    orders_enabled=False)

    def acknowledge_fill(self, order_id, fill_id, price, quantity, now, exit=False):
        """Test/external simulator acknowledgement, never called by transcript inference."""
        if fill_id in self.fill_ids: raise ValueError('DUPLICATE_SIMULATED_FILL')
        if type(price) not in (int, float) or not math.isfinite(price) or price <= 0 or price*4 != round(price*4):
            raise ValueError('INVALID_INDEPENDENT_FILL_PRICE')
        if type(quantity) is not int or quantity <= 0: raise ValueError('INVALID_FILL_QUANTITY')
        if exit:
            if not self.position or self.position['id'] != order_id or quantity > self.position['quantity']:
                raise ValueError('INVALID_EXIT_FILL')
            self.position['quantity'] -= quantity
            self.state = 'PARTIAL' if self.position['quantity'] else 'CLOSED'
            if self.state == 'CLOSED': self.position = None; self.order = None
        else:
            o = self.order
            if not o or o['id'] != order_id or self.state != 'PENDING_ORDER' or quantity != o['quantity']:
                raise ValueError('INVALID_ENTRY_FILL')
            if not 0 <= now-o['source_seconds'] <= self.max_age: raise ValueError('STALE_FILL')
            sign = 1 if o['side'] == 'LONG' else -1
            if o['type'] == 'LIMIT' and (price-o['entry'])*sign > 0: raise ValueError('LIMIT_PRICE_VIOLATION')
            if (price-o['stop'])*sign <= 0 or abs(price-o['stop'])*2*quantity > o['risk_budget']:
                raise ValueError('FILL_STOP_OR_RISK_VIOLATION')
            self.position = dict(o, entry=price)
            self.state = 'OPEN_POSITION'
        self.fill_ids.add(fill_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--transcript', required=True)
    parser.add_argument('--start', default='00:00:00')
    parser.add_argument('--end', default='99:59:59')
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--paper-account', help='Optional explicit simulation account JSON; never a broker account')
    args = parser.parse_args()
    source = Path(args.transcript).resolve()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    out = Path(args.output_dir); out.mkdir(parents=True, exist_ok=True)
    journal = TranscriptJournal(Store(out / 'journal'))
    session, attempt, _ = journal.start(dict(id=digest+':'+args.start+':'+args.end,
        audio_url=str(source), started_at=0, authorization='User-authorized saved historical transcript; no audio access'), 'historical_transcript')
    interpreter = Interpreter()
    paper = PaperCopy(json.loads(Path(args.paper_account).read_text()) if args.paper_account else None)
    actions, events, saved = [], [], 0
    began = time.perf_counter()
    for statement in statements(source, seconds(args.start), seconds(args.end)):
        for event in interpreter.interpret(statement, digest):
            events.append(event)
            actions.append(paper.apply(event, event['source_seconds']))
            if attempt is not None: saved += journal.record_decision(session, attempt, event)
    if attempt is not None: journal.finish(session, attempt, 'COMPLETED')
    elapsed = time.perf_counter()-began
    for name, records in [('events.jsonl', events), ('paper_actions.jsonl', actions)]:
        (out/name).write_text(''.join(json.dumps(x, sort_keys=True, allow_nan=False)+'\n' for x in records))
    with journal.store.connect() as db:
        shadow = [json.loads(x[0]) for x in db.execute('SELECT payload FROM shadow_transcript_observations WHERE session_id=? ORDER BY event_sequence', (session,))]
    (out/'shadow_observations.jsonl').write_text(''.join(json.dumps(x, sort_keys=True)+'\n' for x in shadow))
    summary = dict(source=str(source), source_sha256=digest, session_id=session,
        actions=dict(Counter(e['action'] for e in events)), events=len(events),
        rejected_actions=sum(a['disposition']=='REJECTED' for a in actions),
        submitted_orders=sum(a['disposition']=='PAPER_ORDER_SUBMITTED_NO_FILL' for a in actions),
        independent_simulated_fills=0, paper_state=paper.state, reported_state=interpreter.state,
        shadow_observations=len(shadow), newly_saved=saved, processing_seconds=elapsed,
        mode='HISTORICAL_REPLAY_OBSERVE_ONLY', orders_enabled=False,
        limitations=['Deterministic language rules, not an LLM; unsupported wording can be missed',
            'NoteGPT text not verified against audio or broker; block timestamps are not word timing',
            'Risk sizing supports MNQ only; no inferred contract expiry or account balance',
            'No independent execution feed; no assumed market, limit or exit fills',
            'No live freshness claim: replay clock is historical source time; no source-derived latency measurement'])
    (out/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__': main()
