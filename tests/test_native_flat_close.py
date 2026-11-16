import copy
import json
from pathlib import Path
from unittest.mock import patch
import pytest
import native_flat_close
import builder_crosstrade as native
import directional_account_reconciliation as risk
import test_builder_crosstrade as protocol


def fixture():
    d=json.loads((Path(__file__).parent/'fixtures/closed_mnq_empty_ack.json').read_text())
    d['lives']={int(k):v for k,v in d['lives'].items()}
    return d


def rebind(d):
    d['risk']['native_settlement']['snapshot_sha256']=risk.digest(d['snapshot'])
    d['risk']['native_settlement']['rules_sha256']=risk.digest(d['proof'])


def test_recorded_four_contract_round_trip():
    p=native_flat_close.prove(**fixture())
    assert p['entry_quantity']==p['exit_quantity']==4
    assert (p['gross_usd'],p['captured_cost_usd'],p['net_usd'])==(54,7.52,46.48)
    assert p['close_order_id']==641664660938 and p['remaining_quantity']==0


@pytest.mark.parametrize('case',[
    'stale','slow_read','open_position','working_order','missing_entry','missing_exit','partial_exit',
    'wrong_exit_side','wrong_account','wrong_contract','duplicate_fill','extra_fill','future_fill',
    'missing_fees','revised_fees','wrong_net','cash_mismatch','missing_group','extra_group_fill',
    'risk_unknown','risk_missing','risk_binding','risk_hash','expired_rules','changed_rules',
    'unknown_ack','failed_ack','warning_ack','wrong_ack_account','wrong_api','wrong_instrument',
    'wrong_action','sending_action','other_action_order','missing_lifecycle','working_child',
    'wrong_oco','wrong_parent','wrong_client','wrong_entry_quantity','wrong_entry_price',
    'no_attributed_position','late_close','different_snapshot_time','unaccounted_fill'])
def test_incomplete_or_conflicting_evidence_never_settles(case):
    d=fixture();entry=d['lives'][641664660909];leg=d['lives'][641664660910]
    group=d['risk']['ledger']['closed_groups'][-1]
    if case=='stale':d['now']+=31
    if case=='slow_read':d['snapshot']['read_started_at']-=20;rebind(d)
    if case=='open_position':d['snapshot']['positions']=[{'netPos':1}];rebind(d)
    if case=='working_order':d['snapshot']['orders']=[{'id':123}];rebind(d)
    if case=='missing_entry':d['fills']=d['fills'][1:]
    if case=='missing_exit':d['fills']=d['fills'][:1]
    if case=='partial_exit':d['fills'][1]['qty']=3
    if case=='wrong_exit_side':d['fills'][1]['action']='Sell'
    if case=='wrong_account':d['fills'][1]['accountId']=999
    if case=='wrong_contract':d['fills'][1]['contractId']=999
    if case=='duplicate_fill':d['fills'].append(d['fills'][1])
    if case=='extra_fill':f=copy.deepcopy(d['fills'][1]);f['id']+=1;d['fills'].append(f)
    if case=='future_fill':d['fills'][1]['timestamp']='2030-01-01T00:00:00Z'
    if case=='missing_fees':group['costs_usd']=0
    if case=='revised_fees':group['costs_usd']+=1
    if case=='wrong_net':group['net_usd']+=1
    if case=='cash_mismatch':d['snapshot']['account']['balance']['totalCashValue']+=1;rebind(d)
    if case=='missing_group':d['risk']['ledger']['closed_groups'].pop()
    if case=='extra_group_fill':group['fill_ids'].append(999)
    if case=='risk_unknown':d['risk']['verified']=False
    if case=='risk_missing':d['risk']['missing']=['unknown_margin']
    if case=='risk_binding':d['risk']['account_binding']='another'
    if case=='risk_hash':d['risk']['native_settlement']['snapshot_sha256']='other'
    if case=='expired_rules':d['risk']['native_settlement']['expires_at_ms']=0
    if case=='changed_rules':d['proof']['test_change']=1
    if case.endswith('_ack') or case in ('wrong_ack_account','wrong_api','wrong_instrument'):
        r=json.loads(d['action']['response_json'])
        if case=='unknown_ack':r={}
        if case=='failed_ack':r['success']=False
        if case=='warning_ack':r['warning']='unknown'
        if case=='wrong_ack_account':r['account']='Other'
        if case=='wrong_api':r['api']='cancel_order'
        if case=='wrong_instrument':r['instrument']='MESZ6'
        d['action']['response_json']=json.dumps(r)
    if case=='wrong_action':d['action']['operation']='cancel'
    if case=='sending_action':d['action']['state']='SENDING'
    if case=='other_action_order':d['action']['order_id']='other'
    if case=='missing_lifecycle':d['lives'].pop(641664660911)
    if case=='working_child':leg['order']['ordStatus']='Working'
    if case=='wrong_oco':leg['order']['ocoId']=999
    if case=='wrong_parent':leg['order']['parentId']=999
    if case=='wrong_client':entry['commands'][0]['clOrdId']='other'
    if case=='wrong_entry_quantity':entry['version']['orderQty']=3
    if case=='wrong_entry_price':entry['version']['price']+=1
    if case=='no_attributed_position':d['audits']=[]
    if case=='late_close':d['action']['updated_ms']+=30000
    if case=='different_snapshot_time':d['risk']['snapshot_started_ms']-=1
    if case=='unaccounted_fill':d['risk']['native_settlement']['accounted_fill_ids']=[]
    assert native_flat_close.prove(**d) is None,case


