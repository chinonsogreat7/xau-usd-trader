import unittest
from dataclasses import replace
from datetime import timedelta

from session_feature_fixture import session_fixture
from xau_trader.session_calendar import ScheduledClosure
from xau_trader.session_timing import next_open_start, open_bar_count, validate_calendar


class SessionTimingTests(unittest.TestCase):
    def setUp(self):
        self.bars, self.calendar = session_fixture()
        self.closure = self.calendar.closures[0]

    def test_open_counts_preserve_real_time_and_remove_only_closure(self):
        self.assertEqual(open_bar_count(self.calendar, self.calendar.coverage_start,
                                       self.calendar.coverage_end, timedelta(minutes=15)), 184)
        self.assertEqual(open_bar_count(self.calendar, self.calendar.coverage_start,
                                       self.calendar.coverage_end, timedelta(hours=1)), 46)
        self.assertEqual(open_bar_count(self.calendar, self.closure.start,
                                       self.closure.end, timedelta(hours=1)), 0)
        self.assertEqual(open_bar_count(self.calendar, self.closure.start,
                                       self.closure.end + timedelta(hours=3), timedelta(hours=1)), 3)

    def test_next_open_skips_only_known_closure_and_marks_exhaustion(self):
        self.assertEqual(next_open_start(self.calendar, self.closure.start), self.closure.end)
        self.assertEqual(next_open_start(self.calendar, self.closure.start + timedelta(minutes=15)),
                         self.closure.end)
        self.assertEqual(next_open_start(self.calendar, self.closure.end), self.closure.end)
        self.assertEqual(next_open_start(self.calendar, self.calendar.coverage_end),
                         self.calendar.coverage_end)
        with self.assertRaises(ValueError):
            next_open_start(self.calendar, self.calendar.coverage_end + timedelta(minutes=15))

    def test_invalid_timing_cannot_waive_calendar_or_alignment(self):
        validate_calendar(None)
        with self.assertRaises(TypeError):
            next_open_start(None, self.closure.start)
        with self.assertRaises(TypeError):
            validate_calendar(object())
        with self.assertRaises(ValueError):
            open_bar_count(self.calendar, self.closure.start, self.closure.end, timedelta(minutes=5))
        with self.assertRaises(ValueError):
            open_bar_count(self.calendar, self.closure.end, self.closure.start, timedelta(hours=1))
        with self.assertRaises(ValueError):
            open_bar_count(self.calendar, self.closure.start + timedelta(minutes=15),
                           self.closure.end, timedelta(hours=1))
        with self.assertRaises(ValueError):
            next_open_start(self.calendar, self.closure.start.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            validate_calendar(replace(self.calendar, closures=(ScheduledClosure(
                self.closure.start + timedelta(minutes=15), self.closure.end,
                "Synthetic partial hour is not supported",
            ),)))


if __name__ == "__main__":
    unittest.main()
