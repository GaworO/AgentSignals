"""Durable quote-event simulator for the current Tanja core. No network transport.

A quote queues a request; a later quote with acknowledgement='fill' models its
fill and atomic bracket replacement. 'hold' models an unknown acknowledgement;
'reject' resolves it without a fill. These are simulation assumptions, not broker
behaviour. Supplied quotes are NOT inferred from OHLC intrabar paths.
"""
import hashlib
import json
import math
import sqlite3
from pathlib import Path
from management_policy import contract, local_partial_proposal, es_session_partial_proposal, add_on_proposal
from management_review import propose_stop
from plan_review import review_plan
import source_rules
from datetime import datetime
from zoneinfo import ZoneInfo

VERSION = 'tanja-paper-lifecycle-v1'


def clock(value):
    return type(value) is int and value >= 0


def money(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def price(value):
    return money(value) and value > 0 and value * 4 == round(value * 4)


def encode(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def losses_on_day(runs, now):
    ny=ZoneInfo('America/New_York');day=datetime.fromtimestamp(now,ny).date()
    return sum(r['state']=='CLOSED' and r['gross_usd']-r['fees_usd'] < -1e-8
        and datetime.fromtimestamp(r.get('closed_at',r['ledger'][-1]['at']),ny).date()==day for r in runs)


def gated_plan(decision, now, strategy_mode='source_selections'):
    """Consumes a frozen observer decision; no new AI review or invented approval."""
    p = decision['compiled']['plan']; s = decision['selected']['selection']
    g = decision['account_guard']
    if (decision['state'] != 'PLAN_READY' or decision['orders_enabled'] is not False
        or decision['ai_context']['state'] != 'PERMITTED'
        or decision['scheduled_news']['state'] != 'CLEAR' or decision.get('news_entry_reasons')
        or g['state'] != 'REPORTED_LIMITS_CLEAR'
        or p['direction'] not in ('long', 'short')
        or not p['direction'] == s['direction'] == decision['ai_context']['direction']
        or p['symbol'] != 'MNQ' or p['entry_mode'] not in ('immediate_after_confirmation','retracement')):
        raise ValueError('Current gated plan required')
    if strategy_mode == 'legacy_user_core':
        if s['confirmation_policy'] != 'closed_m5_v1' or s['timeframe_minutes'] != 5 or s['swing_break_mode'] != 'close' or s.get('requires_swing_break') is not True:
            raise ValueError('Legacy M5 plan required')
    else:
        from entry_rules import compile_entry
        checked = compile_entry(decision['packet'], s, decision['selected']['observations'], now=now)
        if p.get('strategy_policy') != source_rules.VERSION or checked.get('plan') != {k:v for k,v in p.items() if k not in ('ai_run_id','management_policy')}:
            raise ValueError('Recompiled source-selected plan required')
    times = [p['trigger_available_at'], decision['frozen_at'], now, p['context_valid_until']]
    freshness = p['selected_at'] if p.get('selection_available_at') is not None else times[0]
    if (not all(clock(t) for t in times) or not times[0] <= times[1] <= times[2] < times[3]
        or (p['entry_mode']=='immediate_after_confirmation' and now-freshness > 15)):
        raise ValueError('Expired or noncausal decision')
    reviewed = review_plan(p, now=now)
    if reviewed['state'] != 'HYPOTHETICAL_PLAN_COMPLETE':
        raise ValueError('Complete price plan required')
    return p, reviewed


class PaperLifecycle:
    def __init__(self, path, *, slippage_points, fee_per_side, strategy_mode='source_selections'):
        if not money(slippage_points) or slippage_points*4 != round(slippage_points*4) or not money(fee_per_side):
            raise ValueError('Explicit nonnegative tick slippage and per-contract per-side fee required')
        self.path = str(path)
        if strategy_mode not in ('source_selections','legacy_user_core'): raise ValueError('Unknown strategy policy')
        self.config = dict(version=VERSION, strategy_mode=strategy_mode, management=source_rules.contract() if strategy_mode=='source_selections' else contract(), slippage_points=slippage_points,
                           fee_per_side=fee_per_side, mode='SIMULATION_ONLY', orders_enabled=False)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS simulation_config(value TEXT);
              CREATE TABLE IF NOT EXISTS simulation_state(id INTEGER PRIMARY KEY, value TEXT);
              CREATE TABLE IF NOT EXISTS simulation_events(id TEXT PRIMARY KEY, fingerprint TEXT, result TEXT);''')
            old = db.execute('SELECT value FROM simulation_config').fetchone()
            if old and old[0] != encode(self.config): raise ValueError('Persisted simulation configuration differs')
            if not old: db.execute('INSERT INTO simulation_config VALUES(?)', (encode(self.config),))
            db.execute('INSERT OR IGNORE INTO simulation_state VALUES(1,?)', (encode(dict(cursor=-1, runs=[])),))

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def _event(self, event_id, payload, at, action):
        if not isinstance(event_id, str) or not event_id.strip() or not clock(at): raise ValueError('Event ID and UTC clock required')
        fingerprint = hashlib.sha256(encode(payload).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT fingerprint,result FROM simulation_events WHERE id=?', (event_id,)).fetchone()
            if old:
                if old[0] != fingerprint: raise ValueError('Conflicting duplicate event')
                return dict(json.loads(old[1]), duplicate=True)
            state = json.loads(db.execute('SELECT value FROM simulation_state WHERE id=1').fetchone()[0])
            if at <= state['cursor']: raise ValueError('Events must advance time; retry with original event ID')
            result = action(state)
            state['cursor'] = at
            result.update(mode='SIMULATION_ONLY', orders_sent=0)
            db.execute('UPDATE simulation_state SET value=? WHERE id=1', (encode(state),))
            db.execute('INSERT INTO simulation_events VALUES(?,?,?)', (event_id, fingerprint, encode(result)))
            return result

    @staticmethod
    def log(run, at, event_type, **values):
        run['ledger'].append(dict(at=at, event=event_type, **values))

    def submit(self, event_id, *, decision, objective=None, now):
        p, reviewed = gated_plan(decision, now, self.config['strategy_mode'])
        if p['initial_quantity'] > decision['account_guard']['max_contracts']:
            raise ValueError('Account contract capacity exceeded')
        if objective is not None or self.config['strategy_mode']=='legacy_user_core':
            if (not isinstance(objective, dict) or not price(objective.get('price'))
                or not clock(objective.get('available_at')) or objective['available_at'] > now
                or not isinstance(objective.get('source'), str) or not objective['source'].strip()):
                raise ValueError('Explicit causally selected local objective required')
        sign = 1 if p['direction'] == 'long' else -1
        if objective is not None and objective.get('symbol','MNQ')=='MNQ' and not 0 < sign*(objective['price']-p['entry_reference']['price']) < sign*(reviewed['target']-p['entry_reference']['price']):
            raise ValueError('Local objective must be between entry and final target')
        def apply(state):
            if self.config['strategy_mode']=='source_selections' and losses_on_day(state['runs'],now) >= 2:
                raise ValueError('TWO_LOSSES_NEW_YORK_DAY')
            if any(r['state'] in ('ENTRY_PENDING', 'OPEN') for r in state['runs']): raise ValueError('A simulation position is already active')
            if any(r['id'] == p['plan_id'] for r in state['runs']): raise ValueError('Plan already consumed')
            if p.get('setup_id') and any(r['decision']['compiled']['plan'].get('setup_id')==p['setup_id'] for r in state['runs']):
                raise ValueError('SETUP_ALREADY_ATTEMPTED')
            budget = min(p['risk_budget_usd'], decision['account_guard']['budget_usd'])
            per_contract = (abs(p['entry_reference']['price']-reviewed['stop'])+2*self.config['slippage_points'])*2+2*self.config['fee_per_side']
            quantity = min(p['initial_quantity'], int(budget//per_contract))
            if quantity < 1: raise ValueError('Budget cannot fund one contract including modeled costs')
            spec = p.get('management_spec')
            if self.config['strategy_mode']=='source_selections':
                source_rules.validate_management(spec, now)
                if sum(source_rules.partial_quantity(r,quantity) for r in spec['partials']) >= quantity:
                    raise ValueError('Explicit partial quantities exceed cost-adjusted position; reselect quantities')
                for rule in spec['partials']:
                    obj=rule['objective']
                    if obj['direction']!=('above' if sign==1 else 'below'):
                        raise ValueError('Objective direction conflicts with position')
                    if obj['symbol']=='MNQ' and not 0 < sign*(obj['price']-p['entry_reference']['price']) < sign*(reviewed['target']-p['entry_reference']['price']):
                        raise ValueError('Selected MNQ partial must precede final target in profit direction')
            run = dict(id=p['plan_id'], direction=p['direction'], sign=sign, state='ENTRY_PENDING',
                decision=decision, objective=objective, budget=budget, stop=reviewed['stop'], target=reviewed['target'],
                original_quantity=quantity, quantity=0, lots=[], gross_usd=0., fees_usd=0.,
                local_done=False, es_done=False, additions_done=0, ledger=[], management_spec=spec, completed_partials=[],
                pending=dict(kind='ENTRY', requested_at=now, quantity=quantity,
                    expires=min(p['context_valid_until'],p.get('entry_valid_until',p['context_valid_until'])),
                    limit_price=p['entry_reference']['price'] if p['entry_mode']=='retracement' else None))
            state['runs'].append(run)
            self.log(run, now, 'SIMULATED_REQUEST', **run['pending'])
            return dict(state=run['state'], position_id=run['id'])
        return self._event(event_id, dict(type='submit', decision=decision, objective=objective, now=now), now, apply)

    def _risk(self, run, extra=None):
        lots = run['lots'] + ([extra] if extra else [])
        slip = self.config['slippage_points']; fee = self.config['fee_per_side']
        # Never finance an add with realized or unrealized gains. Include realized
        # losses, fees already paid, and adverse stop fill + closing fees per lot.
        return (max(0., -run['gross_usd']) + run['fees_usd'] +
            sum((max(0., run['sign']*(lot['price']-run['stop'])+slip)*2+fee)*lot['quantity'] for lot in lots)
            + (extra['quantity']*fee if extra else 0))

    def _bracket(self, run, at):
        self.log(run, at, 'SIMULATED_BRACKET_CONFIRMED', quantity=run['quantity'], stop=run['stop'], target=run['target'])

    def _close(self, run, at, quantity, fill, reason):
        left = quantity
        for lot in run['lots']:
            n = min(left, lot['quantity']); left -= n; lot['quantity'] -= n
            run['gross_usd'] += run['sign']*(fill-lot['price'])*n*2
        run['lots'] = [lot for lot in run['lots'] if lot['quantity']]
        run['quantity'] -= quantity
        run['fees_usd'] += quantity*self.config['fee_per_side']
        self.log(run, at, 'SIMULATED_FILL', operation=reason, quantity=quantity, price=fill, remaining=run['quantity'])
        if run['quantity'] == 0:
            run['state'] = 'CLOSED'; run['pending'] = None; run['closed_at'] = at
            self.log(run, at, 'SIMULATED_BRACKET_CANCELLED', quantity=0)
        else: self._bracket(run, at)

    def _settle(self, run, now, market, acknowledgement):
        pending = run['pending']; kind = pending['kind']
        if pending.get('expires') is not None and now >= pending['expires']:
            self.log(run, now, 'SIMULATED_REQUEST_EXPIRED', operation=kind)
            run['pending'] = None
            if kind == 'ENTRY': run['state'] = 'EXPIRED'
            return 'REQUEST_EXPIRED'
        if kind == 'ENTRY' and pending.get('limit_price') is not None:
            sign=run['sign']; p=run['decision']['compiled']['plan']
            thesis=p.get('thesis_invalidation',p['invalidation'])
            boundary=(thesis if thesis.get('symbol','MNQ')=='MNQ' else p['invalidation'])['price']
            boundary=max(boundary,p['invalidation']['price']) if sign==1 else min(boundary,p['invalidation']['price'])
            if sign*(market-boundary) <= 0 or sign*(market-run['target']) >= 0:
                reason='ENTRY_INVALIDATED' if sign*(market-boundary) <= 0 else 'TARGET_REACHED_BEFORE_ENTRY'
                self.log(run, now, 'SIMULATED_REQUEST_CANCELLED', operation=kind, reason=reason)
                run['pending']=None; run['state']='CANCELLED'
                return reason
            if acknowledgement != 'reject' and sign*(market-pending['limit_price']) > 0:
                return 'WAITING_FOR_RETRACEMENT'
        if acknowledgement == 'hold': return 'AWAITING_SIMULATED_ACK'
        if acknowledgement == 'reject':
            self.log(run, now, 'SIMULATED_REQUEST_REJECTED', operation=kind)
            run['pending'] = None
            if kind == 'ENTRY': run['state'] = 'REJECTED'
            return 'REQUEST_REJECTED'
        sign = run['sign']; slip = self.config['slippage_points']
        if kind == 'STOP':
            stop = pending['stop_price']
            if sign*(market-stop) <= 0:
                self.log(run, now, 'SIMULATED_REQUEST_REJECTED', operation=kind, reason='STOP_WOULD_CROSS_MARKET')
            elif sign*(stop-run['stop']) > 0:
                run['stop'] = stop; self._bracket(run, now)
            run['pending'] = None
            return 'STOP_RESOLVED'
        quantity = pending['quantity']
        if kind in ('LOCAL_PARTIAL', 'ES_PARTIAL', 'SELECTED_PARTIAL'):
            self._close(run, now, quantity, market-sign*slip, kind)
            if kind == 'SELECTED_PARTIAL': run['completed_partials'].append(pending['rule_id'])
            else: run['local_done' if kind == 'LOCAL_PARTIAL' else 'es_done'] = True
        else:
            fill = market+sign*slip
            if kind == 'ENTRY' and pending.get('limit_price') is not None:
                # A limit can improve, but never fill at a worse price than its
                # limit. This quote-touch fill remains a simulation convention.
                fill = min(fill,pending['limit_price']) if sign==1 else max(fill,pending['limit_price'])
            if not sign*(market-run['stop']) > 0 or not sign*(run['target']-fill) > 0:
                allowed = 0
            else:
                allowed = quantity
                while allowed:
                    additional = (max(0., sign*(fill-run['stop'])+slip)*2+2*self.config['fee_per_side'])*allowed
                    if (self._risk(run, dict(price=fill, quantity=allowed)) <= run['budget']+1e-8
                        and additional <= pending.get('shared_budget_available', run['budget'])+1e-8): break
                    allowed -= 1
            # Original requested size is frozen. Reject a changed-risk entry;
            # an add may reduce further, as explicitly allowed by the add policy.
            if not allowed or (kind == 'ENTRY' and allowed != quantity):
                self.log(run, now, 'SIMULATED_REQUEST_REJECTED', operation=kind, reason='FILL_GEOMETRY_OR_COST_INCLUSIVE_RISK')
                if kind == 'ENTRY': run['state'] = 'REJECTED'
                run['pending'] = None
                return 'FILL_REJECTED'
            quantity = allowed
            run['lots'].append(dict(price=fill, quantity=quantity))
            run['quantity'] += quantity; run['fees_usd'] += quantity*self.config['fee_per_side']
            if kind == 'ENTRY':
                run.update(state='OPEN', opened_at=now, base_entry=fill)
            else: run['additions_done'] += 1
            self.log(run, now, 'SIMULATED_FILL', operation=kind, price=fill, quantity=quantity, remaining=run['quantity'])
            self._bracket(run, now)
        run['pending'] = None
        return 'SIMULATED_FILL_CONFIRMED'

    def quote(self, event_id, *, at, received_at, market_price, mnq_bars=(), es_bars=(),
              news_events=(), add_decision=None, acknowledgement='fill', es_quote=None, structural_selection=None,daily_atr=None,nq_quote=None,entry_block_reasons=()):
        if not clock(at) or not clock(received_at) or not 0 <= received_at-at <= 2 or not price(market_price):
            raise ValueError('Fresh tick-aligned quote required')
        if market_price <= self.config['slippage_points']: raise ValueError('Price below slippage')
        if acknowledgement not in ('fill', 'hold', 'reject'): raise ValueError('Explicit simulator acknowledgement required')
        for event in news_events:
            if not clock(event.get('at')) or not clock(event.get('known_at')): raise ValueError('Dated news required')
        payload = dict(type='quote', at=at, received_at=received_at, market_price=market_price,
                       mnq_bars=mnq_bars, es_bars=es_bars, news_events=news_events, add_decision=add_decision, acknowledgement=acknowledgement,
                       es_quote=es_quote, structural_selection=structural_selection,daily_atr=daily_atr,nq_quote=nq_quote,entry_block_reasons=entry_block_reasons)
        def apply(state):
            run = next((r for r in state['runs'] if r['state'] in ('ENTRY_PENDING', 'OPEN')), None)
            if not run: return dict(state='IDLE')
            sign = run['sign']; result = 'NO_ACTION'
            # Protection remains active while any later request awaits acknowledgement.
            if run['state'] == 'OPEN' and (sign*(market_price-run['stop']) <= 0 or sign*(market_price-run['target']) >= 0):
                if run['pending']: self.log(run, received_at, 'SIMULATED_REQUEST_CANCELLED', operation=run['pending']['kind'], reason='PROTECTION_EXIT_FIRST')
                stop_hit = sign*(market_price-run['stop']) <= 0
                fill = market_price-sign*self.config['slippage_points'] if stop_hit else run['target']
                self._close(run, received_at, run['quantity'], fill, 'STOP' if stop_hit else 'TARGET')
                result = 'PROTECTION_EXIT'
            elif run['pending'] and run['pending']['kind']=='ENTRY' and entry_block_reasons:
                self.log(run,received_at,'SIMULATED_REQUEST_CANCELLED',operation='ENTRY',reason='CURRENT_ENTRY_GUARDS',details=list(entry_block_reasons))
                run['pending']=None;run['state']='CANCELLED';result='CURRENT_ENTRY_GUARDS'
            elif run['pending'] and run['pending']['kind'] in ('ENTRY','ADD') and run['decision']['compiled']['plan'].get('trade_kind')=='continuation' and self._continuation_blocked(daily_atr,received_at):
                self.log(run,received_at,'SIMULATED_REQUEST_CANCELLED',operation=run['pending']['kind'],reason='CONTINUATION_ATR_GUARD')
                if run['pending']['kind']=='ENTRY':run['state']='CANCELLED'
                run['pending']=None;result='CONTINUATION_ATR_GUARD'
            elif run['pending'] and run['pending']['kind'] in ('ENTRY','ADD') and any(e['known_at'] <= received_at and e['at']-300 <= received_at < e['at']+300 for e in news_events):
                self.log(run, received_at, 'SIMULATED_REQUEST_CANCELLED', operation=run['pending']['kind'], reason='NEWS_BLACKOUT')
                if run['pending']['kind'] == 'ENTRY': run['state'] = 'CANCELLED'
                run['pending'] = None; result = 'NEWS_BLACKOUT'
            elif run['pending'] and at <= run['pending']['requested_at']:
                result = 'WAIT_FOR_LATER_QUOTE'
            elif run['pending'] and run['pending']['kind']=='ENTRY' and run['decision']['compiled']['plan'].get('analysis_symbol')=='NQ' and received_at < run['pending']['expires'] and self._nq_entry_guard(run,nq_quote,received_at)!='CLEAR':
                result=self._nq_entry_guard(run,nq_quote,received_at)
                if result=='NQ_THESIS_INVALIDATED':
                    self.log(run,received_at,'SIMULATED_REQUEST_CANCELLED',operation='ENTRY',reason=result)
                    run['pending']=None;run['state']='CANCELLED'
            elif run['pending']:
                result = self._settle(run, received_at, market_price, acknowledgement)
            else:
                if self.config['strategy_mode']=='source_selections':
                    proposal = source_rules.partial_proposal(spec=run['management_spec'], position_id=run['id'], original_quantity=run['original_quantity'],
                        remaining_quantity=run['quantity'], completed=run['completed_partials'], now=received_at,
                        quotes=dict(MNQ=dict(at=at,price=market_price), ES=es_quote or {}))
                    stop = source_rules.structural_stop(selection=structural_selection, bars=mnq_bars,
                        direction=run['direction'], opened_at=run['opened_at'], now=received_at,
                        current_stop=run['stop'], market_price=market_price)
                    stops = [stop] if stop['action']=='REVIEW_STOP_PROPOSAL' else []
                    # The user's news protection is retained as a named account
                    # overlay; never use legacy automatic pivots in source mode.
                    for event in news_events:
                        if event['known_at'] <= received_at:
                            candidate = propose_stop([],direction=run['direction'],entry=run['base_entry'],current_stop=run['stop'],
                                opened_at=run['opened_at'],as_of=received_at,market_price=market_price,price_known_at=at,
                                news_at=event['at'],news_known_at=event['known_at'])
                            if candidate['action']=='REVIEW_STOP_PROPOSAL': stops.append(candidate)
                    pending = None
                    if stops:
                        chosen=max(stops,key=lambda s:sign*s['stop_price'])
                        pending=dict(kind='STOP',stop_price=chosen['stop_price'],reason=chosen['reason'])
                    elif proposal['action']=='REVIEW_PARTIAL_PROPOSAL':
                        pending=dict(kind='SELECTED_PARTIAL',quantity=proposal['quantity'],rule_id=proposal['rule_id'],intent_id=proposal['intent_id'])
                    if pending:
                        pending['requested_at']=received_at;run['pending']=pending
                        self.log(run,received_at,'SIMULATED_REQUEST',**pending)
                    result='REQUEST_QUEUED' if pending else ('ADD_REQUIRES_SEPARATE_SOURCE_REVIEW' if add_decision is not None else 'NO_ACTION')
                    return dict(state=run['state'], result=result, quantity=run['quantity'], pending=run['pending'])
                args = dict(position_id=run['id'], direction=run['direction'], original_quantity=run['original_quantity'],
                            remaining_quantity=run['quantity'], opened_at=run['opened_at'], now=received_at)
                partial = local_partial_proposal(**args, entry=run['base_entry'], objective=run['objective'], final_target=run['target'],
                    market_price=market_price, price_known_at=at, first_partial_done=run['local_done'], partial_pending=False)
                es = es_session_partial_proposal(**args, es_bars=es_bars, first_es_partial_done=run['es_done'], partial_pending=False)
                # Tightening protection gets the first management slot, including
                # pre-news protection; sizing actions then serialize local, ES, add.
                stop_args = dict(direction=run['direction'], entry=run['base_entry'], current_stop=run['stop'],
                    opened_at=run['opened_at'], as_of=received_at, market_price=market_price, price_known_at=at)
                stops = [propose_stop(mnq_bars, **stop_args)]
                for event in news_events:
                    if event['known_at'] <= received_at:
                        stops.append(propose_stop(mnq_bars, **stop_args, news_at=event['at'], news_known_at=event['known_at']))
                stops = [s for s in stops if s['action'] == 'REVIEW_STOP_PROPOSAL']
                pending = None
                if stops:
                    chosen = max(stops, key=lambda s:sign*s['stop_price'])
                    pending = dict(kind='STOP', stop_price=chosen['stop_price'], reason=chosen['reason'])
                elif partial['action'] == 'REVIEW_PARTIAL_PROPOSAL': pending = dict(kind='LOCAL_PARTIAL', quantity=partial['quantity'], intent_id=partial['intent_id'])
                elif es['action'] == 'REVIEW_PARTIAL_PROPOSAL': pending = dict(kind='ES_PARTIAL', quantity=es['quantity'], intent_id=es['intent_id'])
                elif add_decision is not None:
                    add = add_on_proposal(**args, current_stop=run['stop'], original_risk_budget_usd=run['budget'],
                        committed_risk_usd=self._risk(run), decision=add_decision, additions_done=run['additions_done'], add_pending=False)
                    if add['action'] == 'REVIEW_ADD_PROPOSAL':
                        # Remaining contract capacity counts this simulated position
                        # in addition to reported exposure: overlap cannot be proved.
                        # This can under-size, but cannot silently free capacity.
                        guard = add_decision['account_guard']
                        cap = guard['max_contracts']-run['quantity']
                        q = min(add['quantity'], max(0, cap))
                        if q:
                            pending = dict(kind='ADD', quantity=q, intent_id=add['intent_id'], shared_budget_available=add['shared_budget_available'],
                                expires=min(add_decision['compiled']['plan']['context_valid_until'], add_decision['compiled']['plan']['trigger_available_at']+16))
                if pending:
                    pending['requested_at'] = received_at; run['pending'] = pending
                    self.log(run, received_at, 'SIMULATED_REQUEST', **pending)
                    result = pending['kind']+'_PENDING'
            return dict(state=run['state'], result=result, position_id=run['id'], quantity=run['quantity'], pending=run['pending'])
        return self._event(event_id, payload, received_at, apply)

    @staticmethod
    def _nq_entry_guard(run,quote,now):
        quote=quote or {};at=quote.get('at');value=quote.get('price')
        if not clock(at) or not 0<=now-at<=2 or not price(value):return 'WAITING_FOR_FRESH_NQ_QUOTE'
        boundary=run['decision']['compiled']['plan']['thesis_invalidation']['price']
        return 'NQ_THESIS_INVALIDATED' if run['sign']*(value-boundary)<=0 else 'CLEAR'

    @staticmethod
    def _continuation_blocked(daily_atr,now):
        from entry_controls import continuation_check
        return continuation_check(daily_atr,'continuation',now)['state']!='CLEAR'

    def snapshot(self):
        with self.connect() as db:
            state = json.loads(db.execute('SELECT value FROM simulation_state WHERE id=1').fetchone()[0])
        for run in state['runs']:
            run['net_realized_usd'] = round(run['gross_usd']-run['fees_usd'], 8)
            run['modeled_stop_risk_usd'] = self._risk(run) if run['state'] == 'OPEN' else None
        return dict(mode='SIMULATION_ONLY', orders_sent=0, config=self.config, **state)
