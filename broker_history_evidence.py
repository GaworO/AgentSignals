"""Read-only validation of archived native bracket records; no trading authority.

A historical filled bracket proves that sample's quantities and linkage, not
the current strategy's live lifecycle, partial-fill repair or restart behavior.
"""
import hashlib
import json


def bracket(records, *, account_id, contract_id, entry_id, stop_id, target_id):
    ids=(entry_id,stop_id,target_id)
    if len(set(ids))!=3 or any(type(i) is not int or i<=0 for i in ids):
        raise ValueError('distinct_broker_ids_required')
    by_id={}
    for row in records:
        if row['broker_id'] in by_id:raise ValueError('duplicate_broker_record')
        by_id[row['broker_id']]=row
    values=[]
    for i in ids:
        row=by_id[i];p=row.get('payload')
        if p is None:p=json.loads(row['payload_json'])
        o,v=p['order'],p['version']
        if (o['id']!=i or v['orderId']!=i or o['accountId']!=account_id
                or o['contractId']!=contract_id or row['observed_ms']<=0):
            raise ValueError('broker_record_identity_mismatch')
        if type(v['orderQty']) is not int or not 1<=v['orderQty']<=40:
            raise ValueError('broker_record_quantity_invalid')
        values.append((o,v,row['observed_ms']))
    (entry,ev,et),(stop,sv,st),(target,tv,tt)=values
    if len({ev['orderQty'],sv['orderQty'],tv['orderQty']})!=1:
        raise ValueError('protective_quantities_mismatch')
    if (entry['ordStatus']!='Filled' or {stop['ordStatus'],target['ordStatus']}!={'Filled','Canceled'}
            or ev['orderType']!='Limit' or ev['timeInForce']!='GTD'
            or sv['orderType']!='Stop' or tv['orderType']!='Limit'):
        raise ValueError('terminal_native_bracket_required')
    if (stop.get('ocoId')!=target_id or target.get('ocoId')!=stop_id
            or entry.get('linkedId') not in (stop_id,target_id)
            or not (stop.get('parentId')==entry_id or target.get('parentId')==entry_id)):
        raise ValueError('native_parent_and_oco_linkage_required')
    if not entry.get('clOrdId') or entry['clOrdId']!=entry.get('correlationClOrdId'):
        raise ValueError('client_correlation_required')
    if entry['action'] not in ('Buy','Sell'):raise ValueError('unknown_broker_side')
    sign=1 if entry['action']=='Buy' else -1
    opposite='Sell' if sign==1 else 'Buy'
    if stop['action']!=opposite or target['action']!=opposite:
        raise ValueError('protective_side_mismatch')
    if not sign*(ev['price']-sv['stopPrice'])>0 or not sign*(tv['price']-ev['price'])>0:
        raise ValueError('historical_bracket_geometry_invalid')
    encoded=json.dumps([by_id[i] for i in ids],sort_keys=True,separators=(',',':')).encode()
    return dict(evidence_kind='HISTORICAL_BASIC_NATIVE_BRACKET',
        broker_order_ids=list(ids),observed_protected_quantity=ev['orderQty'],
        account_id=account_id,contract_id=contract_id,clOrdId=entry['clOrdId'],
        historical_native_oso_oco_verified=True,source_sha256=hashlib.sha256(encoded).hexdigest(),
        last_observed_ms=max(et,st,tt),sample_count=1,
        current_q1_broker_run_verified=False,current_directional_broker_run_verified=False,
        partial_fill_repair_verified=False,restart_recovery_verified=False,
        note='Archived broker sample only. Strategy applicability is a separate code-path assessment; no permission to trade.')
