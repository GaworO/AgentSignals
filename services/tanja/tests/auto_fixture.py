from datetime import datetime
from auto_selector import NY

def fixture():
    day=datetime(2026,10,8,tzinfo=NY)
    origin=int(day.timestamp());start=origin+570*60
    nq=[]
    for t in range(origin,start,60):
        b=dict(time=t,open=111,high=112,low=110,close=111)
        if t==origin+360*60:b.update(high=125)
        if t==start-5*60:b.update(low=100)
        if t==start-60:b.update(high=113)
        nq.append(b)
    for i,(o,h,l,c) in enumerate([(111,112,108,109),(106,108,98,103),(102,104,100,103),(103,115,102,114.75)]):
        nq.append(dict(time=start+i*60,open=o,high=h,low=l,close=c))
    mnq=[dict(b,**{k:b[k]+1 for k in ('open','high','low','close')}) for b in nq]
    es=[dict(time=b['time'],open=60,high=61,low=59,close=60) for b in nq]
    es[570-5]['low']=50
    for b in es[570:]:b['low']=51
    return dict(NQ=nq,MNQ=mnq,ES=es),start+4*60

