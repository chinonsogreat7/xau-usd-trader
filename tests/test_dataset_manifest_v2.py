import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xau_trader.dataset_manifest import (
    DatasetManifestError,
    SessionCalendar,
    dataset_manifest_from_dict,
    load_dataset_manifest,
    validate_calendar_binding,
    validate_dataset_binding,
)
from xau_trader.domain import AvailabilityBasis, QuoteBar
from xau_trader.session_calendar import session_calendar_from_dict


ARTIFACT_HASH = "c" * 64
LEGACY_FINGERPRINT = "0c61e9e3c2ad522575679fd5a91385a5412f3ebc4baa26c2a7285432a64ffb2a"


def _v1_payload():
    return {
        "schema": "xau_trader.dataset_manifest",
        "version": 1,
        "provider": {
            "name": "Local deterministic generator",
            "legal_entity": "Research project owner",
        },
        "instrument": {
            "symbol": "XAU_USD",
            "product_form": "synthetic bid/ask spot-price series",
        },
        "source": {
            "route": "local-generator://xau-m15-fixture-v1",
            "account_environment": "local research process with no trading account",
            "acquisition_basis": "synthetic_generation",
            "request_parameters": {"bar_count": "4", "generator_version": "fixture-v1"},
            "time_normalization_rule": "native UTC quarter-hour boundaries; no conversion",
            "retrieved_at": "2026-01-02T00:00:00Z",
        },
        "bars": {
            "timeframe": "M15",
            "duration_seconds": 900,
            "timezone": "UTC",
            "interval": "start_inclusive_end_exclusive",
            "timestamp": "completed_bar_end",
            "availability_basis": "synthetic",
        },
        "hashes": {"raw_sha256": "a" * 64, "normalized_csv_sha256": "b" * 64},
        "rights": {
            "basis": "self-generated research fixture",
            "api_data_agreement_version": "project-owned fixture policy v1",
            "retention_basis": "project-owned fixture retained with tests",
        },
        "session_calendar": {"id": "synthetic-continuous-utc", "version": "fixture-v1"},
        "coverage": {
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T01:00:00Z",
            "row_count": 4,
            "known_gaps": [],
        },
    }


def _calendar_payload():
    return {
        "schema": "xau_trader.session_calendar",
        "version": 1,
        "id": "synthetic-continuous-utc",
        "revision": "fixture-v1",
        "provider_name": "Local deterministic generator",
        "provider_legal_entity": "Research project owner",
        "instrument": "XAU_USD",
        "product_form": "synthetic bid/ask spot-price series",
        "coverage_start": "2025-12-31T00:00:00Z",
        "coverage_end": "2026-01-03T00:00:00Z",
        "source_reference": "local-calendar://synthetic-fixture-v1",
        "retrieved_at": "2025-12-30T00:00:00Z",
        "closures": [],
    }


def _v2_payload(calendar=None):
    if calendar is None:
        calendar = session_calendar_from_dict(_calendar_payload())
    payload = _v1_payload()
    payload["version"] = 2
    payload["session_calendar"].update(
        {
            "artifact_uri": "file:///fixtures/synthetic-calendar.json",
            "artifact_sha256": ARTIFACT_HASH,
            "content_fingerprint": calendar.fingerprint,
        }
    )
    return payload


def _bars(minutes=(0, 15, 30, 45)):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return tuple(
        QuoteBar.from_mid(
            start_time=start + timedelta(minutes=minute),
            timestamp=start + timedelta(minutes=minute + 15),
            availability_basis=AvailabilityBasis.SYNTHETIC,
            available_at=start + timedelta(minutes=minute + 15),
            mid_open=2000.0,
            mid_high=2001.0,
            mid_low=1999.0,
            mid_close=2000.5,
            spread=0.2,
            volume=100.0,
        )
        for minute in minutes
    )


