import datetime as dt
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock

import news_calendar as cal
from test_builder_crosstrade import functions, ROOT


def stamp(s):
    return dt.datetime.fromisoformat(s).timestamp()


def json_body(date='2026-10-19T08:30:00-04:00', impact='High'):
    return json.dumps([dict(title='CPI m/m', country='CAD', date=date, impact=impact)]).encode()


def xml_body(date='10-19-2026', time='12:30pm', impact='High'):
    return f'<weeklyevents><event><title>CPI m/m</title><country>CAD</country><date>{date}</date><time>{time}</time><impact>{impact}</impact></event></weeklyevents>'.encode()


def response(status=200, body=b''):
    return types.SimpleNamespace(status_code=status, content=body)


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.now = stamp('2026-10-19T13:00:00+00:00')
        self.path = Path(self.tmp.name) / 'calendar.json'
        self.calendar = cal.Calendar(self.path, clock=lambda:self.now)

    def test_rate_limit_falls_back_to_xml_and_preserves_utc_event(self):
        client = Mock();client.get.side_effect=[response(429,b'<html>Rate limited</html>'),response(body=xml_body())]
        self.calendar.refresh(client)
        self.assertEqual(self.calendar.view()['source'],'ForexFactory/XML')
        self.assertEqual(self.calendar.view()['events'],[(stamp('2026-10-19T12:30:00+00:00'),'CPI m/m')])
        self.assertEqual(self.calendar.age_hours(),0)
        self.calendar.refresh(client)
        self.assertEqual(client.get.call_count,2)
        self.assertTrue(self.path.exists())

    def test_json_and_xml_represent_same_time_in_summer_and_winter(self):
        for jdate,xdate,xtime in [('2026-10-19T08:30:00-04:00','10-19-2026','12:30pm'),
                                 ('2026-12-07T08:30:00-05:00','12-07-2026','1:30pm')]:
            now=stamp(jdate)
            a=cal.normalize(json_body(jdate),'json',now)
            b=cal.normalize(xml_body(xdate,xtime),'xml',now)
            self.assertEqual(a['raw_events'],b['raw_events'])

    def test_malformed_empty_old_week_and_missing_timezone_are_rejected(self):
        cases=[(b'{}','json'),(b'[]','json'),(b'<html>blocked</html>','xml'),
               (json_body('2026-10-12T08:30:00-04:00'),'json'),
               (json_body('2026-10-19T08:30:00'),'json'),(json_body(impact='unknown'),'json'),
               (xml_body(time='Tentative'),'xml'),(xml_body(time='All Day'),'xml'),
               (xml_body(date='10-12-2026'),'xml')]
        for body,kind in cases:
            with self.subTest(kind=kind,body=body),self.assertRaises((ValueError,KeyError)):
                cal.normalize(body,kind,self.now)

    def test_zero_high_events_is_valid_only_with_valid_nonempty_schedule(self):
        value=cal.normalize(xml_body(time='All Day',impact='Holiday'),'xml',self.now)
        self.assertEqual(value['raw_events'],[])
        self.assertEqual(value['event_count'],1)

    def test_all_sources_fail_and_backoff_prevents_per_bar_requests(self):
        client=Mock();client.get.return_value=response(429)
        self.calendar.refresh(client)
        for _ in range(60):
            self.now+=1;self.calendar.refresh(client)
        self.assertEqual(client.get.call_count,2)
        self.assertIsNone(self.calendar.age_hours())
        self.assertIn('JSON:HTTP_429',self.calendar.view()['error'])
        self.now+=cal.RETRY_SECONDS;self.calendar.refresh(client)
        self.assertEqual(client.get.call_count,4)

    def test_failed_refresh_does_not_extend_success_age_even_after_restart(self):
        client=Mock();client.get.return_value=response(body=json_body())
        self.calendar.refresh(client)
        receipt=self.now
        self.now+=7*3600;client.get.return_value=response(503)
        self.calendar.refresh(client)
        self.assertEqual(self.calendar.age_hours(),7)
        self.assertEqual(self.calendar.snapshot['received_at'],receipt)
        restored=cal.Calendar(self.path,clock=lambda:self.now)
        self.assertEqual(restored.age_hours(),7)
        self.now+=18*3600
        self.assertGreater(restored.age_hours(),24)

    def test_late_saturday_new_york_can_spill_into_utc_sunday(self):
        body=xml_body().replace(b'</weeklyevents>', xml_body(date='10-25-2026',time='1:00am',impact='Holiday').replace(b'<weeklyevents>', b''))
        self.assertEqual(cal.normalize(body,'xml',self.now)['event_count'],2)
        with self.assertRaises(ValueError):
            cal.normalize(xml_body(date='10-25-2026'),'xml',self.now)

    def test_current_week_required_even_for_recent_cache(self):
        self.calendar.snapshot=cal.normalize(json_body(),'json',self.now)
        self.path.write_text(json.dumps(self.calendar.snapshot))
        self.now=stamp('2026-10-25T01:00:00+00:00')
        self.assertIsNone(self.calendar.age_hours())
        self.assertEqual(self.calendar.view()['events'],[])
        self.assertIsNone(cal.Calendar(self.path,clock=lambda:self.now).age_hours())

    def test_corrupt_and_future_cache_cannot_clear_gate(self):
        self.path.write_text('{')
        self.assertIsNone(cal.Calendar(self.path,clock=lambda:self.now).age_hours())
        value=cal.normalize(json_body(),'json',self.now);value['received_at']=self.now+3600
        self.path.write_text(json.dumps(value))
        self.assertIsNone(cal.Calendar(self.path,clock=lambda:self.now).age_hours())

    def test_failed_persistence_keeps_only_in_memory_verified_data(self):
        self.calendar.path=Path(self.tmp.name) # directory cannot be replaced by a file
        client=Mock();client.get.return_value=response(body=json_body())
        self.calendar.refresh(client)
        self.assertEqual(self.calendar.age_hours(),0)

    def test_agent_flags_still_block_half_hour_around_high_impact_event(self):
        from zoneinfo import ZoneInfo
        ns=dict(dt=dt,NY=ZoneInfo('America/New_York'),_load_calendar=lambda:None,
                _cal={'events':[(stamp('2026-10-19T12:30:00+00:00'),'CPI m/m')]})
        functions(ROOT/'agent.py',{'flags_for'},ns)
        for offset,expected in [(-1801,False),(-1800,True),(0,True),(1800,True),(1801,False)]:
            _,hard=ns['flags_for']({'bos_ms':(stamp('2026-10-19T12:30:00+00:00')+offset)*1000})
            self.assertEqual(hard,expected)

    def test_guard_records_source_errors_and_remains_closed_without_calendar(self):
        state={};ns=dict(_state=lambda:dict(state),_set_state=lambda s:state.update(s),
                         _now_ms=lambda:1000,_env=lambda k,d:'0' if k=='NEWS_CAL_ALERTS' else d,
                         _envf=lambda k,d:d)
        functions(ROOT/'guardrails.py',{'news_calendar_check'},ns)
        check=ns['news_calendar_check']
        self.assertFalse(check(None,True,calendar_info={'error':'JSON:HTTP_429; XML:HTTP_503'}))
        self.assertIn('HTTP_429',state['news_cal_error'])
        self.assertTrue(check(0,True,calendar_info={'source':'ForexFactory/XML','error':None}))
        self.assertEqual(state['news_cal_source'],'ForexFactory/XML')
        self.assertIsNone(state['news_cal_error'])
        self.assertFalse(check(25,True))


if __name__=='__main__':unittest.main()
