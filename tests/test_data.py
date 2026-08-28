import tempfile
import unittest
from pathlib import Path

from xau_trader.data import (
    DataValidationError,
    analyze_cadence,
    generate_demo_bars,
    generate_demo_m15_bars,
    read_quote_bars_csv,
    sha256_file,
    write_quote_bars_csv,
)


class DataTests(unittest.TestCase):
    def test_demo_csv_round_trip_and_hash(self):
        bars = generate_demo_bars(5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            write_quote_bars_csv(path, bars)
            loaded = read_quote_bars_csv(path)
            self.assertEqual(len(loaded), len(bars))
            self.assertEqual(loaded[0], bars[0])
            self.assertEqual(len(sha256_file(path)), 64)

    def test_naive_csv_timestamp_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            path.write_text(
                "start_time,timestamp,available_at,availability_basis,"
                "bid_open,bid_high,bid_low,bid_close,"
                "ask_open,ask_high,ask_low,ask_close\n"
                "2025-12-31T23:00:00,2026-01-01T00:00:00,"
                "2026-01-01T00:00:00,synthetic,"
                "99,99,99,99,101,101,101,101\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DataValidationError, "explicit UTC offset"):
                read_quote_bars_csv(path)

    def test_cadence_report_exposes_gaps(self):
        bars = list(generate_demo_bars(3))
        bars.pop(1)
        report = analyze_cadence(bars, expected_minutes=60.0)
        self.assertEqual(report["gap_count"], 1)
        self.assertEqual(report["duration_mismatch_count"], 0)
        self.assertFalse(report["exact_cadence"])

    def test_m15_demo_is_complete_contiguous_and_round_trips(self):
        bars = generate_demo_m15_bars(8)

        self.assertEqual(len(bars), 8)
        self.assertEqual(
            analyze_cadence(bars, expected_minutes=15.0)["exact_cadence"],
            True,
        )
        self.assertEqual(bars[0].start_time.minute, 0)
        self.assertEqual(bars[-1].timestamp.minute, 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m15.csv"
            write_quote_bars_csv(path, bars)
            self.assertEqual(tuple(read_quote_bars_csv(path)), tuple(bars))

    def test_m15_demo_rejects_partial_hours_and_bad_count_types(self):
        with self.assertRaisesRegex(ValueError, "complete four-bar"):
            generate_demo_m15_bars(5)
        with self.assertRaisesRegex(ValueError, "at least 4"):
            generate_demo_m15_bars(0)
        with self.assertRaisesRegex(TypeError, "integer"):
            generate_demo_m15_bars(True)

    def test_overlapping_bar_intervals_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overlap.csv"
            path.write_text(
                "start_time,timestamp,available_at,availability_basis,"
                "bid_open,bid_high,bid_low,bid_close,"
                "ask_open,ask_high,ask_low,ask_close\n"
                "2026-01-01T00:00:00Z,2026-01-01T01:00:00Z,"
                "2026-01-01T01:00:00Z,historical_close_assumption,"
                "99,99,99,99,101,101,101,101\n"
                "2026-01-01T00:30:00Z,2026-01-01T02:00:00Z,"
                "2026-01-01T02:00:00Z,historical_close_assumption,"
                "99,99,99,99,101,101,101,101\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DataValidationError, "overlaps"):
                read_quote_bars_csv(path)


if __name__ == "__main__":
    unittest.main()
