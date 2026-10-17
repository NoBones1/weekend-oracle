"""Tests for the two safety nets. Run: python3 -m unittest discover -s tests -v"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import cache_guard  # noqa: E402
import health  # noqa: E402

UTC = dt.timezone.utc


class CacheGuard(unittest.TestCase):
    before = {"xyz_NVDA.json": {"n": 5000, "first": 100}, "xyz_GOLD.json": {"n": 4000, "first": 200}}

    def test_growth_is_fine(self):
        after = {"xyz_NVDA.json": {"n": 5010, "first": 100}, "xyz_GOLD.json": {"n": 4010, "first": 200}, "new.json": {"n": 3, "first": 9}}
        self.assertEqual(cache_guard.check(self.before, after), [])

    def test_shrinking_is_caught(self):
        after = {"xyz_NVDA.json": {"n": 4999, "first": 100}, "xyz_GOLD.json": {"n": 4000, "first": 200}}
        self.assertEqual(len(cache_guard.check(self.before, after)), 1)

    def test_lost_file_is_caught(self):
        self.assertEqual(len(cache_guard.check(self.before, {"xyz_NVDA.json": {"n": 5000, "first": 100}})), 1)

    def test_lost_oldest_hours_is_caught(self):
        after = {"xyz_NVDA.json": {"n": 5000, "first": 160}, "xyz_GOLD.json": {"n": 4000, "first": 200}}
        self.assertEqual(len(cache_guard.check(self.before, after)), 1)


class Freshness(unittest.TestCase):
    def test_monday_before_15_utc_expects_previous_weekend(self):
        self.assertEqual(health.expected_weekend(dt.datetime(2026, 10, 19, 14, 0, tzinfo=UTC)), dt.date(2026, 10, 10))

    def test_monday_after_15_utc_expects_this_weekend(self):
        self.assertEqual(health.expected_weekend(dt.datetime(2026, 10, 19, 15, 30, tzinfo=UTC)), dt.date(2026, 10, 17))

    def test_stale_data_is_a_problem(self):
        data = {"overall": {"n": 3000}, "coverage": {"last_weekend": "2026-10-10"}, "settings": {}}
        self.assertTrue(health.problems(data, dt.datetime(2026, 10, 19, 16, tzinfo=UTC)))
        self.assertFalse(health.problems(data, dt.datetime(2026, 10, 18, 22, tzinfo=UTC)))


if __name__ == "__main__":
    unittest.main()
