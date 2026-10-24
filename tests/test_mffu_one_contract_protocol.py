"""One-contract 2R native lifecycle with mocked provider; no real broker writes.

Final quote/AI/risk gates are isolated by the existing protocol fixture and
tested separately. This file does not establish provider-side certification.
"""
import json

import pytest

import builder_crosstrade as broker
import test_builder_crosstrade as protocol


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_one_mnq_limit_ack_fill_protection_restart_and_exit(direction):
    f=protocol.NativeTests();f.setUp()
    try:
        sign=1 if direction=='LONG' else -1
        f.order.update(direction=direction,quantity=1,stop_price=30000-sign*10,
                       target_price=30000+sign*20)
        f.prepare();payload=f.payload();payload['quantity']=1
        result=f.t.submit(payload,'order-one');f.client=result['client_id'];f.lifecycles()
        for value in f.life.values():value['version']['orderQty']=1
        body=f.posts[0][1]
        assert body['qty']==1 and body['maxPositions']==1 and body['requireMarketPosition']=='flat'
        assert body['limitPrice']==30000 and body['stopLoss']==f.order['stop_price']
        assert body['takeProfit']==f.order['target_price'] and body['tif']=='gtd'
        assert result['sent'] and f.local()['position_json'] is None
        f.now+=1;f.t.reconcile_one(f.t.row('order-one'),f.snapshot())
        assert f.local()['position_json'] is None  # ACK/Working is not a fill.
        f.life[11]['order']['ordStatus']='Filled'
        for oid in (12,13):f.life[oid]['order']['ordStatus']='Working'
        entry=f.fill(101,11,1,30000,'Buy' if sign==1 else 'Sell')
        snap=f.snapshot([entry],sign)
        f.t.reconcile_one(f.t.row('order-one'),snap)
        assert f.local()['state']=='BROKER_CONFIRMED_OPEN'
        assert json.loads(f.t.row('order-one')['observation_json'])['protection_verified'] is True
        restarted=broker.Transport(f.api,f.sync,request=f.request,clock=lambda:f.now)
        restarted.reconcile_one(restarted.row('order-one'),snap)
        assert len(f.posts)==1  # Restart does not create another entry.
        f.now+=2;exit_fill=f.fill(102,12,1,f.order['target_price'],'Sell' if sign==1 else 'Buy')
        f.life[12]['order']['ordStatus']='Filled';f.life[13]['order']['ordStatus']='Canceled'
        net=40-1.90
        f.account['balance'].update(totalCashValue=48961.55+net,netLiq=48961.55+net)
        f.history=[dict(fillId=x['id'],orderId=x['orderId'],accountId=2214,
            accountName='Builder account',environment='demo',instrument='MNQ 12-26',
            qty=x['qty'],price=x['price'],action=x['action'],timestamp=x['timestamp'],
            commission=.50,fees=.45) for x in (entry,exit_fill)]
        closed=f.snapshot([entry,exit_fill])
        restarted.reconcile_one(restarted.row('order-one'),closed)
        restarted.reconcile_costs(closed)
        assert restarted.resolution('order-one')['net']==pytest.approx(net)
        assert restarted.entry_blocker() is None
        assert len(f.posts)==1  # All exit events above were mocked native OCO.
    finally:f.doCleanups()
