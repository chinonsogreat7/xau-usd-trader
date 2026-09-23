import copy
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from xau_trader.data import generate_demo_m15_bars
from xau_trader.dataset_manifest import dataset_manifest_from_dict, SessionCalendar
from xau_trader.domain import AvailabilityBasis
from xau_trader.mt5_import_plan import (
    IMPORTER_VERSION, MAX_PLAN_BYTES, Mt5ImportCoverage, Mt5ImportPlanError, Mt5ImportTimestamp,
    build_mt5_import_manifest, load_mt5_import_plan, mt5_import_plan_from_dict,
    parse_mt5_import_plan_bytes,
)
from xau_trader.session_calendar import ScheduledClosure, SessionCalendarArtifact


RAW_HASH = "a" * 64
CSV_HASH = "b" * 64
CALENDAR_HASH = "c" * 64


def fixture():
    source = tuple(replace(bar, volume=None) for bar in generate_demo_m15_bars(8))
    bars = source[:4] + tuple(replace(
        bar, start_time=bar.start_time + timedelta(hours=1),
        timestamp=bar.timestamp + timedelta(hours=1),
        available_at=bar.available_at + timedelta(hours=1),
    ) for bar in source[4:])
    calendar = SessionCalendarArtifact(
        calendar_id="synthetic-import-calendar", revision="fixture-v1",
        provider_name="Local tick fixture generator", provider_legal_entity="Research project owner",
        instrument="XAU_USD", product_form="synthetic bid/ask gold quote series",
        coverage_start=bars[0].start_time, coverage_end=bars[-1].timestamp,
        source_reference="local-fixture://calendar-evidence-v1",
        retrieved_at=bars[0].start_time,
        closures=(ScheduledClosure(start=bars[3].timestamp, end=bars[4].start_time,
                                   reason="Synthetic whole-hour test closure"),),
    )
    payload = {
        "schema": "xau_trader.mt5_tick_import_plan", "version": 1,
        "provider": {"name": calendar.provider_name, "legal_entity": calendar.provider_legal_entity},
        "instrument": {"symbol": "XAU_USD", "product_form": calendar.product_form},
        "source": {
            "route": "local-fixture://mt5-tick-export-v1",
            "account_environment": "synthetic local research with no trading account",
            "acquisition_basis": "synthetic_generation", "source_symbol": "XAUUSD.fixture",
            "retrieved_at": calendar.coverage_end.isoformat().replace("+00:00", "Z"),
        },
        "rights": {"basis": "self-generated fixture",
                   "api_data_agreement_version": "project-owned fixture policy v1",
                   "retention_basis": "project-owned fixtures retained with tests"},
        "timestamp": {"utc_offset_minutes": 120,
                      "evidence_reference": "local-fixture://explicit-fixed-offset-evidence"},
        "coverage": {"start": calendar.coverage_start.isoformat().replace("+00:00", "Z"),
                     "end": calendar.coverage_end.isoformat().replace("+00:00", "Z")},
        "raw_sha256": RAW_HASH,
        "session_calendar": {"id": calendar.calendar_id, "version": calendar.revision,
                             "artifact_uri": "local-fixture://calendar.json",
                             "artifact_sha256": CALENDAR_HASH,
                             "content_fingerprint": calendar.fingerprint},
    }
    return payload, bars, calendar


def build(plan, bars, calendar, **kwargs):
    values = dict(raw_sha256=RAW_HASH, normalized_csv_sha256=CSV_HASH,
                  calendar_artifact_sha256=CALENDAR_HASH)
    values.update(kwargs)
    return build_mt5_import_manifest(plan, bars, calendar, **values)


