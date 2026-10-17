"""Tests for the scoring rules. Run: python3 -m unittest discover -s tests -v"""
import datetime as dt
import os
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import oracle as o  # noqa: E402

UTC = ZoneInfo("UTC")
H = o.HOUR


def ms(*a, tz=UTC):
    return int(dt.datetime(*a, tzinfo=tz).timestamp() * 1000)


def candles(start_ms, prices, n=5):
    return [{"t": start_ms + i * H, "o": p, "c": p, "n": n, "v": 1.0} for i, p in enumerate(prices)]


class NoPeeking(unittest.TestCase):
    def test_candle_still_open_at_t_is_not_used(self):
        cs = candles(ms(2026, 10, 9, 18), [100, 101, 102])  # 18:00, 19:00, 20:00 UTC
        # At 19:30 only the 18:00 candle has finished; the 19:00 one closes at 20:00.
        self.assertEqual(o.price_at(cs, ms(2026, 10, 9, 19, 30))["px"], 100)
        # At exactly 20:00 the 19:00 candle has just finished.
        self.assertEqual(o.price_at(cs, ms(2026, 10, 9, 20))["px"], 101)

    def test_nothing_before_first_candle(self):
        cs = candles(ms(2026, 10, 9, 18), [100])
        self.assertIsNone(o.price_at(cs, ms(2026, 10, 9, 18, 59)))


class SundayCallTime(unittest.TestCase):
    def test_summer_time_is_21_utc(self):
        # 2026-10-11 is a Sunday in New York summer time (EDT, UTC-4).
        t = o.sunday_call_time("2026-10-10", ms(2026, 10, 12, 13, 30))
        self.assertEqual(o._utc(t), dt.datetime(2026, 10, 11, 21, 0, tzinfo=UTC))

    def test_winter_time_is_22_utc(self):
        # 2026-11-08 is after the US clocks go back (EST, UTC-5).
        t = o.sunday_call_time("2026-11-07", ms(2026, 11, 9, 14, 30))
        self.assertEqual(o._utc(t), dt.datetime(2026, 11, 8, 22, 0, tzinfo=UTC))

    def test_early_reopen_reads_an_hour_before(self):
        reopen = ms(2026, 10, 11, 20, 0)
        self.assertEqual(o.sunday_call_time("2026-10-10", reopen), reopen - H)


class FindingWeekends(unittest.TestCase):
    def test_daily_nyse_uses_official_close_and_open(self):
        ny = ZoneInfo("America/New_York")
        bars = [{"t": ms(2026, 10, 9, 9, 30, tz=ny), "o": 233.84, "c": 229.28},
                {"t": ms(2026, 10, 12, 9, 30, tz=ny), "o": 228.73, "c": 226.32}]
        (ev,) = o.weekends_daily(bars, "America/New_York")
        self.assertEqual(ev["weekend"], "2026-10-10")
        self.assertEqual(ev["close_px"], 229.28)
        self.assertEqual(ev["open_px"], 228.73)
        self.assertEqual(o._utc(ev["close_t"]), dt.datetime(2026, 10, 9, 20, 0, tzinfo=UTC))
        self.assertEqual(o._utc(ev["open_t"]), dt.datetime(2026, 10, 12, 13, 30, tzinfo=UTC))

    def test_korean_holiday_friday_still_a_weekend(self):
        # Korea shut on Friday 9 Oct 2026 (Hangul Day): Thursday to Monday.
        seoul = ZoneInfo("Asia/Seoul")
        bars = [{"t": ms(2026, 10, 8, 9, 0, tz=seoul), "o": 1, "c": 262000},
                {"t": ms(2026, 10, 12, 9, 0, tz=seoul), "o": 260000, "c": 2}]
        (ev,) = o.weekends_daily(bars, "Asia/Seoul")
        self.assertEqual(ev["weekend"], "2026-10-10")
        self.assertEqual(o._utc(ev["close_t"]), dt.datetime(2026, 10, 8, 6, 30, tzinfo=UTC))

    def test_midweek_holiday_is_not_a_weekend(self):
        ny = ZoneInfo("America/New_York")
        bars = [{"t": ms(2026, 12, 22, 9, 30, tz=ny), "o": 1, "c": 1},
                {"t": ms(2026, 12, 24, 9, 30, tz=ny), "o": 1, "c": 1}]  # Tue -> Thu: a shut Wednesday, no Saturday
        self.assertEqual(o.weekends_daily(bars, "America/New_York"), [])

    def test_hourly_futures_gap(self):
        bars = [{"t": ms(2026, 10, 9, 20), "o": 4220.2, "c": 4216.3},
                {"t": ms(2026, 10, 11, 22), "o": 4216.7, "c": 4210.7},
                {"t": ms(2026, 10, 11, 23), "o": 4211.1, "c": 4215.5}]
        (ev,) = o.weekends_hourly(bars)
        self.assertEqual(ev["close_px"], 4216.3)
        self.assertEqual(ev["open_px"], 4216.7)
        self.assertEqual(o._utc(ev["close_t"]), dt.datetime(2026, 10, 9, 21, tzinfo=UTC))

    def test_daily_halt_is_not_a_weekend(self):
        bars = [{"t": ms(2026, 10, 12, 20), "o": 1, "c": 1}, {"t": ms(2026, 10, 12, 22), "o": 1, "c": 1}]
        self.assertEqual(o.weekends_hourly(bars), [])