def test_integration_atomic_restart_no_http_no_orders():
    f=protocol.NativeTests();f.setUp()
    try:
        d=fixture();oid=d['row']['order_id'];f.now=d['now']
        f.sync.binding=d['binding'];f.sync.name=d['name'];f.t.binding=d['binding'];f.t.contract_id=4470324
        f.t.resolve_contract=lambda:4470324
        def no_http(*args,**kwargs):raise AssertionError('settlement must not call broker')
        f.t.request=no_http
        with f.sync.connect() as c:
            for x in d['fills']:c.execute('INSERT OR REPLACE INTO broker_fills VALUES(?,?,?,?,?)',(d['binding'],str(x['id']),f.now,f.now,json.dumps(x)))
        with f.api.connect() as c:
            c.execute('INSERT INTO native_orders VALUES(?,?,?,?,?,?,?,?,?,?,?)',tuple(d['row'][k] for k in ('binding','order_id','client_id','created_ms','expiry_ms','state','command_json','receipt_json','observation_json','error','updated_ms')))
            c.execute('INSERT INTO native_actions VALUES(?,?,?,?,?,?,?,?)',tuple(d['action'][k] for k in ('binding','action_id','order_id','operation','state','payload_json','response_json','updated_ms')))
            for k,v in d['lives'].items():c.execute('INSERT INTO native_lifecycles VALUES(?,?,?,?)',(d['binding'],k,int(f.now*1000),json.dumps(v)))
            for a in d['audits']:c.execute('INSERT INTO native_audit(binding,order_id,at_ms,kind,payload_json) VALUES(?,?,?,?,?)',(d['binding'],oid,a['at_ms'],a['kind'],a['payload_json']))
            c.execute('INSERT INTO orders VALUES(?,?,?,?,?)',(oid,'OPEN',json.dumps({}),json.dumps({'position_id':'ct:641664660909'}),0))
            c.execute('INSERT INTO native_exit_requests VALUES(?,?,?,?)',(d['binding'],oid,'recovery',0))
            c.execute('INSERT INTO native_operator_alerts VALUES(?,?,?,?,0)',(d['binding'],oid,'UNKNOWN',0))
        with patch('directional_account_reconciliation.state',return_value=d['risk']),patch('directional_account_limits.read_static_evidence',return_value=d['proof']):
            f.t.reconcile_one(f.t.row(oid),d['snapshot'])
            assert f.t.row(oid)['state']=='CLOSED'
            with f.api.connect() as c:
                assert c.execute('SELECT state FROM native_actions').fetchone()[0]=='CONFIRMED'
                assert c.execute('SELECT count(*) FROM native_exit_requests').fetchone()[0]==0
                assert c.execute('SELECT resolved FROM native_operator_alerts').fetchone()[0]==1
                assert c.execute('SELECT exit_pnl FROM exit_trades WHERE trade_id=?',(oid,)).fetchone()[0]==46.48
            resumed=native.Transport(f.api,f.sync,request=no_http,clock=lambda:f.now)
            assert resumed.reconcile_flat_close(resumed.row(oid),d['snapshot']) is False
            with patch.object(f.sync,'snapshot',return_value=d['snapshot']):
                assert resumed.entry_blocker() is None
        assert f.posts==[]
    finally:f.doCleanups()


def test_fee_history_uses_supported_query_parameters():
    from urllib.parse import urlparse,parse_qs
    f=protocol.NativeTests();f.setUp()
    try:
        f.submit();f.open();snapshot,_=f.complete();f.t.reconcile_one(f.t.row('order-one'),snapshot)
        original=f.t.request;queries=[]
        def checked(method,path,body=None):
            if path.startswith('/fills/history?'):
                queries.append(path)
                assert set(parse_qs(urlparse(path).query)) <= {'account','environment','limit','cursor'}
            return original(method,path,body)
        f.t.request=checked;f.t.reconcile_costs(snapshot)
        assert queries and f.t.row('order-one')['state']=='CLOSED'
    finally:f.doCleanups()
