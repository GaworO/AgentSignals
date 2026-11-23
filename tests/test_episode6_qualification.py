"""Focused source/geometry, chronology and no-fill regression guards."""
import copy
import unittest
from services.tanja.ict_agent.episode6_qualification import (
    assess_conditions, classification, retest_observation, target_inventory,
    target_availability_at_retest, make_chart)


def candle(ident,ts,low,high,close=None):
    return dict(id=ident,ts=ts,end=ts+60,available_at=ts+60,timeframe=1,
                symbol='MNQ',contract='archive',low=low,high=high,
                open=high-1,close=low+1 if close is None else close)


class FakeDB:
    def execute(self,*args):return []


class LocalArchive:
    def __init__(self,records,bars):
        self.db=FakeDB();self.symbol='MNQ';self.contract='archive';self.by_id={x['id']:x for x in records}
        self.bars=bars;self.levels=[];self.bull_zones=[]
    def record(self,i):return self.by_id[i]
    def between(self,a,b):return [f for f in self.bars if a<f['available_at']<=b]
    def closed(self,tf,at,count):return [f for f in self.bars if f['available_at']<=at][-count:]
    def lowest_after(self,a,b):
        values=[f['low'] for f in self.between(a,b)]
        return min(values) if values else None


def fixture():
    a=candle('C1',560,100,105);b=candle('C2',620,95,104);c=candle('C3',680,94,98)
    rf=candle('raidbar',360,105,111,109)
    ref=dict(id='high',available_at=300,price=110,ts=180,pivot_ts=180)
    low=dict(id='low',available_at=350,price=96,ts=240,pivot_ts=240)
    raid=dict(id='raid',kind='SWEEP',available_at=420,ts=360,extreme=111,price=110,evidence_ids=['high','raidbar'])
    disp=dict(id='disp',kind='DISPLACEMENT',available_at=680,ts=620,evidence_ids=['C2'],definition='ENGINEERING_ONLY')
    cross=dict(id='cross',kind='STRUCTURE_BREAK',available_at=680,ts=620,price=96,evidence_ids=['low','C2'])
    gap=dict(id='gap',kind='FVG',available_at=740,ts=680,lower=98,upper=100,evidence_ids=['C1','C2','C3'])
    chain=dict(raid=raid,displacement=disp,cross=cross,fvg=gap,c1=a,c2=b,c3=c)
    touch=candle('touch',740,97,112,99)
    records=[a,b,c,rf,ref,low,raid,disp,cross,gap,touch]
    archive=LocalArchive(records,[rf,a,b,c,touch])
    mandatory=['E6R-01','E6R-03','E6R-04','E6R-05','E6R-06','E6R-07','E6R-09','E6R-10','E6R-11','E6R-12','E6R-13','E6R-15','E6R-16','E6R-17','E6R-19']
    source={'conditions':[dict(id=f'E6R-{i:02}',title='source condition',semantic_role='test',
              source_rule_ids=['real-source-rule'],requirement='MANDATORY' if f'E6R-{i:02}' in mandatory else 'OPTIONAL',scope='source') for i in range(1,21)],
            'qualification_logic':{'mandatory_condition_ids':mandatory}}
    return archive,chain,source


