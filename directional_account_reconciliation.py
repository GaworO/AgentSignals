"""GET-only account-wide reconciliation. No order or settings writes.

Provider history is captured history, not proof of completeness since account
creation. A bound, dated flat/cash anchor plus capture-coverage evidence is
required before reconstructed losses can authorize an entry. Never infer a
provider floor or margin entitlement from autoLiqLevel or zero margin used.
"""
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import math
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')


def number(x):
    return type(x) in (int, float) and math.isfinite(x)


def millis(value):
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('history_timestamp_timezone_missing')
    return int(d.timestamp()*1000)


def history(sync):
    rows, seen, cursors = [], {}, set()
    cursor = None
    for _ in range(10):
        query = dict(account=sync.name, environment=sync.environment, limit=1000)
        if cursor is not None:
            query['cursor'] = cursor
        response = sync.reader('/fills/history?' + urlencode(query))
        if (not isinstance(response, dict) or response.get('success') is not True
                or response.get('partial') or response.get('unavailable')
                or not isinstance(response.get('data'), list)):
            raise ValueError('durable_history_response_incomplete')
        for f in response['data']:
            if (not isinstance(f, dict) or str(f.get('accountId')) != sync.account
                    or f.get('accountName') != sync.name or f.get('environment') != sync.environment
                    or type(f.get('fillId')) is not int or f['fillId'] <= 0):
                raise ValueError('durable_history_account_binding_mismatch')
            key = f['fillId']
            if key in seen and seen[key] != f:
                raise ValueError('durable_history_duplicate_conflict')
            if key not in seen:
                seen[key] = f
                rows.append(f)
        cursor = response.get('nextCursor')
        if cursor is None:
            return rows
        if not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or cursor in cursors:
            raise ValueError('durable_history_cursor_invalid')
        cursors.add(cursor)
    raise ValueError('durable_history_page_limit')


def margin_row(sync):
    result = sync.reader('/marginsnapshots')
    if not isinstance(result, dict) or result.get('success') is not True or result.get('partial') or result.get('unavailable') or not isinstance(result.get('data'), list):
        raise ValueError('margin_response_incomplete')
    matches = []
    for envelope in result['data']:
        if not isinstance(envelope, dict):
            raise ValueError('margin_identity_unavailable')
        if envelope.get('environment') != sync.environment:
            continue
        data = envelope.get('data')
        if not isinstance(data, list):
            raise ValueError('margin_identity_unavailable')
        matches.extend(r for r in data if isinstance(r, dict) and str(r.get('id')) == sync.account)
    if len(matches) != 1:
        raise ValueError('margin_account_snapshot_missing_or_ambiguous')
    return matches[0]


def reconstruct(rows, anchor, now_ms):
    """Flat-to-flat groups across the whole account, not just bot order IDs."""
    cash, positions, closed = Decimal(str(anchor['cash'])), {}, []
    last_loss = anchor.get('last_loss_closed_ms')
    streak = anchor['consecutive_losses']
    for f in sorted(rows, key=lambda x: (millis(x['timestamp']), x['fillId'])):
        at = millis(f['timestamp'])
        if at > now_ms:
            raise ValueError('history_future_fill')
        if at <= anchor['at_ms']:
            continue
        symbol = f.get('instrument', '')
        # This release knows the MNQ multiplier only. Foreign instruments block
        # account-wide completeness rather than being silently filtered out.
        import re
        if not re.fullmatch(r'MNQ (03|06|09|12)-\d{2}', symbol):
            raise ValueError('account_history_unknown_instrument_multiplier')
        qty, price = f.get('qty'), f.get('price')
        if (type(qty) is not int or qty <= 0 or not number(price) or price <= 0
                or Decimal(str(price)) % Decimal('.25') != 0
                or f.get('action') not in ('Buy', 'Sell')
                or any(not number(f.get(k)) or f[k] < 0 for k in ('commission', 'fees'))):
            raise ValueError('account_fill_or_fees_unknown')
        sign = 1 if f['action'] == 'Buy' else -1
        fees = Decimal(str(f['commission'])) + Decimal(str(f['fees']))
        pos = positions.setdefault(symbol, dict(qty=0, pnl=Decimal(0), fees=Decimal(0)))
        if pos['qty']*sign < 0 and qty > abs(pos['qty']):
            raise ValueError('account_history_reversal_requires_reconciliation')
        pos['qty'] += sign*qty
        pos['pnl'] -= sign*qty*Decimal(str(price))*2
        pos['fees'] += fees
        if not pos['qty']:
            net = pos['pnl'] - pos['fees']
            cash += net
            loss = net < Decimal('-0.005')
            streak = streak+1 if loss else 0
            if loss:
                last_loss = at
            closed.append(dict(closed_ms=at, instrument=symbol, net_usd=float(net), loss=loss))
            pos.update(pnl=Decimal(0), fees=Decimal(0))
    if any(p['qty'] for p in positions.values()):
        raise ValueError('account_history_not_flat')
    day = datetime.fromtimestamp(now_ms/1000, NY).date()
    losses = sum(r['loss'] for r in closed if datetime.fromtimestamp(r['closed_ms']/1000, NY).date() == day)
    return dict(cash=float(cash), losses_today=losses, consecutive_losses=streak,
                last_loss_closed_ms=last_loss, closed_groups=closed)


