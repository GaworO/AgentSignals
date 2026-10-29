import copy
import pytest
from broker_history_evidence import bracket

def records():
    rows=[]
    for i,kind,action,status,oco,parent in [(1,'Limit','Sell','Filled',None,None),(2,'Stop','Buy','Filled',3,None),(3,'Limit','Buy','Canceled',2,1)]:
        o=dict(id=i,accountId=9,contractId=7,action=action,ordStatus=status)
        if oco:o['ocoId']=oco
        if parent:o['parentId']=parent
        if i==1:o.update(linkedId=3,clOrdId='SYNTHETIC',correlationClOrdId='SYNTHETIC')
        v=dict(orderId=i,orderQty=5,orderType=kind,timeInForce='GTD' if i==1 else 'GTC')
        v['stopPrice' if kind=='Stop' else 'price']={1:100,2:110,3:80}[i]
        rows.append(dict(broker_id=i,observed_ms=1000,payload=dict(order=o,version=v)))
    return rows

def run(r):
    return bracket(r,account_id=9,contract_id=7,entry_id=1,stop_id=2,target_id=3)

def test_realistic_record_shape_is_only_historical_scope():
    v=run(records())
    assert v['observed_protected_quantity']==5
    assert v['historical_native_oso_oco_verified']
    assert not v['current_q1_broker_run_verified']
    assert not v['restart_recovery_verified']

@pytest.mark.parametrize('index,part,key,value',[
 (1,'version','orderQty',4),(1,'order','ocoId',99),(0,'order','accountId',8),
 (2,'order','contractId',8),(2,'order','parentId',99),(0,'order','ordStatus','Working'),
 (1,'order','action','Sell'),(1,'version','stopPrice',90),(0,'order','correlationClOrdId','OTHER')])
def test_changed_quantity_identity_link_or_state_cannot_be_certified(index,part,key,value):
    r=records();r[index]['payload'][part][key]=value
    with pytest.raises(ValueError):run(r)
