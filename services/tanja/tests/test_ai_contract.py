"""Response-contract checks, not evidence of strategy profitability or fidelity."""
import copy
import json
import unittest
import test_ai as fixture
from ai_review import encoded
from context_layer import canonical_hash
from context_v3 import unknown_decision, validate_decision


class TestNeutralContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.TestAI.setUpClass()

    def setUp(self):
        self.case=fixture.TestAI();self.case.setUp();self.addCleanup(self.case.tearDown)
        self.p=copy.deepcopy(self.case.packet)
        key=next(k for k,e in self.p['evidence'].items() if e['kind']=='bar' and e['symbol']=='MNQ' and e['timeframe']==1)
        self.event='synthetic-short-trigger'
        self.p['evidence'][self.event]=dict(kind='IFVG',direction='short',symbol='MNQ',timeframe=1,
            available_at=self.p['as_of'],evidence_ids=[key],lower=100,upper=101,close=99,zone_id='synthetic')
        self.p['packet_id']=canonical_hash({k:v for k,v in self.p.items() if k!='packet_id'})

    def decision(self):
        d=unknown_decision(self.p);d['context'].update(bias='neutral',decision='wait',setup='inversion')
        d['context']['claims']['confirmation_accepted']=dict(value=False,evidence_ids=[self.event],reason='Observed short event is not selected under neutral context.')
        return d

    def test_neutral_can_cite_unselected_directional_observation(self):
        r=validate_decision(self.p,self.decision())
        self.assertTrue(r['valid']);self.assertEqual(r['review_state'],'WAIT');self.assertFalse(r['executable'])

    def test_neutral_or_opposite_selected_trigger_still_rejected(self):
        for bias in ['neutral','unknown','long']:
            d=self.decision();d['context'].update(bias=bias,selected_trigger_id=self.event)
            with self.subTest(bias=bias),self.assertRaisesRegex(ValueError,'Trigger does not support'):
                validate_decision(self.p,d)

    def test_worker_reports_specific_error_and_does_not_retry(self):
        c=copy.deepcopy(self.case.context);c['packet']=self.p
        with self.case.store.connect() as db:db.execute('UPDATE jobs SET packet=?',(encoded(c),))
        def response(payload,key):
            d=self.decision();d['context']['selected_trigger_id']=self.event
            return dict(status='completed',output=[dict(type='message',content=[dict(type='output_text',text=encoded(d))])])
        self.assertTrue(self.case.ai.process_one(response,lambda:self.case.now))
        state=self.case.ai.state(self.case.now,c)
        self.assertEqual(state['records'][0]['error'],'TRIGGER_BIAS_MISMATCH')
        self.assertEqual(state['status'],'PAUSED_AFTER_ERROR')
        self.assertFalse(self.case.ai.process_one(lambda *a:self.fail('Unexpected retry'),lambda:self.case.now+1))


if __name__=='__main__':unittest.main()
