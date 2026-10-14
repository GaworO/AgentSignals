import sys
from pathlib import Path
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from review_calendar import HOLIDAYS, review_day


class ReviewCalendarTests(unittest.TestCase):
    def at(self, value, zone='America/New_York'):
        return review_day(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(zone)).timestamp())

    def test_all_published_observed_dates(self):
        self.assertEqual(len(HOLIDAYS),25)
        for day,name in HOLIDAYS.items():
            with self.subTest(day=day):
                result=self.at(day+'T10:00')
                self.assertEqual(result['status'],'HOLIDAY_NO_AI')
                self.assertEqual(result['holiday'],name)
                self.assertFalse(result['new_reviews_allowed'])

    def test_us_holidays_are_not_exchange_closed_claims(self):
        for day in ('2026-10-12','2026-11-11','2027-12-31'):
            self.assertEqual(self.at(day+'T10:00')['status'],'HOLIDAY_NO_AI')
        # A shortened-session day is not a full holiday under this policy.
        self.assertEqual(self.at('2026-11-27T10:00')['status'],'READY')

    def test_new_york_day_not_utc_or_berlin(self):
        self.assertEqual(self.at('2026-10-10T03:30','UTC')['date'],'2026-10-09')
        self.assertEqual(self.at('2026-10-10T03:30','UTC')['status'],'READY')
        self.assertEqual(self.at('2026-10-10T04:00','UTC')['status'],'WEEKEND_NO_AI')
        self.assertEqual(self.at('2026-10-13T03:59','UTC')['status'],'HOLIDAY_NO_AI')
        self.assertEqual(self.at('2026-10-13T04:00','UTC')['status'],'READY')
        self.assertEqual(self.at('2026-11-07T04:30','UTC')['status'],'READY')
        self.assertEqual(self.at('2026-11-07T05:00','UTC')['status'],'WEEKEND_NO_AI')

    def test_unknown_year_never_assumed_open(self):
        self.assertEqual(self.at('2028-01-04T10:00')['status'],'CALENDAR_UPDATE_REQUIRED')


if __name__=='__main__':unittest.main()