class DatasetManifestV2ParsingTests(unittest.TestCase):
    def test_legacy_json_and_fingerprint_remain_unchanged(self):
        payload = _v1_payload()
        manifest = dataset_manifest_from_dict(payload)
        self.assertEqual(manifest.as_dict(), payload)
        self.assertEqual(manifest.fingerprint, LEGACY_FINGERPRINT)
        self.assertEqual(
            manifest.canonical_json,
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        )
        self.assertEqual(manifest.session_calendar, SessionCalendar("synthetic-continuous-utc", "fixture-v1"))
        self.assertEqual(manifest.identity, "xau_trader.dataset_manifest:v1:" + LEGACY_FINGERPRINT)

    def test_legacy_hash_normalization_is_unchanged(self):
        payload = _v1_payload()
        payload["hashes"]["raw_sha256"] = " AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA "
        self.assertEqual(dataset_manifest_from_dict(payload).fingerprint, LEGACY_FINGERPRINT)

    def test_v2_round_trip_and_file_loading(self):
        payload = _v2_payload()
        manifest = dataset_manifest_from_dict(payload)
        self.assertEqual(manifest.as_dict(), payload)
        reordered = {key: payload[key] for key in reversed(payload)}
        self.assertEqual(dataset_manifest_from_dict(reordered).fingerprint, manifest.fingerprint)
        self.assertEqual(manifest.identity, "xau_trader.dataset_manifest:v2:" + manifest.fingerprint)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(load_dataset_manifest(path), manifest)

    def test_v2_fingerprint_binds_each_calendar_field(self):
        payload = _v2_payload()
        original = dataset_manifest_from_dict(payload)
        replacements = {
            "id": "synthetic-weekday-utc",
            "version": "fixture-v2",
            "artifact_uri": "file:///fixtures/another-calendar.json",
            "artifact_sha256": "d" * 64,
            "content_fingerprint": "e" * 64,
        }
        for field, value in replacements.items():
            with self.subTest(field=field):
                changed = copy.deepcopy(payload)
                changed["session_calendar"][field] = value
                self.assertNotEqual(dataset_manifest_from_dict(changed).fingerprint, original.fingerprint)

    def test_v2_requires_exact_calendar_fields(self):
        for field in ("id", "version", "artifact_uri", "artifact_sha256", "content_fingerprint"):
            with self.subTest(missing=field):
                payload = _v2_payload()
                del payload["session_calendar"][field]
                with self.assertRaisesRegex(DatasetManifestError, "missing fields"):
                    dataset_manifest_from_dict(payload)
        payload = _v2_payload()
        payload["session_calendar"]["assumed_timezone"] = "UTC"
        with self.assertRaisesRegex(DatasetManifestError, "unknown fields"):
            dataset_manifest_from_dict(payload)
        for field in ("artifact_uri", "artifact_sha256", "content_fingerprint"):
            with self.subTest(null=field):
                payload = _v2_payload()
                payload["session_calendar"][field] = None
                with self.assertRaises(DatasetManifestError):
                    dataset_manifest_from_dict(payload)

    def test_v1_rejects_each_artifact_field_even_null(self):
        for field in ("artifact_uri", "artifact_sha256", "content_fingerprint"):
            with self.subTest(field=field):
                payload = _v1_payload()
                payload["session_calendar"][field] = None
                with self.assertRaisesRegex(DatasetManifestError, "unknown fields"):
                    dataset_manifest_from_dict(payload)

    def test_dataclass_versions_cannot_bypass_calendar_requirements(self):
        v1 = dataset_manifest_from_dict(_v1_payload())
        v2 = dataset_manifest_from_dict(_v2_payload())
        for manifest, version in ((v1, 2), (v2, 1), (v2, 3), (v1, True)):
            with self.subTest(version=version, original=manifest.version):
                with self.assertRaises(DatasetManifestError):
                    replace(manifest, version=version)
        with self.assertRaisesRegex(DatasetManifestError, "provided together"):
            SessionCalendar("synthetic-continuous-utc", "fixture-v1", artifact_uri="file:///calendar.json")
        for version in (0, 3, True, 2.0, "2"):
            with self.subTest(version=version):
                payload = _v2_payload()
                payload["version"] = version
                with self.assertRaises(DatasetManifestError):
                    dataset_manifest_from_dict(payload)

    def test_artifact_hashes_require_exact_lowercase_sha256(self):
        for field in ("artifact_sha256", "content_fingerprint"):
            for value in ("a" * 63, "g" * 64, "A" * 64, " " + "a" * 64, 123, ""):
                with self.subTest(field=field, value=value):
                    payload = _v2_payload()
                    payload["session_calendar"][field] = value
                    with self.assertRaises(DatasetManifestError):
                        dataset_manifest_from_dict(payload)

    def test_artifact_uri_is_nonempty_and_redacted(self):
        for value in (
            "", "TBD", "<calendar-uri>",
            "https://user:secret@example.test/calendar.json",
            "https://example.test/calendar.json?token=secret",
            "https://example.test/calendar.json#secret",
            "Authorization: Bearer secret",
            "Authorization = secret",
            "Basic\tsecret",
            "file:///calendar.json\nsecret",
            "file:///calendar.json\n",
            "file:///calendar.json\x00secret",
            "https://[malformed/calendar.json",
        ):
            with self.subTest(value=value):
                payload = _v2_payload()
                payload["session_calendar"]["artifact_uri"] = value
                with self.assertRaises(DatasetManifestError):
                    dataset_manifest_from_dict(payload)


