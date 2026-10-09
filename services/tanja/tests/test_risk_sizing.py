import unittest
from risk_sizing import risk_budget,contracts_for_stop

class RiskSizingTests(unittest.TestCase):
    def test_current_balance_and_cents_rounded_down(self):
        self.assertEqual(risk_budget(48234.56),241.17)
        self.assertEqual(risk_budget(48234.99),241.17)
        self.assertEqual(risk_budget(48000),240)
    def test_stop_size_both_directions(self):
        for direction,stop in [('long',29960),('short',30040)]:
            self.assertEqual(contracts_for_stop(30000,stop,direction,241.17,40),3)
        self.assertEqual(contracts_for_stop(30000,29980,'long',241.17,40),6)
        self.assertEqual(contracts_for_stop(30000,29920,'long',241.17,40),1)
    def test_skip_instead_of_rounding_up(self):
        self.assertEqual(contracts_for_stop(30000,29800,'long',241.17,40),0)
        self.assertEqual(contracts_for_stop(30000,29999.75,'long',241.17,40),40)
        self.assertEqual(contracts_for_stop(30000,29990,'long',241.17,2),2)
    def test_invalid_inputs(self):
        for balance in (True,0,-1,float('nan'),float('inf'),'50000'):
            with self.assertRaises(ValueError):risk_budget(balance)
        for stop in (30000,30001,29999.9):
            with self.assertRaises(ValueError):contracts_for_stop(30000,stop,'long',241.17,40)
