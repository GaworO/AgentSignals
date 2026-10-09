import unittest
from management_review import propose_stop

class DirectionalManagementTests(unittest.TestCase):
    def args(self):
        bars=[dict(time=t,open=100,high=h,low=95,close=99) for t,h in [(0,101),(60,103),(120,102),(180,101)]]
        return dict(bars=bars,direction='short',entry=105,current_stop=110,opened_at=0,as_of=240,market_price=99,price_known_at=240)
    def test_confirmed_short_high_stop(self):
        r=propose_stop(**self.args());self.assertEqual(r['stop_price'],103.25)
        self.assertEqual(r['anchor_available_at'],180);self.assertFalse(r['is_order'])
    def test_never_loosen(self):
        a=self.args();a['current_stop']=102
        self.assertEqual(propose_stop(**a)['action'],'NONE')
    def test_right_bar_must_close(self):
        a=self.args();a.update(as_of=120,price_known_at=120)
        self.assertEqual(propose_stop(**a)['action'],'NONE')
    def test_news_offset_both_directions(self):
        for side,expected,market,stop in [('short',106.5,100,110),('long',103.5,110,100)]:
            a=self.args();a.update(bars=[],direction=side,entry=105,current_stop=stop,market_price=market,news_at=300,news_known_at=0)
            self.assertEqual(propose_stop(**a)['stop_price'],expected)
            a['news_known_at']=241;self.assertEqual(propose_stop(**a)['action'],'NONE')
    def test_missing_or_revisited_pivot_not_traded(self):
        a=self.args();a['bars'][-1]['time']=240;a.update(as_of=300,price_known_at=300)
        self.assertEqual(propose_stop(**a)['action'],'NONE')
        a=self.args();a['bars'][-1]['high']=103
        self.assertEqual(propose_stop(**a)['action'],'NONE')
    def test_stale_quote_and_news_past(self):
        a=self.args();a['price_known_at']=230
        self.assertEqual(propose_stop(**a)['reason'],'NEEDS_FRESH_QUOTE')
        a=self.args();a.update(bars=[],news_at=240,news_known_at=0)
        self.assertEqual(propose_stop(**a)['action'],'NONE')
