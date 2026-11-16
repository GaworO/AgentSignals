"""Evidence-only settlement of an empty-body liquidation acknowledgement.

Never infers a fill from HTTP success or flat inventory. No broker I/O. Requires
an exact account-ledger round trip, raw fills, immutable terminal OSO evidence,
a prior attributed position and a current snapshot-bound risk receipt.
"""
import json
from decimal import Decimal


def prove(row, action, lives, fills, audits, snapshot, risk, proof, *, account,
          name, environment, binding, now):
    from builder_crosstrade import stamp, tick, numeric, oco_linked, contract_aliases
    from directional_account_reconciliation import digest
    try:
        cmd = json.loads(row['command_json']); receipt = json.loads(row['receipt_json'])
        order = cmd['order']; reply = json.loads(action['response_json'])
        sr = risk['native_settlement']; balance = snapshot['account']['balance']
        at = int(now*1000)
        if (row['binding'] != binding or action['binding'] != binding
                or action['order_id'] != row['order_id'] or action['operation'] != 'close'
                or action['state'] not in ('UNKNOWN', 'ACKNOWLEDGED')
                or set(receipt) != {'orderId','oso1Id','oso2Id'}
                or len(set(receipt.values())) != 3
                or any(not numeric(i) for i in receipt.values())
                or snapshot['positions'] or snapshot['orders']
                or not 0 <= now-snapshot['observed_at'] <= 3
                or not 0 <= snapshot['observed_at']-snapshot['read_started_at'] <= 12
                or risk.get('verified') is not True or risk.get('missing')
                or risk['account_binding'] != binding or risk['controls']['verified'] is not True
                or risk['controls']['fees_reconciled'] is not True
                or sr['version'] != 1 or sr['snapshot_sha256'] != digest(snapshot)
                or sr['rules_sha256'] != digest(proof) or at >= sr['expires_at_ms']
                or risk['snapshot_started_ms'] != int(snapshot['read_started_at']*1000)
                or str(snapshot['account']['accountId']) != account
                or snapshot['account']['name'] != name
                or snapshot['account']['environment'] != environment):
            return None
        # Only the documented successful empty-body close shape. Timeout,
        # rejected acknowledgement, or a missing action remains unresolved.
        response = reply.get('response', {})
        if (reply.get('success') is not True or reply.get('warning')
                or reply.get('account') != name or reply.get('destination') != 'tradovate'
                or reply.get('api') != 'liquidate_position'
                or response.get('ack') != 'empty_body' or response.get('ok') is not True
                or response.get('orderId') is not None
                or reply.get('instrument') not in contract_aliases(order['contract'])):
            return None
        parent = lives[receipt['orderId']]
        if (parent['order']['ordStatus'] != 'Filled'
                or parent['version']['orderQty'] != cmd['body']['qty']
                or parent['version']['orderType'].lower() != 'limit'
                or parent['version']['price'] != order['entry_price']
                or parent['order']['action'].lower() != cmd['body']['action']
                or not any(c.get('commandType') == 'New' and c.get('clOrdId') == row['client_id'] for c in parent['commands'])):
            return None
        for oid in receipt.values():
            life = lives[oid]
            if (life['order']['id'] != oid or str(life['order']['accountId']) != account
                    or life['order']['contractId'] != cmd['contract_id']
                    or life['version']['orderId'] != oid):
                return None
        legs = [lives[receipt[k]] for k in ('oso1Id','oso2Id')]
        if (not oco_linked(legs[0]['order'],legs[1]['order'])
                or receipt['orderId'] not in [l['order'].get('parentId') for l in legs]
                or any(l['order'].get('parentId') not in (None,receipt['orderId'])
                       or l['order']['ordStatus'] not in ('Canceled','Cancelled','Expired')
                       or l['order']['action'].lower() == cmd['body']['action'] for l in legs)):
            return None
        # Strict subset: one fully closed, uniquely attributable round trip;
        # no interleaved trades or external post-entry account activity.
        recent = [f for f in fills if stamp(f['timestamp']) >= row['created_ms']]
        if not recent or len({f['id'] for f in recent}) != len(recent):return None
        for f in recent:
            if (str(f['accountId']) != account or f.get('accountName') not in (None,name)
                    or f.get('environment') not in (None,environment)
                    or f['contractId'] != cmd['contract_id'] or not numeric(f['id'])
                    or not numeric(f['orderId']) or not numeric(f['qty']) or not tick(f['price'])
                    or stamp(f['timestamp']) > int(snapshot['observed_at']*1000)):
                return None
        ins = [f for f in recent if f['orderId'] == receipt['orderId']]
        outs = [f for f in recent if f['orderId'] != receipt['orderId']]
        eq = sum(f['qty'] for f in ins); xq = sum(f['qty'] for f in outs)
        if (not ins or not outs or eq != cmd['body']['qty'] or xq != eq
                or len({f['orderId'] for f in outs}) != 1
                or any(f['action'].lower() != cmd['body']['action'] for f in ins)
                or any(f['action'].lower() not in ('buy','sell') or f['action'].lower() == cmd['body']['action']
                       or f['orderId'] in receipt.values() for f in outs)):
            return None
        # The captured attributable-position observation must precede every
        # closing fill, which must precede the received close response.
        attributed = []
        for a in audits:
            if a['kind'] != 'LIFECYCLE_PHASE':continue
            e = json.loads(a['payload_json']).get('exposure') or {}
            if (e.get('attributable') is True and e.get('entry_quantity') == eq
                    and e.get('exit_quantity') == 0 and e.get('remaining_quantity') == eq
                    and max(stamp(f['timestamp']) for f in ins) <= a['at_ms']
                    <= min(stamp(f['timestamp']) for f in outs)):
                attributed.append(a['at_ms'])
        if (not attributed or max(stamp(f['timestamp']) for f in outs) > action['updated_ms']
                or action['updated_ms']-max(attributed) > 10000):return None
        ids = sorted(f['id'] for f in recent)
        groups = [g for g in risk['ledger']['closed_groups'] if sorted(g['fill_ids']) == ids]
        if len(groups) != 1 or not set(ids).issubset(sr['accounted_fill_ids']):return None
        group = groups[0]; dec = lambda x: Decimal(str(x))
        entry = sum(dec(f['price'])*f['qty'] for f in ins)/eq
        exit = sum(dec(f['price'])*f['qty'] for f in outs)/eq
        gross = (exit-entry)*(1 if cmd['body']['action']=='buy' else -1)*eq*2
        costs = dec(group['costs_usd']); net = gross-costs
        closed = max(stamp(f['timestamp']) for f in outs)
        if (costs <= 0 or not costs.is_finite() or group['closed_ms'] != closed
                or abs(gross-dec(group['gross_usd'])) > dec('.01')
                or abs(net-dec(group['net_usd'])) > dec('.01')
                or abs(dec(cmd['before_cash'])+net-dec(balance['totalCashValue'])) > dec('.01')
                or abs(dec(risk['ledger']['cash'])-dec(balance['totalCashValue'])) > dec('.01')):
            return None
        return dict(observed_at=snapshot['observed_at'], entry_quantity=eq, exit_quantity=eq,
            remaining_quantity=0, quantity=eq, entry_status='Filled', entry_price=float(entry),
            exit_price=float(exit), fill_ms=min(stamp(f['timestamp']) for f in ins),
            exit_fill_ms=closed, owned_fill_ids=ids, native_active=[], gross_usd=float(gross),
            net_usd=float(net), captured_cost_usd=float(costs), costs_reconciled=True, fees_final=False,
            reconciliation_valid=True, settlement_source='VERIFIED_ACCOUNT_ROUND_TRIP_AND_TERMINAL_OSO',
            close_order_id=outs[0]['orderId'], snapshot_sha256=sr['snapshot_sha256'],
            historical_protection_not_recertified=True)
    except (KeyError,TypeError,ValueError,ArithmeticError):
        return None