class Mt5ImportPlanParsingTests(unittest.TestCase):
    def setUp(self):
        self.payload, self.bars, self.calendar = fixture()

    def test_round_trip_canonical_fingerprint_and_immutable_fields(self):
        plan = mt5_import_plan_from_dict(self.payload)
        self.assertEqual(plan.as_dict(), self.payload)
        encoded = json.dumps(self.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(plan.canonical_json, encoded)
        self.assertEqual(plan.fingerprint, hashlib.sha256(encoded.encode("utf-8")).hexdigest())
        reordered = {key: self.payload[key] for key in reversed(self.payload)}
        self.assertEqual(mt5_import_plan_from_dict(reordered).fingerprint, plan.fingerprint)
        self.assertEqual(plan.coverage_start, self.bars[0].start_time)
        self.assertEqual(plan.coverage_end, self.bars[-1].timestamp)
        self.assertEqual(plan.utc_offset_minutes, 120)
        with self.assertRaises(FrozenInstanceError):
            plan.raw_sha256 = "d" * 64
        with self.assertRaises(FrozenInstanceError):
            plan.timestamp.utc_offset_minutes = 0
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(encoded + "\n", encoding="utf-8")
            self.assertEqual(load_mt5_import_plan(path), plan)

    def test_each_semantic_field_is_fingerprint_bound(self):
        original = mt5_import_plan_from_dict(self.payload)
        for section, field, value in (
            ("provider", "name", "Another fixture provider"),
            ("source", "source_symbol", "XAUUSD.second-fixture"),
            ("timestamp", "utc_offset_minutes", 0),
            ("timestamp", "evidence_reference", "local-fixture://second-time-evidence"),
            ("rights", "retention_basis", "Another truthful fixture retention rule"),
            ("session_calendar", "artifact_sha256", "d" * 64),
        ):
            with self.subTest(section=section, field=field):
                payload = copy.deepcopy(self.payload)
                payload[section][field] = value
                self.assertNotEqual(mt5_import_plan_from_dict(payload).fingerprint, original.fingerprint)

    def test_schema_and_version_closed_values(self):
        for field, value in (("schema", "other"), ("version", True), ("version", 2),
                             ("version", 1.0), ("version", "1")):
            with self.subTest(field=field, value=value):
                payload = copy.deepcopy(self.payload)
                payload[field] = value
                with self.assertRaises(Mt5ImportPlanError):
                    mt5_import_plan_from_dict(payload)

    def test_every_object_rejects_missing_and_extra_fields(self):
        for section in (None, "provider", "instrument", "source", "rights", "timestamp",
                        "coverage", "session_calendar"):
            original = self.payload if section is None else self.payload[section]
            for key in original:
                with self.subTest(section=section, missing=key):
                    payload = copy.deepcopy(self.payload)
                    target = payload if section is None else payload[section]
                    del target[key]
                    with self.assertRaisesRegex(Mt5ImportPlanError, "missing fields"):
                        mt5_import_plan_from_dict(payload)
            payload = copy.deepcopy(self.payload)
            target = payload if section is None else payload[section]
            target["extra"] = "disallowed"
            with self.subTest(section=section, extra=True):
                with self.assertRaisesRegex(Mt5ImportPlanError, "unknown fields"):
                    mt5_import_plan_from_dict(payload)

    def test_no_implicit_acquisition_basis_or_availability(self):
        plan = mt5_import_plan_from_dict(self.payload)
        self.assertEqual(plan.availability_basis, AvailabilityBasis.SYNTHETIC)
        self.payload["source"]["acquisition_basis"] = "user_export"
        plan = mt5_import_plan_from_dict(self.payload)
        self.assertEqual(plan.availability_basis, AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION)
        for value in ("api_response", "provider_download", "", None, True):
            with self.subTest(value=value):
                self.payload["source"]["acquisition_basis"] = value
                with self.assertRaises(Mt5ImportPlanError):
                    mt5_import_plan_from_dict(self.payload)

    def test_fixed_offsets_are_exact_integers_and_bounded(self):
        for value in (-840, 0, 840):
            self.payload["timestamp"]["utc_offset_minutes"] = value
            self.assertEqual(mt5_import_plan_from_dict(self.payload).utc_offset_minutes, value)
        for value in (-841, 841, True, 120.0, "120", None):
            with self.subTest(value=value):
                self.payload["timestamp"]["utc_offset_minutes"] = value
                with self.assertRaises(Mt5ImportPlanError):
                    mt5_import_plan_from_dict(self.payload)

    def test_coverage_utc_alignment_order_and_retrieval(self):
        for section, field, value in (
            ("coverage", "start", "2024-01-01T00:15:00Z"),
            ("coverage", "end", "2024-01-01T03:00:00.000001Z"),
            ("coverage", "start", "2024-01-01T00:00:00"),
            ("coverage", "start", "2024-01-01T01:00:00+01:00"),
            ("coverage", "end", "2024-01-01T00:00:00Z"),
            ("coverage", "start", "2024-13-01T00:00:00Z"),
            ("coverage", "start", "2024-01-01 00:00:00Z"),
            ("source", "retrieved_at", "2024-01-01T02:59:59Z"),
        ):
            with self.subTest(section=section, field=field, value=value):
                payload = copy.deepcopy(self.payload)
                payload[section][field] = value
                with self.assertRaises(Mt5ImportPlanError):
                    mt5_import_plan_from_dict(payload)
        with self.assertRaises(Mt5ImportPlanError):
            Mt5ImportCoverage(self.bars[0].start_time.replace(tzinfo=None), self.bars[-1].timestamp)
        with self.assertRaises(Mt5ImportPlanError):
            Mt5ImportCoverage(self.bars[0].start_time, self.bars[-1].timestamp.astimezone(
                timezone(timedelta(hours=1))))

    def test_hashes_are_strict_without_normalization(self):
        for section, field in ((None, "raw_sha256"), ("session_calendar", "artifact_sha256"),
                               ("session_calendar", "content_fingerprint")):
            for value in ("a" * 63, "A" * 64, "a" * 64 + " ", "g" * 64, None, 1):
                with self.subTest(section=section, field=field, value=value):
                    payload = copy.deepcopy(self.payload)
                    (payload if section is None else payload[section])[field] = value
                    with self.assertRaises(Mt5ImportPlanError):
                        mt5_import_plan_from_dict(payload)

    def test_text_rejects_controls_surrogates_placeholders_and_obvious_secrets(self):
        for section, field in (("provider", "name"), ("instrument", "product_form"),
                               ("source", "account_environment"), ("source", "source_symbol"),
                               ("rights", "retention_basis"), ("session_calendar", "id")):
            for value in ("", "TBD", "<fill-me>", "hello\n", "hello\x00", "hello\ud800",
                          "Authorization = secret", "Bearer secret", "api_key=secret",
                          "https://username:secret@fixture.test/data"):
                with self.subTest(section=section, field=field, value=repr(value)):
                    payload = copy.deepcopy(self.payload)
                    payload[section][field] = value
                    with self.assertRaises(Mt5ImportPlanError):
                        mt5_import_plan_from_dict(payload)

    def test_reference_fields_are_redacted_but_not_fetched(self):
        for section, field in (("source", "route"), ("timestamp", "evidence_reference"),
                               ("session_calendar", "artifact_uri")):
            for value in ("https://fixture.test/data?token=secret", "local://item#fragment",
                          "https://user@fixture.test/data", "https://[bad"):
                with self.subTest(section=section, field=field, value=value):
                    payload = copy.deepcopy(self.payload)
                    payload[section][field] = value
                    with self.assertRaises(Mt5ImportPlanError):
                        mt5_import_plan_from_dict(payload)

    def test_direct_constructor_cannot_bypass_required_types_or_calendar_artifact(self):
        plan = mt5_import_plan_from_dict(self.payload)
        for name in ("provider", "source", "rights", "timestamp", "coverage", "session_calendar"):
            with self.subTest(name=name):
                with self.assertRaises(Mt5ImportPlanError):
                    replace(plan, **{name: {}})
        with self.assertRaises(Mt5ImportPlanError):
            replace(plan, session_calendar=SessionCalendar("fixture-calendar", "v1"))
        with self.assertRaises(Mt5ImportPlanError):
            replace(plan, provider=replace(plan.provider, name="bad\ud800"))
        with self.assertRaises(Mt5ImportPlanError):
            Mt5ImportTimestamp(True, "local-fixture://evidence")

    def test_loader_rejects_duplicate_json_nonfinite_invalid_utf8_and_nonobjects(self):
        serialized = json.dumps(self.payload)
        bad_texts = (
            serialized.replace('"version": 1', '"version": 1, "version": 1', 1),
            serialized.replace('"utc_offset_minutes": 120', '"utc_offset_minutes": NaN'),
            serialized.replace('"utc_offset_minutes": 120', '"utc_offset_minutes": Infinity'),
            "[]", "null", "{", serialized.replace('"version": 1', '"\\ud800": 1, "version": 1', 1),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            for value in bad_texts:
                with self.subTest(value=value[:40]):
                    path.write_text(value, encoding="utf-8")
                    with self.assertRaises(Mt5ImportPlanError):
                        load_mt5_import_plan(path)
            path.write_bytes(b"\xff")
            with self.assertRaises(Mt5ImportPlanError):
                load_mt5_import_plan(path)
            with self.assertRaises(Mt5ImportPlanError):
                load_mt5_import_plan(Path(directory) / "absent.json")

    def test_exact_byte_parser_is_bounded_and_retains_strict_json_validation(self):
        raw = json.dumps(self.payload).encode("utf-8")
        plan = mt5_import_plan_from_dict(self.payload)
        self.assertEqual(parse_mt5_import_plan_bytes(raw), plan)
        self.assertEqual(MAX_PLAN_BYTES, 1024 * 1024)
        maximum = raw + b" " * (MAX_PLAN_BYTES - len(raw))
        self.assertEqual(parse_mt5_import_plan_bytes(maximum), plan)
        for value in (b"", maximum + b" ", "{}", bytearray(raw), memoryview(raw), None,
                      b"\xff", b'\xef\xbb\xbf' + raw,
                      raw.replace(b'"version": 1', b'"version": 1, "version": 1', 1),
                      raw.replace(b'"utc_offset_minutes": 120', b'"utc_offset_minutes": NaN'),
                      b"[" * 1500 + b"]" * 1500):
            with self.subTest(value_type=type(value).__name__, length=len(value) if value is not None else None):
                with self.assertRaises(Mt5ImportPlanError):
                    parse_mt5_import_plan_bytes(value)

    def test_loader_uses_same_bounded_byte_contract(self):
        raw = json.dumps(self.payload).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_bytes(raw + b" " * (MAX_PLAN_BYTES + 1 - len(raw)))
            with self.assertRaisesRegex(Mt5ImportPlanError, "at most 1 MiB"):
                load_mt5_import_plan(path)


class Mt5ImportManifestBuildingTests(unittest.TestCase):
    def setUp(self):
        self.payload, self.bars, self.calendar = fixture()
        self.plan = mt5_import_plan_from_dict(self.payload)

    def test_builder_binds_plan_calendar_source_and_exact_observed_gaps(self):
        manifest = build(self.plan, self.bars, self.calendar)
        self.assertEqual(manifest.version, 2)
        self.assertEqual(dataset_manifest_from_dict(manifest.as_dict()), manifest)
        self.assertEqual(manifest.hashes.raw_sha256, RAW_HASH)
        self.assertEqual(manifest.hashes.normalized_csv_sha256, CSV_HASH)
        self.assertEqual(manifest.session_calendar, self.plan.session_calendar)
        self.assertEqual(manifest.coverage.row_count, 8)
        gap = manifest.coverage.known_gaps[0]
        self.assertEqual((gap.start, gap.end, gap.reason), (
            self.calendar.closures[0].start, self.calendar.closures[0].end,
            self.calendar.closures[0].reason,
        ))
        params = dict(manifest.source.request_parameters)
        self.assertEqual(params["importer_version"], IMPORTER_VERSION)
        self.assertEqual(params["import_plan_fingerprint"], self.plan.fingerprint)
        self.assertEqual(params["source_symbol"], "XAUUSD.fixture")
        self.assertEqual(params["utc_offset_minutes"], "120")
        self.assertEqual(params["timezone_evidence_reference"], self.plan.timestamp.evidence_reference)
        self.assertEqual(params["quote_policy"], "complete_bid_ask_snapshots_no_fill")
        self.assertEqual(params["bar_open_semantics"], "first_observed_tick_not_guaranteed_boundary_quote")
        self.assertEqual(params["tick_completeness"], "operator_claim_not_verified")
        self.assertIn("Subtract", manifest.source.time_normalization_rule)
        self.assertIn("not a daylight-saving schedule", manifest.source.time_normalization_rule)

    def test_user_exports_use_historical_close_assumption_only(self):
        self.payload["source"]["acquisition_basis"] = "user_export"
        plan = mt5_import_plan_from_dict(self.payload)
        bars = tuple(replace(bar, availability_basis=AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION)
                     for bar in self.bars)
        manifest = build(plan, bars, self.calendar)
        self.assertEqual(manifest.bars.availability_basis, AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION)
        with self.assertRaisesRegex(Mt5ImportPlanError, "availability basis"):
            build(plan, self.bars, self.calendar)
        late = bars[:-1] + (replace(bars[-1], available_at=bars[-1].timestamp + timedelta(seconds=1)),)
        with self.assertRaisesRegex(Mt5ImportPlanError, "available_at"):
            build(plan, late, self.calendar)

    def test_raw_plan_and_actual_calendar_hashes_must_match(self):
        for kwargs in ({"raw_sha256": "d" * 64}, {"calendar_artifact_sha256": "d" * 64},
                       {"normalized_csv_sha256": "A" * 64}, {"raw_sha256": "a" * 64 + " "}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(Mt5ImportPlanError):
                    build(self.plan, self.bars, self.calendar, **kwargs)
        for field, value in (("content_fingerprint", "d" * 64), ("calendar_id", "different-calendar"),
                             ("version", "v2")):
            with self.subTest(field=field):
                plan = replace(self.plan, session_calendar=replace(self.plan.session_calendar, **{field: value}))
                with self.assertRaises(Mt5ImportPlanError):
                    build(plan, self.bars, self.calendar)

    def test_provider_legal_entity_product_and_calendar_retrieval_must_match(self):
        for field, value in (("provider_name", "Other provider"),
                             ("provider_legal_entity", "Other legal entity"),
                             ("product_form", "Other gold contract"),
                             ("retrieved_at", self.plan.source.retrieved_at + timedelta(seconds=1))):
            with self.subTest(field=field):
                calendar = replace(self.calendar, **{field: value})
                plan = replace(self.plan, session_calendar=replace(
                    self.plan.session_calendar, content_fingerprint=calendar.fingerprint))
                with self.assertRaises(Mt5ImportPlanError):
                    build(plan, self.bars, calendar)

    def test_unknown_missing_open_bars_and_undeclared_gap_are_not_repaired(self):
        for bars in (self.bars[:2] + self.bars[3:], self.bars[1:], self.bars[:-1], tuple(), (object(),)):
            with self.subTest(length=len(bars)):
                with self.assertRaises(Mt5ImportPlanError):
                    build(self.plan, bars, self.calendar)
        calendar = replace(self.calendar, closures=tuple())
        with self.assertRaisesRegex(Mt5ImportPlanError, "gap"):
            build(self.plan, self.bars, calendar)

    def test_populated_closure_partial_hours_and_insufficient_calendar_coverage_rejected(self):
        closed = self.bars[:4] + tuple(replace(
            bar, start_time=bar.start_time - timedelta(hours=1),
            timestamp=bar.timestamp - timedelta(hours=1), available_at=bar.available_at - timedelta(hours=1),
        ) for bar in self.bars[4:]) + self.bars[4:]
        with self.assertRaisesRegex(Mt5ImportPlanError, "overlaps a scheduled closure"):
            build(self.plan, closed, self.calendar)
        partial = replace(self.calendar, closures=(replace(
            self.calendar.closures[0], start=self.calendar.closures[0].start + timedelta(minutes=15)),))
        with self.assertRaisesRegex(Mt5ImportPlanError, "whole UTC hour"):
            build(self.plan, self.bars, partial)
        shorter = replace(self.calendar, coverage_end=self.calendar.coverage_end - timedelta(minutes=15))
        with self.assertRaisesRegex(Mt5ImportPlanError, "coverage"):
            build(self.plan, self.bars, shorter)

    def test_builder_rejects_wrong_object_types(self):
        with self.assertRaises(Mt5ImportPlanError):
            build({}, self.bars, self.calendar)
        with self.assertRaises(Mt5ImportPlanError):
            build(self.plan, self.bars, {})


if __name__ == "__main__":
    unittest.main()