class Scoring(unittest.TestCase):
    def test_direction(self):
        self.assertEqual(o.direction(0.02, 0.01), "hit")
        self.assertEqual(o.direction(0.02, -0.01), "miss")
        self.assertEqual(o.direction(0.0005, 0.03), "no_call")
        self.assertEqual(o.direction(0.02, 0.0), "miss")

    def test_beat_friday(self):
        self.assertTrue(o.beat_friday(0.02, 0.025))    # off by 0.5% vs Friday's 2.5%
        self.assertFalse(o.beat_friday(0.02, -0.01))   # wrong way: off by 3% vs 1%
        self.assertIsNone(o.beat_friday(0.01, 0.005))  # exactly as far as Friday: a tie

    def test_returns_compare_scaled_perps_fairly(self):
        # A perp quoted at 10x the reference: returns, not prices, are compared.
        fri, sun = ms(2026, 10, 9, 20), ms(2026, 10, 11, 21)
        cs = candles(fri - H, [1000.0] + [1000.0] * 49 + [1020.0] * 2)
        ev = {"weekend": "2026-10-10", "close_t": fri, "close_px": 100.0, "open_t": ms(2026, 10, 12, 13, 30), "open_px": 101.5}
        r = o.score_event(ev, cs)
        self.assertAlmostEqual(r["actual"], 0.015)
        self.assertAlmostEqual(r["guess"], 0.0, places=9)  # the 1020 candles finish after the Sunday call
        self.assertAlmostEqual(r["guess_last"], 0.02)

    def test_silent_weekend_is_flagged_and_left_out(self):
        fri = ms(2026, 10, 9, 20)
        cs = candles(fri - H, [50.0])  # one candle before the close, then nothing
        ev = {"weekend": "2026-10-10", "close_t": fri, "close_px": 50.0, "open_t": ms(2026, 10, 12, 13, 30), "open_px": 51.0}
        r = o.score_event(ev, cs)
        self.assertTrue(r["silent"])
        self.assertEqual(o.summarise([r]), {"n": 0})

    def test_not_listed_yet_gives_no_row(self):
        fri = ms(2026, 10, 9, 20)
        cs = candles(ms(2026, 10, 10, 12), [50.0] * 40)  # listed on Saturday
        ev = {"weekend": "2026-10-10", "close_t": fri, "close_px": 50.0, "open_t": ms(2026, 10, 12, 13, 30), "open_px": 51.0}
        self.assertIsNone(o.score_event(ev, cs))

    def test_summary_numbers(self):
        rows = [
            {"actual": 0.02, "guess": 0.015, "guess_last": 0.02, "silent": False},   # hit, beat
            {"actual": -0.01, "guess": 0.005, "guess_last": -0.01, "silent": False},  # miss, lost
            {"actual": 0.003, "guess": 0.0002, "guess_last": 0.0, "silent": False},   # no call, beat
        ]
        s = o.summarise(rows)
        self.assertEqual((s["n"], s["calls"], s["hits"]), (3, 2, 1))
        self.assertEqual((s["beat"], s["beat_n"]), (2, 3))
        self.assertAlmostEqual(s["mae_friday"], (0.02 + 0.01 + 0.003) / 3)
        self.assertEqual(s["big_n"], 2)

    def test_sign_test(self):
        self.assertAlmostEqual(o.sign_test_p(5, 10), 1.0)
        self.assertLess(o.sign_test_p(20, 25), 0.01)
        self.assertIsNone(o.sign_test_p(0, 0))


