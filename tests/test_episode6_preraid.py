import copy
import unittest
from services.tanja.ict_agent.episode6_preraid import enumerate_references, first_below


def bar(name,ts,low,high=115):
    return dict(id=name,symbol='NQ',contract='dated-test',timeframe=1,ts=ts,end=ts+60,
                available_at=ts+60,open=110,close=low+1,low=low,high=high)


class Archive:
    symbol='NQ';contract='dated-test'
    def __init__(self):
        self.frames={1:[bar('a',0,105),bar('b',60,100),bar('c',120,104),bar('d',180,106),
                       bar('raidbar',240,105),bar('e',300,104),bar('dispbar',360,97),bar('gapbar',420,96)]}
        self.times={1:[x['available_at'] for x in self.frames[1]]}
        self.records={b['id']:b for b in self.frames[1]}
        self.levels=[dict(id='old',kind='PIVOT',side='LOW',symbol='NQ',contract='dated-test',
                        timeframe=1,pivot_ts=60,available_at=180,price=100,evidence_ids=['a','b','c'])]
        self.chain={'raid':dict(id='raid',available_at=300,evidence_ids=['oldhigh','raidbar']),
                    'cross':dict(evidence_ids=['old','dispbar'],timeframe=1),
                    'displacement':dict(id='disp',available_at=420),
                    'fvg':dict(id='gap',available_at=480)}
    def record(self,ident):return self.records[ident]
    def lowest_after(self,start,end):
        values=[b['low'] for b in self.frames[1] if start<b['available_at']<=end]
        return min(values) if values else None


class PreRaidTests(unittest.TestCase):
    def test_true_pre_raid_reference_is_preserved_not_promoted(self):
        a=Archive();r=enumerate_references(a,a.chain)
        self.assertEqual(len(r['witnesses']),1)
        self.assertEqual(r['witnesses'][0]['reference_id'],'old')
        self.assertFalse(r['witnesses'][0]['mss_qualified']);self.assertIsNone(r['selected_reference'])
    def test_post_raid_and_future_reference_never_substituted(self):
        a=Archive();a.levels.extend([dict(a.levels[0],id='post',pivot_ts=300,available_at=420),
                                   dict(a.levels[0],id='future',available_at=481)])
        r=enumerate_references(a,a.chain)
        self.assertEqual([w['reference_id'] for w in r['witnesses']],['old'])
        self.assertEqual(next(x for x in r['references'] if x['id']=='post')['status'],'FORMED_AFTER_RAID')
        self.assertNotIn('future',[x['id'] for x in r['references']])
    def test_all_competing_valid_references_retained(self):
        a=Archive();a.levels.append(dict(a.levels[0],id='other',price=101))
        r=enumerate_references(a,a.chain)
        self.assertEqual({w['reference_id'] for w in r['witnesses']},{'old','other'})
        self.assertIsNone(r['selected_reference'])
    def test_aggregated_pivot_overlaps_raid_not_pre_raid(self):
        a=Archive();a.levels[0].update(timeframe=5,pivot_ts=60)
        r=enumerate_references(a,a.chain)
        self.assertEqual(r['references'][0]['status'],'FORMATION_OVERLAPS_RAID')
        self.assertFalse(r['witnesses'])
    def test_cross_within_raid_is_not_ordered_after_raid(self):
        a=Archive();a.records['raidbar']['low']=99
        r=enumerate_references(a,a.chain)
        self.assertEqual(r['references'][0]['status'],'CROSS_BEFORE_OR_WITHIN_RAID')
        self.assertFalse(r['witnesses'])
    def test_cross_before_displacement_preserved_not_merged(self):
        a=Archive();a.records['e']['low']=99
        r=enumerate_references(a,a.chain)
        self.assertEqual(r['references'][0]['status'],'CROSS_PRECEDES_FROZEN_DISPLACEMENT')
        self.assertFalse(r['witnesses'])
    def test_earlier_consumption_does_not_become_fresh_cross(self):
        a=Archive();a.records['d']['low']=99
        r=enumerate_references(a,a.chain)
        self.assertEqual(r['references'][0]['status'],'RECROSS_CONTEXT_UNRESOLVED')
        self.assertFalse(r['witnesses'])
    def test_first_cross_uses_closed_available_candles(self):
        a=Archive()
        self.assertIsNone(first_below(a,180,419,100))
        self.assertEqual(first_below(a,180,420,100)['id'],'dispbar')
        a.chain['fvg']['available_at']=419
        self.assertFalse(enumerate_references(a,a.chain)['witnesses'])

if __name__=='__main__':unittest.main()