class DatasetCalendarBindingTests(unittest.TestCase):
    def setUp(self):
        self.calendar = session_calendar_from_dict(_calendar_payload())
        self.manifest = dataset_manifest_from_dict(_v2_payload(self.calendar))

    def test_binding_returns_json_safe_identity(self):
        result = validate_calendar_binding(self.manifest, self.calendar, artifact_sha256=ARTIFACT_HASH)
        self.assertEqual(
            result,
            {
                "calendar_id": self.calendar.calendar_id,
                "revision": self.calendar.revision,
                "artifact_sha256": ARTIFACT_HASH,
                "content_fingerprint": self.calendar.fingerprint,
            },
        )
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_v1_and_wrong_object_types_are_rejected(self):
        with self.assertRaisesRegex(DatasetManifestError, "manifest v2"):
            validate_calendar_binding(dataset_manifest_from_dict(_v1_payload()), self.calendar, artifact_sha256=ARTIFACT_HASH)
        with self.assertRaisesRegex(DatasetManifestError, "DatasetManifest"):
            validate_calendar_binding({}, self.calendar, artifact_sha256=ARTIFACT_HASH)
        with self.assertRaisesRegex(DatasetManifestError, "SessionCalendarArtifact"):
            validate_calendar_binding(self.manifest, {}, artifact_sha256=ARTIFACT_HASH)

    def test_raw_artifact_hash_must_match(self):
        for digest in ("d" * 64, ARTIFACT_HASH.upper(), "bad", None):
            with self.subTest(digest=digest):
                with self.assertRaisesRegex(DatasetManifestError, "artifact_sha256"):
                    validate_calendar_binding(self.manifest, self.calendar, artifact_sha256=digest)

    def test_canonical_hash_must_match_even_when_raw_hash_is_supplied(self):
        changed = replace(self.calendar, source_reference="local-calendar://edited-fixture")
        with self.assertRaisesRegex(DatasetManifestError, "content_fingerprint"):
            validate_calendar_binding(self.manifest, changed, artifact_sha256=ARTIFACT_HASH)

    def test_raw_bytes_and_canonical_hash_are_distinct(self):
        compact = self.calendar.canonical_json.encode("utf-8")
        formatted = json.dumps(self.calendar.as_dict(), indent=2).encode("utf-8")
        compact_hash = hashlib.sha256(compact).hexdigest()
        formatted_hash = hashlib.sha256(formatted).hexdigest()
        self.assertNotEqual(compact_hash, formatted_hash)
        payload = _v2_payload(self.calendar)
        payload["session_calendar"]["artifact_sha256"] = formatted_hash
        manifest = dataset_manifest_from_dict(payload)
        validate_calendar_binding(manifest, self.calendar, artifact_sha256=formatted_hash)
        with self.assertRaisesRegex(DatasetManifestError, "artifact_sha256"):
            validate_calendar_binding(manifest, self.calendar, artifact_sha256=compact_hash)

    def test_identity_provider_and_product_must_match_even_with_valid_hashes(self):
        cases = (
            ("calendar_id", "another-calendar", "calendar id"),
            ("revision", "fixture-v2", "revision"),
            ("provider_name", "Another provider", "provider_name"),
            ("provider_legal_entity", "Another legal entity", "provider_legal_entity"),
            ("product_form", "synthetic futures-price series", "product_form"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                calendar = replace(self.calendar, **{field: value})
                manifest = dataset_manifest_from_dict(_v2_payload(calendar))
                with self.assertRaisesRegex(DatasetManifestError, message):
                    validate_calendar_binding(manifest, calendar, artifact_sha256=ARTIFACT_HASH)

    def test_instrument_mismatch_is_rejected_defensively(self):
        calendar = copy.copy(self.calendar)
        object.__setattr__(calendar, "instrument", "EUR_USD")
        manifest = dataset_manifest_from_dict(_v2_payload(calendar))
        with self.assertRaisesRegex(DatasetManifestError, "instrument"):
            validate_calendar_binding(manifest, calendar, artifact_sha256=ARTIFACT_HASH)

    def test_calendar_coverage_must_contain_entire_dataset(self):
        for field, value in (
            ("coverage_start", datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc)),
            ("coverage_end", datetime(2026, 1, 1, 0, 45, tzinfo=timezone.utc)),
        ):
            with self.subTest(field=field):
                calendar = replace(self.calendar, **{field: value})
                manifest = dataset_manifest_from_dict(_v2_payload(calendar))
                with self.assertRaisesRegex(DatasetManifestError, "coverage"):
                    validate_calendar_binding(manifest, calendar, artifact_sha256=ARTIFACT_HASH)
        calendar = replace(self.calendar, coverage_start=self.manifest.coverage.start, coverage_end=self.manifest.coverage.end)
        manifest = dataset_manifest_from_dict(_v2_payload(calendar))
        validate_calendar_binding(manifest, calendar, artifact_sha256=ARTIFACT_HASH)

    def test_calendar_retrieval_cannot_follow_dataset_retrieval(self):
        calendar = replace(self.calendar, retrieved_at=self.manifest.source.retrieved_at + timedelta(seconds=1))
        manifest = dataset_manifest_from_dict(_v2_payload(calendar))
        with self.assertRaisesRegex(DatasetManifestError, "retrieved_at"):
            validate_calendar_binding(manifest, calendar, artifact_sha256=ARTIFACT_HASH)
        calendar = replace(self.calendar, retrieved_at=self.manifest.source.retrieved_at)
        manifest = dataset_manifest_from_dict(_v2_payload(calendar))
        validate_calendar_binding(manifest, calendar, artifact_sha256=ARTIFACT_HASH)

    def test_calendar_can_be_retrieved_before_future_coverage(self):
        self.assertLess(self.calendar.retrieved_at, self.calendar.coverage_start)
        validate_calendar_binding(self.manifest, self.calendar, artifact_sha256=ARTIFACT_HASH)

    def test_v2_preserves_exact_dataset_gap_binding(self):
        payload = _v2_payload(self.calendar)
        payload["coverage"].update(
            {
                "end": "2026-01-01T01:15:00Z",
                "known_gaps": [
                    {
                        "start": "2026-01-01T00:30:00Z",
                        "end": "2026-01-01T00:45:00Z",
                        "reason": "declared maintenance",
                    }
                ],
            }
        )
        manifest = dataset_manifest_from_dict(payload)
        valid = validate_dataset_binding(manifest, _bars((0, 15, 45, 60)), raw_sha256="a" * 64, normalized_csv_sha256="b" * 64)
        self.assertEqual(valid.known_gap_count, 1)
        with self.assertRaisesRegex(DatasetManifestError, "exactly match"):
            validate_dataset_binding(manifest, _bars((0, 30, 45, 60)), raw_sha256="a" * 64, normalized_csv_sha256="b" * 64)


if __name__ == "__main__":
    unittest.main()
