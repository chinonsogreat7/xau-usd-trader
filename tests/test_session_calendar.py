import hashlib
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import mock_open, patch

from xau_trader.session_calendar import (
    MAX_CALENDAR_BYTES,
    ScheduledClosure,
    SessionCalendarArtifact,
    SessionCalendarError,
    load_session_calendar,
    parse_session_calendar_bytes,
    session_calendar_from_dict,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _time(hours=0, minutes=0, seconds=0, microseconds=0):
    return BASE + timedelta(
        hours=hours, minutes=minutes, seconds=seconds, microseconds=microseconds
    )


def _payload():
    return {
        "schema": "xau_trader.session_calendar",
        "version": 1,
        "id": "synthetic-session-fixture",
        "revision": "fixture-v1",
        "provider_name": "Local deterministic generator",
        "provider_legal_entity": "Research project owner",
        "instrument": "XAU_USD",
        "product_form": "synthetic bid/ask spot-price series",
        "coverage_start": "2026-01-01T00:00:00Z",
        "coverage_end": "2026-01-02T00:00:00Z",
        "source_reference": "local-generator://session-fixture-v1",
        "retrieved_at": "2025-12-31T00:00:00Z",
        "closures": [
            {
                "start": "2026-01-01T05:00:00Z",
                "end": "2026-01-01T07:00:00Z",
                "reason": "Synthetic maintenance interval",
            },
            {
                "start": "2026-01-01T12:00:00Z",
                "end": "2026-01-01T13:00:00Z",
                "reason": "Synthetic session break",
            },
        ],
    }


def _calendar():
    return session_calendar_from_dict(_payload())


class SessionCalendarParsingTests(unittest.TestCase):
    def test_round_trip_and_canonical_fingerprint(self):
        payload = _payload()
        calendar = session_calendar_from_dict(payload)
        self.assertEqual(calendar.as_dict(), payload)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(calendar.canonical_json, canonical)
        self.assertEqual(calendar.fingerprint, hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        self.assertEqual(len(calendar.fingerprint), 64)
        reordered = {key: payload[key] for key in reversed(list(payload))}
        self.assertEqual(calendar.fingerprint, session_calendar_from_dict(reordered).fingerprint)
        self.assertEqual(hash(calendar), hash(session_calendar_from_dict(reordered)))

    def test_provenance_and_closure_changes_change_fingerprint(self):
        original = _calendar()
        for field, value in (
            ("id", "synthetic-session-fixture-two"),
            ("revision", "fixture-v2"),
            ("provider_name", "Second local generator"),
            ("provider_legal_entity", "Second research owner"),
            ("product_form", "synthetic point-price series"),
            ("source_reference", "local-generator://session-fixture-v2"),
            ("retrieved_at", "2025-12-30T00:00:00Z"),
            ("coverage_end", "2026-01-03T00:00:00Z"),
        ):
            with self.subTest(field=field):
                payload = _payload()
                payload[field] = value
                self.assertNotEqual(original.fingerprint, session_calendar_from_dict(payload).fingerprint)
        changed = _payload()
        changed["closures"][0]["reason"] = "Revised synthetic maintenance interval"
        self.assertNotEqual(original.fingerprint, session_calendar_from_dict(changed).fingerprint)

    def test_artifact_and_closures_are_immutable_and_detached_from_input(self):
        original = _calendar()
        source_closures = list(original.closures)
        calendar = replace(original, closures=source_closures)
        source_closures.clear()
        self.assertIsInstance(calendar.closures, tuple)
        self.assertEqual(len(calendar.closures), 2)
        with self.assertRaises(FrozenInstanceError):
            calendar.revision = "fixture-v2"
        with self.assertRaises(FrozenInstanceError):
            calendar.closures[0].reason = "Changed reason"
        serialized = calendar.as_dict()
        serialized["closures"][0]["reason"] = "Changed reason"
        self.assertEqual(calendar, original)

    def test_zero_offsets_normalize_to_utc_and_z(self):
        payload = _payload()
        payload["coverage_start"] = "2026-01-01T00:00:00+00:00"
        payload["coverage_end"] = "2026-01-02T00:00:00-00:00"
        calendar = session_calendar_from_dict(payload)
        self.assertIs(calendar.coverage_start.tzinfo, timezone.utc)
        self.assertEqual(calendar.as_dict()["coverage_start"], _payload()["coverage_start"])
        self.assertEqual(calendar.as_dict()["coverage_end"], _payload()["coverage_end"])
        named_zero = timezone(timedelta(0), "Zero offset")
        closure = ScheduledClosure(_time(5).replace(tzinfo=named_zero), _time(6), "Fixture break")
        self.assertIs(closure.start.tzinfo, timezone.utc)

    def test_future_schedule_retrieval_before_coverage_is_valid(self):
        calendar = _calendar()
        self.assertLess(calendar.retrieved_at, calendar.coverage_start)
        self.assertEqual(replace(calendar, retrieved_at=_time(4)).retrieved_at, _time(4))
        self.assertEqual(replace(calendar, retrieved_at=_time(30)).retrieved_at, _time(30))

    def test_missing_and_unknown_fields_are_rejected_at_each_level(self):
        for field in _payload():
            with self.subTest(missing=field):
                payload = _payload()
                del payload[field]
                with self.assertRaisesRegex(SessionCalendarError, "missing fields"):
                    session_calendar_from_dict(payload)
        for nested in (False, True):
            with self.subTest(nested=nested):
                payload = _payload()
                target = payload["closures"][0] if nested else payload
                target["guessed"] = True
                with self.assertRaisesRegex(SessionCalendarError, "unknown fields"):
                    session_calendar_from_dict(payload)
        for field in ("start", "end", "reason"):
            payload = _payload()
            del payload["closures"][0][field]
            with self.assertRaisesRegex(SessionCalendarError, "missing fields"):
                session_calendar_from_dict(payload)

    def test_closed_schema_instrument_and_strict_container_types(self):
        for field, value in (
            ("schema", "other.schema"), ("version", 2), ("version", True),
            ("version", 1.0), ("version", "1"), ("instrument", "XAUUSD"),
            ("instrument", "EUR_USD"), ("closures", {}), ("closures", ()),
            ("closures", None), ("closures", ["invalid"]),
        ):
            with self.subTest(field=field, value=value):
                payload = _payload()
                payload[field] = value
                with self.assertRaises(SessionCalendarError):
                    session_calendar_from_dict(payload)
        for root in ([], None, "calendar", {1: "value"}):
            with self.subTest(root=root), self.assertRaises(SessionCalendarError):
                session_calendar_from_dict(root)
        for closures in (None, "closure", {}, ["closure"]):
            with self.subTest(closures=closures), self.assertRaises(SessionCalendarError):
                replace(_calendar(), closures=closures)

    def test_text_fields_reject_missing_placeholder_and_corrupted_strings(self):
        for field in (
            "id", "revision", "provider_name", "provider_legal_entity",
            "product_form", "source_reference",
        ):
            for value in (None, 42, "", " ", "TBD", "unknown", "N/A", "<fill-me>",
                          "your_value", "replace-me-now", "broken\x00text", "bad\ud800text"):
                with self.subTest(field=field, value=repr(value)):
                    payload = _payload()
                    payload[field] = value
                    with self.assertRaises(SessionCalendarError):
                        session_calendar_from_dict(payload)
        for reason in ("", "placeholder", "bad\nreason", "bad\rreason"):
            payload = _payload()
            payload["closures"][0]["reason"] = reason
            with self.subTest(reason=reason), self.assertRaises(SessionCalendarError):
                session_calendar_from_dict(payload)

    def test_source_reference_rejects_credentials_queries_fragments_and_headers(self):
        for reference in (
            "https://user:password@calendar.invalid/closures",
            "//user@calendar.invalid/closures",
            "https://calendar.invalid/closures?token=secret",
            "https://calendar.invalid/closures#secret",
            "Authorization: hidden", "authorization = hidden",
            "Bearer hidden", "Basic hidden", "https://[malformed",
        ):
            with self.subTest(reference=reference):
                payload = _payload()
                payload["source_reference"] = reference
                with self.assertRaises(SessionCalendarError):
                    session_calendar_from_dict(payload)

    def test_timestamp_strings_are_strict_and_preserve_microseconds(self):
        for value in (
            None, 0, "", "2026-01-01", "2026-01-01T00:00:00",
            "2026-01-01T01:00:00+01:00", "2026-13-01T00:00:00Z",
            "2026-01-01X00:00:00Z", "2026-01-01T00:00:00.1234567Z",
            "2026-01-01T00:00:00Z\n", " 2026-01-01T00:00:00Z",
        ):
            with self.subTest(value=value):
                payload = _payload()
                payload["coverage_start"] = value
                with self.assertRaises(SessionCalendarError):
                    session_calendar_from_dict(payload)
        payload = _payload()
        payload["retrieved_at"] = "2025-12-31T00:00:00.123456Z"
        self.assertEqual(session_calendar_from_dict(payload).as_dict(), payload)
        for field in ("coverage_start", "coverage_end", "retrieved_at"):
            for value in (BASE.replace(tzinfo=None), BASE.replace(tzinfo=timezone(timedelta(hours=1))), "UTC"):
                with self.subTest(field=field, value=value), self.assertRaises(SessionCalendarError):
                    replace(_calendar(), **{field: value})

    def test_strict_json_loader_and_read_errors(self):
        malformed = (
            '{"schema":"a","schema":"b"}',
            '{"closures":[{"start":"a","start":"b"}]}',
            '{"value":NaN}', '{"value":Infinity}', '{"value":-Infinity}',
            "{", "[]", json.dumps(_payload()).replace('"version": 1', '"version": 1e999'),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            for content in malformed:
                path.write_text(content, encoding="utf-8")
                with self.subTest(content=content), self.assertRaises(SessionCalendarError):
                    load_session_calendar(path)
            path.write_bytes(b"\xff")
            with self.assertRaisesRegex(SessionCalendarError, "UTF-8 JSON"):
                load_session_calendar(path)
            path.write_text(json.dumps(_payload()), encoding="utf-8")
            self.assertEqual(load_session_calendar(path), _calendar())
            with self.assertRaisesRegex(SessionCalendarError, "could not read"):
                load_session_calendar(Path(directory) / "missing.json")

    def test_byte_parser_matches_canonical_content_without_reopening_files(self):
        raw = json.dumps(_payload(), indent=2).encode("utf-8") + b"\n"
        self.assertEqual(parse_session_calendar_bytes(raw), _calendar())
        self.assertNotEqual(hashlib.sha256(raw).hexdigest(), _calendar().fingerprint)
        self.assertEqual(parse_session_calendar_bytes(_calendar().canonical_json.encode("utf-8")), _calendar())
        for value in (None, "{}", bytearray(raw), memoryview(raw)):
            with self.subTest(value_type=type(value).__name__), self.assertRaisesRegex(SessionCalendarError, "must be bytes"):
                parse_session_calendar_bytes(value)

    def test_byte_parser_retains_strict_json_and_utf8_validation(self):
        raw = json.dumps(_payload()).encode("utf-8")
        malformed = (
            b"", b"\xff", b"\xef\xbb\xbf" + raw,
            json.dumps(_payload()).encode("utf-16"), b"{} {}",
            b'{"schema":"a","schema":"b"}',
            b'{"closures":[{"start":"a","start":"b"}]}',
            b'{"value":NaN}', b'{"value":Infinity}', b'{"value":-Infinity}',
            b"[" * 1200 + b"]" * 1200,
        )
        for value in malformed:
            with self.subTest(length=len(value)), self.assertRaises(SessionCalendarError):
                parse_session_calendar_bytes(value)

    def test_calendar_size_limit_and_bounded_loader_read(self):
        self.assertEqual(MAX_CALENDAR_BYTES, 1024 * 1024)
        raw = _calendar().canonical_json.encode("utf-8")
        at_limit = raw + b" " * (MAX_CALENDAR_BYTES - len(raw))
        self.assertEqual(parse_session_calendar_bytes(at_limit), _calendar())
        with self.assertRaisesRegex(SessionCalendarError, "MAX_CALENDAR_BYTES"):
            parse_session_calendar_bytes(at_limit + b" ")
        opened = mock_open(read_data=raw)
        with patch.object(Path, "open", opened):
            self.assertEqual(load_session_calendar("fixture.json"), _calendar())
        opened.assert_called_once_with("rb")
        opened().read.assert_called_once_with(MAX_CALENDAR_BYTES + 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oversized-calendar.json"
            path.write_bytes(at_limit + b" ")
            with self.assertRaisesRegex(SessionCalendarError, "MAX_CALENDAR_BYTES"):
                load_session_calendar(path)


class SessionCalendarIntervalTests(unittest.TestCase):
    def test_coverage_and_closures_must_have_positive_duration(self):
        for endpoint in (BASE, BASE - timedelta(seconds=1)):
            with self.subTest(endpoint=endpoint), self.assertRaises(SessionCalendarError):
                replace(_calendar(), coverage_end=endpoint)
            with self.subTest(endpoint=endpoint), self.assertRaises(SessionCalendarError):
                ScheduledClosure(BASE, endpoint, "Fixture interval")

    def test_closures_reject_overlap_adjacency_disorder_and_outside_coverage(self):
        pairs = ((4, 6), (6, 8), (7, 8), (1, 2), (-1, 1), (23, 25))
        original = _calendar()
        for start, end in pairs:
            with self.subTest(start=start, end=end):
                extra = ScheduledClosure(_time(start), _time(end), "Second fixture interval")
                with self.assertRaises(SessionCalendarError):
                    replace(original, closures=(original.closures[0], extra))
        with self.assertRaises(SessionCalendarError):
            replace(original, closures=tuple(reversed(original.closures)))
        spanning = ScheduledClosure(BASE, _time(24), "Full fixture closure")
        self.assertEqual(replace(original, closures=(spanning,)).closures, (spanning,))

    def test_boundary_bars_are_allowed_but_all_closure_overlaps_are_rejected(self):
        calendar = _calendar()
        for start, end in ((0, 1), (4, 5), (7, 8), (11, 12), (13, 24)):
            with self.subTest(start=start, end=end):
                self.assertIsNone(calendar.validate_bar(_time(start), _time(end)))
        for start, end in ((4, 6), (5, 6), (6, 7), (5, 7), (6, 8), (4, 8), (11, 14)):
            with self.subTest(start=start, end=end), self.assertRaisesRegex(SessionCalendarError, "overlaps"):
                calendar.validate_bar(_time(start), _time(end))

    def test_bars_reject_zero_negative_and_outside_coverage_intervals(self):
        calendar = _calendar()
        for start, end in ((0, 0), (2, 1), (-1, 0), (24, 25)):
            with self.subTest(start=start, end=end), self.assertRaises(SessionCalendarError):
                calendar.validate_bar(_time(start), _time(end))
        with self.assertRaisesRegex(SessionCalendarError, "UTC"):
            calendar.validate_bar(BASE.replace(tzinfo=None), _time(1))

    def test_transitions_allow_equality_and_exactly_one_declared_closure(self):
        calendar = _calendar()
        for hour in (0, 4, 5, 7, 24):
            self.assertIsNone(calendar.validate_transition(_time(hour), _time(hour)))
        for closure in calendar.closures:
            self.assertIsNone(calendar.validate_transition(closure.start, closure.end))
        for start, end in ((4, 5), (5, 6), (6, 7), (4, 7), (5, 8), (5, 13), (8, 9)):
            with self.subTest(start=start, end=end), self.assertRaisesRegex(SessionCalendarError, "exactly match"):
                calendar.validate_transition(_time(start), _time(end))

    def test_transitions_reject_backwards_and_outside_coverage(self):
        for start, end in ((4, 3), (-1, 0), (24, 25), (-1, -1), (25, 25)):
            with self.subTest(start=start, end=end), self.assertRaises(SessionCalendarError):
                _calendar().validate_transition(_time(start), _time(end))

    def test_empty_calendar_does_not_infer_a_gap(self):
        calendar = replace(_calendar(), closures=())
        calendar.validate_bar(BASE, _time(24))
        calendar.require_hour_aligned_closures()
        with self.assertRaisesRegex(SessionCalendarError, "exactly match"):
            calendar.validate_transition(_time(5), _time(7))

    def test_partial_hours_are_representable_but_rejected_by_kernel_requirement(self):
        _calendar().require_hour_aligned_closures()
        for offset in (timedelta(minutes=15), timedelta(seconds=1), timedelta(microseconds=1)):
            for change_start in (False, True):
                with self.subTest(offset=offset, change_start=change_start):
                    closure = ScheduledClosure(
                        _time(5) + offset if change_start else _time(5),
                        _time(7) + offset if not change_start else _time(7),
                        "Partial-hour fixture interval",
                    )
                    calendar = replace(_calendar(), closures=(closure,))
                    self.assertEqual(session_calendar_from_dict(calendar.as_dict()), calendar)
                    calendar.validate_transition(closure.start, closure.end)
                    with self.assertRaisesRegex(SessionCalendarError, "whole UTC hour"):
                        calendar.require_hour_aligned_closures()
        calendar = replace(_calendar(), coverage_start=_time(minutes=15), coverage_end=_time(23, 45))
        calendar.require_hour_aligned_closures()


if __name__ == "__main__":
    unittest.main()
