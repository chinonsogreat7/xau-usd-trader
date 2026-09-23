"""Tests for the supported complete-snapshot MT5 import subset."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import hashlib
import unittest
from unittest.mock import patch

from xau_trader.domain import AvailabilityBasis
from xau_trader.mt5_ticks import (
    BidAskTick, KNOWN_FLAG_MASK, MAX_RAW_BYTES, MAX_TICKS, MT5_COLUMNS,
    Mt5TickError, ParsedMt5Ticks, aggregate_mt5_ticks_m15, parse_mt5_tick_bytes,
)
from xau_trader.session_calendar import ScheduledClosure, SessionCalendarArtifact


START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def raw_ticks(rows, *, delimiter="\t", flags=False):
    columns = MT5_COLUMNS + (("<FLAGS>",) if flags else ())
    return (delimiter.join(columns) + "\r\n" + "\r\n".join(delimiter.join(row) for row in rows) + "\r\n").encode("utf-8")


def row_at(timestamp, bid="2000", ask="2001", *, flags=None):
    row = [timestamp.strftime("%Y.%m.%d"), timestamp.strftime("%H:%M:%S.%f")[:-3], bid, ask, "", ""]
    if flags is not None:
        row.append(str(flags))
    return row


def calendar(end=START + timedelta(hours=1), closures=()):
    return SessionCalendarArtifact(
        calendar_id="synthetic-mt5-import-test", revision="fixture-v1",
        provider_name="Synthetic fixture provider", provider_legal_entity="Synthetic fixture entity",
        instrument="XAU_USD", product_form="synthetic_quotes",
        coverage_start=START, coverage_end=end,
        source_reference="local-fixture://mt5-import-tests", retrieved_at=end,
        closures=tuple(closures),
    )


def one_hour_rows():
    return [row_at(START + timedelta(minutes=15 * index)) for index in range(4)]


def aggregate(rows, *, end=START + timedelta(hours=1), schedule=None, **kwargs):
    return aggregate_mt5_ticks_m15(
        parse_mt5_tick_bytes(raw_ticks(rows), utc_offset_minutes=0),
        coverage_start=START, coverage_end=end, calendar=schedule or calendar(end),
        availability_basis=AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION, **kwargs,
    )


class Mt5TickParserTests(unittest.TestCase):
    def test_exact_supported_columns_and_metadata(self):
        self.assertEqual(MAX_RAW_BYTES, 64 * 1024 * 1024)
        self.assertEqual(MAX_TICKS, 500000)
        for delimiter in (",", "\t"):
            raw = raw_ticks(one_hour_rows(), delimiter=delimiter)
            parsed = parse_mt5_tick_bytes(raw, utc_offset_minutes=0)
            self.assertEqual(parsed.delimiter, delimiter)
            self.assertEqual(parsed.encoding, "utf-8")
            self.assertEqual(parsed.columns, MT5_COLUMNS)
            self.assertEqual(parsed.raw_sha256, hashlib.sha256(raw).hexdigest())
            self.assertEqual(parsed.ticks[0].source_row, 2)
            self.assertEqual(parsed.ticks[-1].source_row, 5)
            self.assertEqual(parsed.ticks[0].timestamp, START)
            self.assertIsNone(parsed.ticks[0].last)
            self.assertIsNone(parsed.ticks[0].volume)
            with self.assertRaises(FrozenInstanceError):
                parsed.ticks = ()

    def test_supported_bom_encodings(self):
        text = raw_ticks(one_hour_rows()).decode("utf-8")
        for encoding, raw in (
            ("utf-8-sig", text.encode("utf-8-sig")),
            ("utf-16-le", b"\xff\xfe" + text.encode("utf-16-le")),
            ("utf-16-be", b"\xfe\xff" + text.encode("utf-16-be")),
        ):
            with self.subTest(encoding=encoding):
                parsed = parse_mt5_tick_bytes(raw, utc_offset_minutes=0)
                self.assertEqual(parsed.encoding, encoding)
                self.assertEqual(len(parsed.ticks), 4)
                self.assertEqual(parsed.raw_sha256, hashlib.sha256(raw).hexdigest())

    def test_offset_converts_rollover_and_is_not_inferred(self):
        raw = raw_ticks([row_at(START)])
        self.assertEqual(parse_mt5_tick_bytes(raw, utc_offset_minutes=120).ticks[0].timestamp, START - timedelta(hours=2))
        self.assertEqual(parse_mt5_tick_bytes(raw, utc_offset_minutes=-840).ticks[0].timestamp, START + timedelta(hours=14))
        for offset in (True, False, 1.0, "120", 841, -841, None):
            with self.subTest(offset=offset), self.assertRaises(Mt5TickError):
                parse_mt5_tick_bytes(raw, utc_offset_minutes=offset)

    def test_supported_fractional_precision(self):
        for time, micros in (("00:00:00", 0), ("00:00:00.1", 100000), ("00:00:00.12", 120000), ("00:00:00.123", 123000)):
            row = row_at(START)
            row[1] = time
            parsed = parse_mt5_tick_bytes(raw_ticks([row]), utc_offset_minutes=0)
            self.assertEqual(parsed.ticks[0].timestamp.microsecond, micros)

    def test_equal_timestamp_snapshots_preserve_source_order(self):
        raw = raw_ticks([row_at(START, "2000", "2001"), row_at(START, "2002", "2003"), row_at(START, "2002", "2003")])
        parsed = parse_mt5_tick_bytes(raw, utc_offset_minutes=0)
        self.assertEqual([tick.bid for tick in parsed.ticks], [2000, 2002, 2002])
        self.assertEqual([tick.source_row for tick in parsed.ticks], [2, 3, 4])

    def test_flags_validate_known_bitset_and_optional_unused_fields(self):
        for flags in (0, 2, 4, 8, 16, 32, 64, KNOWN_FLAG_MASK):
            row = row_at(START, flags=flags)
            row[4:6] = ["0", "12.5"]
            tick = parse_mt5_tick_bytes(raw_ticks([row], flags=True), utc_offset_minutes=0).ticks[0]
            self.assertEqual(tick.flags, flags)
            self.assertEqual(tick.last, 0)
            self.assertEqual(tick.volume, 12.5)
        for flags in (1, 3, 127, 128, -2, "2.0", "", "NaN", "100000", "2e0"):
            with self.subTest(flags=flags), self.assertRaises(Mt5TickError):
                parse_mt5_tick_bytes(raw_ticks([row_at(START, flags=flags)], flags=True), utc_offset_minutes=0)

    def test_partial_and_invalid_quotes_are_not_carried(self):
        for index in (2, 3):
            for invalid in ("", "0", "-1", "nan", "NaN", "inf", "Infinity", "1e999", " 2000", "1_000", "1,000"):
                row = row_at(START)
                row[index] = invalid
                with self.subTest(index=index, invalid=invalid), self.assertRaises(Mt5TickError):
                    parse_mt5_tick_bytes(raw_ticks([row_at(START), row]), utc_offset_minutes=0)
        with self.assertRaises(Mt5TickError):
            parse_mt5_tick_bytes(raw_ticks([row_at(START, "2002", "2001")]), utc_offset_minutes=0)

    def test_invalid_unused_fields_are_still_rejected(self):
        for index in (4, 5):
            for invalid in ("-1", "NaN", "inf", "1e999", " ", "abc"):
                row = row_at(START)
                row[index] = invalid
                with self.subTest(index=index, invalid=invalid), self.assertRaises(Mt5TickError):
                    parse_mt5_tick_bytes(raw_ticks([row]), utc_offset_minutes=0)

    def test_invalid_times_dates_and_backwards_time(self):
        for index, invalid in ((0, "2024-01-01"), (0, "2024.1.01"), (0, "2024.02.30"), (0, "0000.01.01"), (1, "24:00:00"), (1, "00:00:60"), (1, "0:00:00"), (1, "00:00:00.1234"), (1, "00:00:00Z")):
            row = row_at(START)
            row[index] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(Mt5TickError):
                parse_mt5_tick_bytes(raw_ticks([row]), utc_offset_minutes=0)
        with self.assertRaises(Mt5TickError):
            parse_mt5_tick_bytes(raw_ticks([row_at(START + timedelta(milliseconds=1)), row_at(START)]), utc_offset_minutes=0)
        row = row_at(START)
        row[0] = "0001.01.01"
        with self.assertRaises(Mt5TickError):
            parse_mt5_tick_bytes(raw_ticks([row]), utc_offset_minutes=1)

    def test_strict_headers_rows_and_error_redaction(self):
        raw = raw_ticks(one_hour_rows())
        invalids = (
            b"", b"\xef\xbb\xbf", ",".join(MT5_COLUMNS).encode(),
            raw.replace(b"<ASK>", b"<BID>"), raw.replace(b"<DATE>", b"DATE"),
            raw.replace(b"<LAST>\t<VOLUME>", b"<VOLUME>\t<LAST>"),
            raw.replace(b"<DATE>\t", b"<DATE>,"),
            raw.replace(b"\t<ASK>", b";<ASK>"),
            raw + raw, raw + b"\r\n", raw + b"sensitive-account-token\r\n",
            raw_ticks([row_at(START)[:-1]]), raw_ticks([row_at(START) + ["extra"]]),
            raw + b'"unterminated',
        )
        for invalid in invalids:
            with self.subTest(raw_length=len(invalid)):
                with self.assertRaises(Mt5TickError) as caught:
                    parse_mt5_tick_bytes(invalid, utc_offset_minutes=0)
                self.assertNotIn("sensitive-account-token", str(caught.exception))

    def test_encoding_errors_and_unsupported_encodings(self):
        text = raw_ticks(one_hour_rows()).decode("utf-8")
        for raw in (text.encode("utf-16-le"), text.encode("utf-32"), b"\xff", b"\xff\xfe\x01", raw_ticks(one_hour_rows()) + b"\x00", raw_ticks(one_hour_rows()) + b"\x1b"):
            with self.subTest(length=len(raw)), self.assertRaises(Mt5TickError):
                parse_mt5_tick_bytes(raw, utc_offset_minutes=0)

    def test_bounded_input_and_tick_counts(self):
        raw = raw_ticks(one_hour_rows())
        with patch("xau_trader.mt5_ticks.MAX_RAW_BYTES", len(raw) - 1), self.assertRaises(Mt5TickError):
            parse_mt5_tick_bytes(raw, utc_offset_minutes=0)
        with patch("xau_trader.mt5_ticks.MAX_RAW_BYTES", len(raw)):
            self.assertEqual(len(parse_mt5_tick_bytes(raw, utc_offset_minutes=0).ticks), 4)
        with patch("xau_trader.mt5_ticks.MAX_TICKS", 3), self.assertRaises(Mt5TickError):
            parse_mt5_tick_bytes(raw, utc_offset_minutes=0)
        with patch("xau_trader.mt5_ticks.MAX_TICKS", 4):
            self.assertEqual(len(parse_mt5_tick_bytes(raw, utc_offset_minutes=0).ticks), 4)
        for invalid in (None, raw.decode(), bytearray(raw)):
            with self.assertRaises(Mt5TickError):
                parse_mt5_tick_bytes(invalid, utc_offset_minutes=0)

    def test_direct_dataclass_inputs_are_validated(self):
        tick = BidAskTick(START, 2000, 2001, 2)
        with self.assertRaises(FrozenInstanceError):
            tick.bid = 0
        for kwargs in ({"bid": True}, {"ask": float("nan")}, {"source_row": True}, {"source_row": 1}, {"timestamp": START.replace(tzinfo=None)}, {"timestamp": START.astimezone(timezone(timedelta(hours=1)))}, {"last": -1}, {"flags": 1}, {"volume": "2"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(Mt5TickError):
                replace(tick, **kwargs)
        parsed = parse_mt5_tick_bytes(raw_ticks(one_hour_rows()), utc_offset_minutes=0)
        for kwargs in ({"ticks": []}, {"ticks": ()}, {"ticks": ("bad",)}, {"ticks": (parsed.ticks[1], parsed.ticks[0])}, {"encoding": "latin1"}, {"delimiter": ";"}, {"columns": MT5_COLUMNS + ("<FLAGS>",)}, {"raw_sha256": "A" * 64}):
            with self.subTest(kwargs=kwargs), self.assertRaises(Mt5TickError):
                replace(parsed, **kwargs)


class Mt5TickAggregationTests(unittest.TestCase):
    def test_observed_bid_ask_ohlc_preserves_equal_timestamp_order(self):
        rows = [
            row_at(START, "2000", "2002"), row_at(START, "2003", "2004"),
            row_at(START + timedelta(seconds=2), "1999", "2001"),
            row_at(START + timedelta(seconds=4), "2001", "2003"),
        ] + one_hour_rows()[1:]
        output = aggregate(rows)
        self.assertEqual(len(output.bars), 4)
        first = output.bars[0]
        self.assertEqual((first.bid_open, first.bid_high, first.bid_low, first.bid_close), (2000, 2003, 1999, 2001))
        self.assertEqual((first.ask_open, first.ask_high, first.ask_low, first.ask_close), (2002, 2004, 2001, 2003))
        self.assertEqual(first.start_time, START)
        self.assertEqual(first.timestamp, START + timedelta(minutes=15))
        self.assertEqual(first.available_at, first.timestamp)
        self.assertEqual(first.availability_basis, AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION)
        self.assertIsNone(first.volume)
        self.assertEqual(output.tick_count, 7)
        self.assertEqual(output.bucket_diagnostics[0].tick_count, 4)
        self.assertEqual(output.bucket_diagnostics[0].maximum_intertick_seconds, 2)
        self.assertEqual(output.maximum_intertick_seconds, 900)
        with self.assertRaises(FrozenInstanceError):
            output.tick_count = 8

    def test_sparse_quotes_report_boundary_silence_without_filling(self):
        rows = [row_at(START + timedelta(minutes=15 * index, seconds=60)) for index in range(4)]
        output = aggregate(rows)
        self.assertEqual(output.tick_count, 4)
        self.assertEqual(output.maximum_boundary_silence_seconds, 840)
        self.assertTrue(all(item.start_boundary_silence_seconds == 60 for item in output.bucket_diagnostics))
        self.assertTrue(all(item.end_boundary_silence_seconds == 840 for item in output.bucket_diagnostics))
        self.assertTrue(all(item.maximum_intertick_seconds == 0 for item in output.bucket_diagnostics))
        self.assertTrue(any("does not prove feed completeness" in warning for warning in output.warnings))

    def test_m15_boundaries_half_open_and_no_input_trimming(self):
        rows = [row_at(START + timedelta(minutes=14, seconds=59, milliseconds=999), "2000", "2001")] + one_hour_rows()[1:]
        output = aggregate(rows)
        self.assertEqual(output.bucket_diagnostics[0].last_tick_at, START + timedelta(minutes=14, seconds=59, milliseconds=999))
        self.assertEqual(output.bucket_diagnostics[1].first_tick_at, START + timedelta(minutes=15))
        for outside in (row_at(START - timedelta(milliseconds=1)), row_at(START + timedelta(hours=1))):
            invalid = sorted(rows + [outside], key=lambda row: row[:2])
            with self.assertRaisesRegex(Mt5TickError, "half-open declared coverage"):
                aggregate(invalid)

    def test_documented_closure_skipped_without_bar_or_quote_carry(self):
        end = START + timedelta(hours=3)
        closure = ScheduledClosure(START + timedelta(hours=1), START + timedelta(hours=2), "Synthetic closure fixture")
        rows = one_hour_rows() + [row_at(START + timedelta(hours=2, minutes=15 * index), "2100", "2101") for index in range(4)]
        output = aggregate(rows, end=end, schedule=calendar(end, (closure,)))
        self.assertEqual(len(output.bars), 8)
        self.assertEqual(output.bars[3].timestamp, closure.start)
        self.assertEqual(output.bars[4].start_time, closure.end)
        self.assertEqual(output.bars[4].bid_open, 2100)
        self.assertEqual(output.maximum_intertick_seconds, 4500)
        self.assertFalse(any(closure.start <= bar.start_time < closure.end for bar in output.bars))
        with self.assertRaises(ValueError):
            aggregate(rows, end=end)

    def test_missing_buckets_rejected_even_with_plenty_of_duplicate_ticks(self):
        for rows in (one_hour_rows()[1:], one_hour_rows()[:-1], [one_hour_rows()[0]] * 5 + one_hour_rows()[2:]):
            with self.assertRaises(ValueError):
                aggregate(rows)

    def test_closed_slot_and_partial_hour_closure_rejected(self):
        end = START + timedelta(hours=3)
        closure = ScheduledClosure(START + timedelta(hours=1), START + timedelta(hours=2), "Synthetic closure fixture")
        rows = one_hour_rows() + [row_at(closure.start)] + [row_at(START + timedelta(hours=2, minutes=15 * index)) for index in range(4)]
        with self.assertRaisesRegex(ValueError, "overlaps a scheduled closure"):
            aggregate(rows, end=end, schedule=calendar(end, (closure,)))
        partial = ScheduledClosure(START + timedelta(minutes=30), START + timedelta(minutes=45), "Synthetic partial-hour closure")
        with self.assertRaisesRegex(ValueError, "whole UTC hour"):
            aggregate(one_hour_rows(), schedule=calendar(closures=(partial,)))

    def test_edge_closures_rejected_not_silently_trimmed(self):
        end = START + timedelta(hours=2)
        for closure in (
            ScheduledClosure(START, START + timedelta(hours=1), "Synthetic leading closure"),
            ScheduledClosure(START + timedelta(hours=1), end, "Synthetic trailing closure"),
        ):
            rows = [row_at(START + timedelta(minutes=15 * index)) for index in range(8) if not closure.start <= START + timedelta(minutes=15 * index) < closure.end]
            with self.assertRaisesRegex(ValueError, "overlaps a scheduled closure"):
                aggregate(rows, end=end, schedule=calendar(end, (closure,)))

    def test_coverage_and_availability_require_explicit_supported_values(self):
        parsed = parse_mt5_tick_bytes(raw_ticks(one_hour_rows()), utc_offset_minutes=0)
        arguments = dict(parsed=parsed, coverage_start=START, coverage_end=START + timedelta(hours=1), calendar=calendar(), availability_basis=AvailabilityBasis.SYNTHETIC)
        self.assertEqual(aggregate_mt5_ticks_m15(**arguments).bars[0].availability_basis, AvailabilityBasis.SYNTHETIC)
        for changed in (
            {"coverage_start": START.replace(tzinfo=None)}, {"coverage_end": START},
            {"coverage_start": START + timedelta(minutes=15)}, {"coverage_end": START + timedelta(hours=2)},
            {"coverage_start": START.astimezone(timezone(timedelta(hours=1)))},
            {"availability_basis": AvailabilityBasis.OBSERVED_RECEIPT}, {"availability_basis": "invalid"},
            {"calendar": None}, {"parsed": None},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                aggregate_mt5_ticks_m15(**dict(arguments, **changed))


if __name__ == "__main__":
    unittest.main()