class PriceMatch(unittest.TestCase):
    def test_constant_scale_is_fine(self):
        rows = [{"weekend": f"2026-0{i}-01", "perp_fri": 10 * p, "ref_close": p} for i, p in enumerate((100, 120, 90, 130, 110), 1)]
        self.assertIsNone(o.price_mismatch(rows))

    def test_wrong_reference_is_caught(self):
        # Perp drifting on its own while the "reference" goes elsewhere.
        rows = [{"weekend": f"2026-0{i}-01", "perp_fri": a, "ref_close": b} for i, (a, b) in enumerate(((100, 100), (110, 95), (90, 120), (130, 100), (100, 80)), 1)]
        self.assertIsNotNone(o.price_mismatch(rows))

    def test_slow_currency_drift_is_fine(self):
        # A dollar perp against a reference in won: the ratio drifts 1% a week.
        rows = [{"weekend": f"2026-0{m}-01", "perp_fri": 100 * (1.01 ** m), "ref_close": 140000} for m in range(1, 9)]
        self.assertIsNone(o.price_mismatch(rows))

    def test_one_odd_weekend_is_tolerated(self):
        rows = [{"weekend": f"2026-{i:02d}-01", "perp_fri": 110 if i == 10 else 100, "ref_close": 100} for i in range(1, 11)]
        self.assertIsNone(o.price_mismatch(rows))


class StaysUnderTheLimit(unittest.TestCase):
    """The Oracle shares Hyperliquid's per-IP limit with the other HT10 apps."""

    def setUp(self):
        self.clock = [1000.0]
        self._time, self._sleep = o.time.time, o.time.sleep
        o.time.time = lambda: self.clock[0]
        o.time.sleep = lambda s: self.clock.__setitem__(0, self.clock[0] + s)
        o.RATE_LIMITED["count"] = 0

    def tearDown(self):
        o.time.time, o.time.sleep = self._time, self._sleep
        o.RATE_LIMITED["count"] = 0

    def test_never_more_than_budget_in_any_minute(self):
        t = o.Throttle(per_min=240)
        stamps = []
        for _ in range(30):
            t.spend(104)  # a full-history candle download
            stamps.append(self.clock[0])
        for i, s in enumerate(stamps):
            in_window = sum(104 for x in stamps[: i + 1] if s - x < 60)
            self.assertLessEqual(in_window, 240)

    def test_a_429_halves_the_budget(self):
        t = o.Throttle(per_min=240)
        o.RATE_LIMITED["count"] = 1
        t.spend(20)
        self.assertEqual(t.per_min, 120)

    def test_budget_is_a_fifth_of_the_shared_limit(self):
        self.assertLessEqual(o.HL_BUDGET_PER_MIN, 1200 // 5)


if __name__ == "__main__":
    unittest.main()
