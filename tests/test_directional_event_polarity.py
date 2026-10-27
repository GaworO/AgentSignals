"""Direction-aware revalidation. Mock AI only; no broker or paid requests."""
import json

import pytest
import ab_ai_context as ai
import directional_ai_review as gate
import directional_hybrid_lifecycle as life
from test_directional_hybrid import h, candidate, mocked_response
from test_directional_setup_review import service


@pytest.mark.parametrize('kind', ['MSS','BOS','STRUCTURE_BREAK','SWEEP_RECLAIM',
                                  'IFVG','OBSERVED_LIQUIDITY_TAKEN'])
@pytest.mark.parametrize('symbol', ['MNQ','NQ'])
@pytest.mark.parametrize('side,event_direction,blocks', [
    ('LONG','UP',False),('LONG','DOWN',True),
    ('SHORT','DOWN',False),('SHORT','UP',True)])
def test_direction_matrix(kind,symbol,side,event_direction,blocks):
    result=life.structure_event_reason([dict(kind=kind,symbol=symbol,direction=event_direction)],side)
    assert result==('HYBRID_NEW_STRUCTURE_EVENT' if blocks else None)


def test_recorded_oct27_events_support_long_but_block_short():
    # Authentic 10:35 NY events that invalidated the 10:34 LONG in production.
    events=[dict(kind='IFVG',symbol=symbol,timeframe=5,direction='UP',
                 available_at=1793111700,price=price)
            for symbol,price in [('NQ',29626.5),('MNQ',29626.75)]]
    assert life.structure_event_reason(events,'LONG') is None
    assert life.structure_event_reason(events,'SHORT')=='HYBRID_NEW_STRUCTURE_EVENT'
    assert life.structure_event_reason(events)=='HYBRID_NEW_STRUCTURE_EVENT'


@pytest.mark.parametrize('event', [dict(kind='MSS',symbol='MNQ'),
    dict(kind='NEW_UNKNOWN_KIND',symbol='MNQ',direction='UP'),
    dict(kind='MSS',symbol=None,direction='UP'),
    dict(kind='MSS',symbol='MNQ',direction='MIXED')])
def test_unknown_is_not_silently_treated_as_support(event):
    assert life.structure_event_reason([event],'LONG')=='HYBRID_STRUCTURE_EVENT_UNCLASSIFIED'


def test_revision_and_opposition_cannot_hide_behind_aligned_event():
    aligned=dict(kind='IFVG',symbol='MNQ',direction='UP')
    opposite=dict(kind='BOS',symbol='NQ',direction='DOWN')
    assert life.structure_event_reason([aligned,opposite],'LONG')=='HYBRID_NEW_STRUCTURE_EVENT'
    assert life.structure_event_reason([opposite,aligned],'LONG')=='HYBRID_NEW_STRUCTURE_EVENT'
    assert life.structure_event_reason([aligned,dict(kind='DATA_REVISION')],'LONG')=='HYBRID_DATA_REVISED'
    assert life.structure_event_reason([dict(kind='MSS',symbol='ES',direction='DOWN')],'LONG') is None


def stable_source(h):
    def read():
        source=h.source();p=source['context']['packet']
        for e in p['evidence'].values():
            if e['kind']=='bar' and e['symbol']=='MNQ' and e['timeframe']==1:
                e.update(low=99,high=101)
        p['packet_id']=ai.digest({k:v for k,v in p.items() if k!='packet_id'})
        return source
    return read


@pytest.mark.parametrize('side', ['LONG','SHORT'])
@pytest.mark.parametrize('opposing', [False,True])
def test_inflight_review_persists_direction_aware_result(h,side,opposing):
    h.support.reader=stable_source(h)
    def transport(payload):
        response=mocked_response(h,payload);h.now+=60
        direction='UP' if side=='LONG' else 'DOWN'
        if opposing:direction='DOWN' if direction=='UP' else 'UP'
        with h.support.connect() as db:
            db.execute('INSERT INTO hybrid_events VALUES(?,?,?)',('new',h.now,json.dumps(
                dict(kind='IFVG',symbol='MNQ',direction=direction))))
        return response
    h.support.transport=transport
    h.support.queue_setup(candidate(h,side));assert h.support.process_one()
    assert gate.check(side,'one')[0]==('v3_ai:invalidated' if opposing else None)
    with h.support.connect() as db:
        row=db.execute('SELECT status,error_code FROM reviews').fetchone()
        assert row['status']==('invalidated' if opposing else 'validated')
        assert row['error_code']==('HYBRID_NEW_STRUCTURE_EVENT' if opposing else None)
    assert len(h.calls)==1


@pytest.mark.parametrize('failure', ['stop','target','expiry','revision','missing_bar'])
def test_aligned_events_never_bypass_other_guards(h,failure):
    h.support.reader=stable_source(h)
    h.support.queue_setup(candidate(h));h.support.process_one()
    h.now+=60
    p=h.support.packet(h.support.reader(),h.now)
    row=next(e for e in p['evidence'].values() if e['kind']=='bar'
             and e['symbol']=='MNQ' and e['timeframe']==1 and e['available_at']==p['as_of'])
    if failure=='stop':row['low']=90
    if failure=='target':row['high']=120
    if failure=='missing_bar':
        p['evidence']={k:e for k,e in p['evidence'].items() if e is not row}
    life.refresh(h.support,p)
    with h.support.connect() as db:
        events=[dict(kind='IFVG',symbol='MNQ',direction='UP')]
        if failure=='revision':events.append(dict(kind='DATA_REVISION'))
        for i,event in enumerate(events):
            db.execute('INSERT INTO hybrid_events VALUES(?,?,?)',(str(i),h.now,json.dumps(event)))
    if failure=='expiry':h.now+=61
    expected={'stop':'hybrid_original_bracket_touched','target':'hybrid_original_bracket_touched',
              'expiry':'expired','revision':'hybrid_data_revised','missing_bar':'hybrid_setup_history_incomplete'}
    assert gate.check('LONG','one')[0]=='v3_ai:'+expected[failure]