class Episode6QualificationTests(unittest.TestCase):
    def test_zone_overlap_is_not_source_return_or_fill(self):
        archive,chain,source=fixture();retest=retest_observation(archive,chain,90)
        rows,_,_=assess_conditions(archive,{'symbol':'MNQ','contract':'archive'},chain,retest,[],[],source)
        result={r['condition_id']:r for r in rows}['E6R-15']
        self.assertEqual(retest['status'],'VERIFIED')
        self.assertEqual(result['geometry_status'],'VERIFIED')
        self.assertEqual(result['status'],'CONTEXT_UNRESOLVED')
        self.assertFalse(retest['fill_inferred'])
        self.assertTrue(retest['touch_wick_reference_checks']['high_above_C2_high'])
        self.assertFalse(retest['intrabar_order_verified'])

    def test_generic_fvg_can_fail_explicit_c3_condition(self):
        archive,chain,source=fixture();chain['c3']['low']=96
        rows,geometry,_=assess_conditions(archive,{'symbol':'MNQ','contract':'archive'},chain,retest_observation(archive,chain,90),[],[],source)
        by={r['condition_id']:r for r in rows}
        self.assertTrue(geometry['bearish_nonoverlap'])
        self.assertEqual(by['E6R-12']['status'],'VERIFIED')
        self.assertEqual(by['E6R-11']['status'],'FAILED')
        self.assertEqual(classification(rows,source['qualification_logic']['mandatory_condition_ids'])[0],'SOURCE_CONDITION_FAILED')

    def test_formation_candle_is_not_subsequent_retest(self):
        archive,chain,_=fixture();archive.bars=[chain['c3']]
        result=retest_observation(archive,chain,90)
        self.assertIsNone(result['first_available_touch'])
        self.assertEqual(result['status'],'DATA_UNAVAILABLE')

    def test_missing_bar_prevents_earliest_retest_claim(self):
        archive,chain,_=fixture();archive.bars[-1]['ts']+=60;archive.bars[-1]['end']+=60;archive.bars[-1]['available_at']+=60
        result=retest_observation(archive,chain,90)
        self.assertEqual(result['status'],'DATA_UNAVAILABLE');self.assertTrue(result['missing_intervals'])

    def test_post_raid_low_fails_but_overlap_retains_uncertainty(self):
        archive,chain,source=fixture();retest=retest_observation(archive,chain,90)
        for ts,expected in [(450,'FAILED'),(360,'CONTEXT_UNRESOLVED'),(240,'CONTEXT_UNRESOLVED')]:
            archive.by_id['low']['pivot_ts']=ts
            rows,_,_=assess_conditions(archive,{'symbol':'MNQ','contract':'archive'},chain,retest,[],[],source)
            self.assertEqual(next(r for r in rows if r['condition_id']=='E6R-04')['status'],expected)

    def test_execution_and_optional_conditions_do_not_block_market_classification(self):
        _,_,source=fixture()
        rows=[{'condition_id':c['id'],'status':'VERIFIED' if c['id'] in source['qualification_logic']['mandatory_condition_ids'] else 'DATA_UNAVAILABLE'} for c in source['conditions']]
        self.assertEqual(classification(rows,source['qualification_logic']['mandatory_condition_ids'])[0],'SOURCE_QUALIFIED_HISTORICAL_SETUP')

    def test_future_targets_excluded_and_retest_consumption_separate(self):
        archive,chain,_=fixture()
        archive.levels=[dict(id='oldlow',side='LOW',price=97,available_at=740,kind='PIVOT',timeframe=1),
                        dict(id='futurelow',side='LOW',price=96,available_at=801,kind='PIVOT',timeframe=1)]
        targets=target_inventory(archive,740,98)
        self.assertEqual([x['id'] for x in targets],['oldlow'])
        archive.bars[-1]['low']=96
        later=target_availability_at_retest(archive,targets,740,800)
        self.assertTrue(later[0]['consumed_before_or_on_retest'])
        self.assertFalse(targets[0]['strictly_crossed_asof'])
        self.assertEqual(later[0]['source_preference'],'UNRESOLVED')

    def test_chart_uses_actual_displacement_close_and_bounded_past(self):
        archive,chain,_=fixture();retest=retest_observation(archive,chain,90)
        chart=make_chart(archive,chain,retest,[],800)
        mark=next(x for x in chart['annotations'] if x['kind']=='DISPLACEMENT')
        self.assertEqual(mark['price'],chain['c2']['close'])
        self.assertTrue(all(f['available_at']<=800 for f in chart['frames']['1']))
        self.assertFalse(chart['orders_or_fills'])

if __name__=='__main__':unittest.main()