def assess(snapshot, rows, margin, proof, binding, contract, now_ms):
    result = dict(verified=False, source='CROSSTRADE_ACCOUNT_RECONCILIATION_V1',
                  account_binding=binding, observed_at_ms=now_ms,
                  snapshot_started_ms=int(snapshot['read_started_at']*1000),
                  missing=[], controls=None)
    missing = result['missing']
    b = snapshot['account']['balance']
    result['observed'] = dict(cash=b.get('totalCashValue'), equity=b.get('netLiq'),
        realized_pnl=b.get('realizedPnL'), unrealized_pnl=b.get('openPnL'),
        positions=len(snapshot['positions']), working_orders=len(snapshot['orders']),
        captured_fills=len(rows), margin_snapshot={k:margin.get(k) for k in
            ('timestamp','initialMargin','maintenanceMargin','totalUsedMargin','openCollateralReq')},
        margin_timestamp_semantics='PROVIDER_ROW_LAST_CHANGE_NOT_NEW_POSITION_ALLOWANCE')
    if not 0 <= now_ms-result['snapshot_started_ms'] <= 30000:
        missing.append('account_snapshot_stale')
    if snapshot['positions'] or snapshot['orders']:
        missing.append('account_has_positions_or_orders')
    if any(not number(b.get(k)) for k in ('totalCashValue', 'netLiq', 'realizedPnL', 'openPnL')):
        missing.append('account_pnl_unknown')
    elif abs(b['netLiq']-b['totalCashValue']-b['openPnL']) > .01:
        missing.append('account_equity_unreconciled')
    if (proof.get('verified') is not True or proof.get('account_binding') != binding
            or proof.get('provider') != 'MFFU' or not proof.get('source_reference')
            or not type(proof.get('observed_at_ms')) is int
            or not type(proof.get('expires_at_ms')) is int
            or not proof.get('observed_at_ms', now_ms+1) <= result['snapshot_started_ms'] <= now_ms < proof.get('expires_at_ms', 0)):
        missing.append('current_account_rules_missing_or_expired')
    floor, maximum = proof.get('minimum_balance'), proof.get('max_micro_contracts')
    if not number(floor) or floor <= 0:
        missing.append('current_drawdown_floor_unknown')
    if type(maximum) is not int or maximum <= 0:
        missing.append('account_maximum_mnq_unknown')
    try:
        from directional_account_limits import daily_policy
        daily = daily_policy(proof)
    except ValueError:
        daily = None
        missing.append('account_daily_loss_rule_unknown')
    result['account_rules'] = dict(minimum_balance=floor, max_micro_contracts=maximum,
                                 daily_loss_policy=proof.get('daily_loss_policy'))
    # Full history costs are observed; completeness needs the anchor below.
    if any(any(not number(f.get(k)) or f[k] < 0 for k in ('commission','fees')) for f in rows):
        missing.append('captured_fees_unknown')
    else:
        result['observed']['captured_costs_usd'] = round(sum(f['commission']+f['fees'] for f in rows), 8)
    ids = {f.get('fillId'): f for f in rows}
    for f in snapshot['fills']:
        if f.get('active') is False or f.get('id') not in ids:
            missing.append('live_fill_not_reconciled_with_durable_history')
            break
        h = ids[f['id']]
        if any(f.get(k) != h.get(k) for k in ('orderId', 'qty', 'price', 'action')):
            missing.append('live_and_durable_fill_conflict')
            break
    anchor = proof.get('reconciliation_anchor', {})
    day_start = int(datetime.fromtimestamp(now_ms/1000, NY).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()*1000)
    valid_anchor = (isinstance(anchor, dict) and anchor.get('verified') is True
        and anchor.get('flat') is True and anchor.get('account_binding') == binding
        and bool(anchor.get('source_reference')) and number(anchor.get('cash'))
        and type(anchor.get('at_ms')) is int and 0 < anchor['at_ms'] <= day_start
        and type(anchor.get('capture_complete_since_ms')) is int
        and 0 < anchor['capture_complete_since_ms'] <= anchor['at_ms']
        and bool(anchor.get('coverage_reference'))
        and type(anchor.get('consecutive_losses')) is int and anchor['consecutive_losses'] >= 0
        and (anchor['consecutive_losses'] == 0 or type(anchor.get('last_loss_closed_ms')) is int
             and 0 < anchor['last_loss_closed_ms'] <= anchor['at_ms']))
    ledger = None
    if not valid_anchor:
        missing.append('account_history_coverage_and_flat_anchor_missing')
    else:
        try:
            ledger = reconstruct(rows, anchor, now_ms)
            result['ledger'] = ledger
            if not number(b.get('totalCashValue')) or abs(ledger['cash']-b['totalCashValue']) > .01:
                missing.append('cash_fills_fees_or_transfer_unreconciled')
        except (ValueError,KeyError,TypeError,OverflowError) as exc:
            reason = str(exc)
            missing.append(reason if reason.startswith(('account_history_', 'account_fill_', 'history_')) and reason.replace('_','').isalpha() else 'account_history_invalid')
    rule = proof.get('margin_policy', {})
    cap = None
    if (not isinstance(rule,dict) or rule.get('verified') is not True or not rule.get('source_reference')
            or rule.get('contract') != contract or type(rule.get('observed_at_ms')) is not int
            or type(rule.get('expires_at_ms')) is not int
            or not rule.get('observed_at_ms',now_ms+1) <= now_ms < rule.get('expires_at_ms',0)):
        missing.append('account_margin_rule_unknown_or_expired')
    elif rule.get('mode') == 'CONTRACT_CAP_ONLY':
        cap = maximum if type(maximum) is int and maximum > 0 else None
    elif rule.get('mode') == 'FIXED_PER_CONTRACT':
        fields = ('initialMargin','maintenanceMargin','totalUsedMargin','openCollateralReq')
        per = rule.get('usd_per_contract')
        if (not number(per) or per <= 0 or any(not number(margin.get(k)) or margin[k] < 0 for k in fields)
                or not number(b.get('netLiq')) or not number(b.get('totalCashValue'))):
            missing.append('account_margin_inputs_unknown')
        else:
            available = min(b['netLiq'],b['totalCashValue']) - max(margin[k] for k in fields)
            cap = max(0, int(Decimal(str(available)) // Decimal(str(per))))
    else:
        missing.append('account_margin_rule_unsupported')
    if cap is None and 'account_margin_rule_unknown_or_expired' not in missing:
        missing.append('account_available_margin_unknown')
    result['missing'] = list(dict.fromkeys(missing))
    if not result['missing']:
        remaining = None if daily is None else max(0,daily+min(0,b['realizedPnL'])+min(0,b['openPnL']))
        result['controls'] = dict(verified=True, source_reference=result['source'],
            observed_at_ms=result['snapshot_started_ms'], contract=contract,
            ny_day=datetime.fromtimestamp(now_ms/1000,NY).date().isoformat(),
            cash=b['totalCashValue'], equity=b['netLiq'], fees_reconciled=True,
            losses_today=ledger['losses_today'],consecutive_losses=ledger['consecutive_losses'],
            last_loss_closed_ms=ledger['last_loss_closed_ms'], margin_max_new_micro_contracts=cap,
            remaining_daily_loss_usd=remaining)
        result['verified'] = True
    return result


def refresh(sync, snapshot):
    """Called by the existing account poller; errors persist a blocking result."""
    now = int(sync.clock()*1000)
    result = dict(verified=False, observed_at_ms=now, account_binding=sync.binding,
                  snapshot_started_ms=int(snapshot['read_started_at']*1000), controls=None)
    try:
        import directional_account_limits
        try:
            proof = directional_account_limits.read_static_evidence()
        except (OSError,ValueError,KeyError,TypeError):
            proof = {}
        rows = history(sync)
        margin = margin_row(sync)
        result = assess(snapshot, rows, margin, proof, sync.binding,
                        sync.config.get('AB_V3_CONTRACT'), int(sync.clock()*1000))
        import builder_broker_sync
        result['rules_sha256'] = hashlib.sha256(builder_broker_sync.encode(proof).encode()).hexdigest()
        result['input_sha256'] = hashlib.sha256(json.dumps(dict(fills=rows,margin=margin),sort_keys=True).encode()).hexdigest()
    except Exception:
        result['missing'] = ['account_risk_provider_read_or_schema_failed']
    with sync.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS account_risk_reconciliation(binding TEXT PRIMARY KEY,payload TEXT)')
        db.execute('INSERT OR REPLACE INTO account_risk_reconciliation VALUES(?,?)',
                   (sync.binding,json.dumps(result,sort_keys=True,allow_nan=False)))
    return result


def state(sync=None, *, now_ms=None):
    if sync is None:
        import builder_broker_sync
        sync = builder_broker_sync.instance()
    if not sync.enabled:
        return dict(verified=False,missing=['broker_sync_disabled'])
    now_ms = int(sync.clock()*1000) if now_ms is None else now_ms
    try:
        with sync.connect() as db:
            row=db.execute('SELECT payload FROM account_risk_reconciliation WHERE binding=?',(sync.binding,)).fetchone()
        if not row:
            raise ValueError()
        result=json.loads(row[0])
        snap=sync.snapshot()
        if (not sync.state()['current'] or not snap
                or result.get('snapshot_started_ms')!=int(snap['read_started_at']*1000)
                or not 0 <= now_ms-result['snapshot_started_ms'] <= 30000):
            result.update(verified=False,controls=None,missing=['account_risk_reconciliation_stale'])
        return result
    except Exception:
        return dict(verified=False,controls=None,missing=['account_risk_reconciliation_missing'])
