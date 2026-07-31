import datetime as dt
import unittest

import timebase


class TimebaseTests(unittest.TestCase):
    def test_fixed_utc_minus_four_in_winter_and_summer(self):
        jan = dt.datetime(2026, 1, 15, 14, 30, tzinfo=dt.timezone.utc)
        jul = dt.datetime(2026, 7, 15, 13, 30, tzinfo=dt.timezone.utc)
        self.assertEqual(jan.astimezone(timebase.STRATEGY_TZ).utcoffset(), dt.timedelta(hours=-4))
        self.assertEqual(jul.astimezone(timebase.STRATEGY_TZ).utcoffset(), dt.timedelta(hours=-4))
        self.assertEqual(jan.astimezone(timebase.STRATEGY_TZ).strftime('%H:%M'), '10:30')
        self.assertEqual(jul.astimezone(timebase.STRATEGY_TZ).strftime('%H:%M'), '09:30')

    def test_session_boundaries(self):
        def at(h, m):
            return dt.datetime(2026, 7, 1, h, m, tzinfo=timebase.STRATEGY_TZ)
        self.assertEqual(timebase.session_name(at(1, 59)), 'ASIA')
        self.assertEqual(timebase.session_name(at(2, 0)), 'LO')
        self.assertEqual(timebase.session_name(at(5, 0)), 'PREM')
        self.assertEqual(timebase.session_name(at(9, 30)), 'NYAM')
        self.assertEqual(timebase.session_name(at(11, 0)), 'NYL')
        self.assertEqual(timebase.session_name(at(13, 30)), 'NYPM')
        self.assertEqual(timebase.session_name(at(16, 0)), 'PM_AH')
        self.assertEqual(timebase.session_name(at(18, 0)), 'ASIA')

    def test_trading_day_rolls_at_1800(self):
        before = dt.datetime(2026, 7, 1, 17, 59, tzinfo=timebase.STRATEGY_TZ)
        after = dt.datetime(2026, 7, 1, 18, 0, tzinfo=timebase.STRATEGY_TZ)
        self.assertEqual(timebase.trading_day(before), '2026-07-01')
        self.assertEqual(timebase.trading_day(after), '2026-07-02')

    def test_parse_event_ms_is_utc(self):
        self.assertEqual(timebase.parse_event_ms('2026-07-30T12:00:00Z'), 1785412800000)
        self.assertEqual(timebase.parse_event_ms(1785412800), 1785412800000)


if __name__ == '__main__':
    unittest.main()
