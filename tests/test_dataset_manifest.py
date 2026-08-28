import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xau_trader.dataset_manifest import (
    DatasetManifestError,
    dataset_manifest_from_dict,
    load_dataset_manifest,
    validate_dataset_binding,
)
from xau_trader.domain import AvailabilityBasis, QuoteBar


RAW_HASH = "a" * 64
NORMALIZED_HASH = "b" * 64


def _payload():
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
            "request_parameters": {
                "bar_count": "4",
                "generator_version": "fixture-v1",
            },
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
        "hashes": {
            "raw_sha256": RAW_HASH,
            "normalized_csv_sha256": NORMALIZED_HASH,
        },
        "rights": {
            "basis": "self-generated research fixture",
            "api_data_agreement_version": "project-owned fixture policy v1",
            "retention_basis": "project-owned fixture retained with tests",
        },
        "session_calendar": {
            "id": "synthetic-continuous-utc",
            "version": "fixture-v1",
        },
        "coverage": {
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T01:00:00Z",
            "row_count": 4,
            "known_gaps": [],
        },
    }


def _bar(start, *, basis=AvailabilityBasis.SYNTHETIC, available_at=None, minutes=15):
    end = start + timedelta(minutes=minutes)
    return QuoteBar.from_mid(
        start_time=start,
        timestamp=end,
        availability_basis=basis,
        available_at=end if available_at is None else available_at,
        mid_open=2000.0,
        mid_high=2001.0,
        mid_low=1999.0,
        mid_close=2000.5,
        spread=0.2,
        volume=100.0,
    )


def _bars():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return tuple(_bar(start + timedelta(minutes=15 * index)) for index in range(4))


class DatasetManifestParsingTests(unittest.TestCase):
    def test_complete_manifest_round_trips_and_has_stable_identity(self):
        payload = _payload()
        manifest = dataset_manifest_from_dict(payload)
        reordered = {key: payload[key] for key in reversed(list(payload))}
        second = dataset_manifest_from_dict(reordered)

        self.assertEqual(manifest.as_dict(), payload)
        self.assertEqual(manifest.canonical_json, second.canonical_json)
        self.assertEqual(manifest.fingerprint, second.fingerprint)
        self.assertEqual(len(manifest.fingerprint), 64)
        self.assertEqual(
            manifest.identity,
            "xau_trader.dataset_manifest:v1:" + manifest.fingerprint,
        )

    def test_manifest_is_immutable(self):
        manifest = dataset_manifest_from_dict(_payload())
        with self.assertRaises(FrozenInstanceError):
            manifest.version = 2
        with self.assertRaises(FrozenInstanceError):
            manifest.provider.name = "Changed"

    def test_load_rejects_unknown_root_and_nested_fields(self):
        for path, key in (((), "extra"), (("bars",), "guessed")):
            with self.subTest(path=path):
                payload = _payload()
                target = payload
                for segment in path:
                    target = target[segment]
                target[key] = True
                with self.assertRaisesRegex(DatasetManifestError, "unknown fields"):
                    dataset_manifest_from_dict(payload)

    def test_load_rejects_missing_fields_at_every_level(self):
        for path, key in (
            ((), "rights"),
            (("provider",), "legal_entity"),
            (("coverage",), "known_gaps"),
        ):
            with self.subTest(path=path, key=key):
                payload = _payload()
                target = payload
                for segment in path:
                    target = target[segment]
                del target[key]
                with self.assertRaisesRegex(DatasetManifestError, "missing fields"):
                    dataset_manifest_from_dict(payload)

    def test_json_loader_rejects_duplicates_and_nonstandard_constants(self):
        with tempfile.TemporaryDirectory() as directory:
            duplicate_path = Path(directory) / "duplicate.json"
            duplicate_path.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
            with self.assertRaisesRegex(DatasetManifestError, "duplicate JSON field"):
                load_dataset_manifest(duplicate_path)

            nan_path = Path(directory) / "nan.json"
            nan_path.write_text('{"value":NaN}', encoding="utf-8")
            with self.assertRaisesRegex(DatasetManifestError, "non-standard JSON"):
                load_dataset_manifest(nan_path)

    def test_json_loader_reads_a_complete_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.manifest.json"
            path.write_text(json.dumps(_payload()), encoding="utf-8")
            self.assertEqual(load_dataset_manifest(path).coverage.row_count, 4)

    def test_placeholder_and_empty_values_are_rejected(self):
        cases = (
            (("provider", "name"), " "),
            (("provider", "legal_entity"), "TBD"),
            (("instrument", "product_form"), "<product-form>"),
            (("source", "route"), "replace-me"),
            (("source", "account_environment"), "TBD"),
            (("source", "time_normalization_rule"), "unknown"),
            (("rights", "basis"), "unknown"),
            (("rights", "api_data_agreement_version"), "pending"),
            (("rights", "retention_basis"), "your_retention_rule"),
            (("session_calendar", "id"), "placeholder"),
        )
        for path, value in cases:
            with self.subTest(path=path, value=value):
                payload = _payload()
                target = payload
                for segment in path[:-1]:
                    target = target[segment]
                target[path[-1]] = value
                with self.assertRaisesRegex(DatasetManifestError, "empty|placeholder"):
                    dataset_manifest_from_dict(payload)

    def test_schema_instrument_and_timeframe_are_closed(self):
        cases = (
            (("schema",), "other.schema"),
            (("version",), 2),
            (("instrument", "symbol"), "XAUUSD"),
            (("bars", "timeframe"), "H1"),
            (("bars", "duration_seconds"), 3600),
            (("bars", "timezone"), "Africa/Lagos"),
            (("bars", "interval"), "closed"),
            (("bars", "timestamp"), "bar_start"),
            (("bars", "availability_basis"), "guessed_close"),
        )
        for path, value in cases:
            with self.subTest(path=path, value=value):
                payload = _payload()
                target = payload
                for segment in path[:-1]:
                    target = target[segment]
                target[path[-1]] = value
                with self.assertRaises(DatasetManifestError):
                    dataset_manifest_from_dict(payload)

    def test_hashes_must_be_sha256_digests(self):
        for field, value in (
            ("raw_sha256", "a" * 63),
            ("normalized_csv_sha256", "g" * 64),
            ("normalized_csv_sha256", 123),
        ):
            with self.subTest(field=field):
                payload = _payload()
                payload["hashes"][field] = value
                with self.assertRaisesRegex(DatasetManifestError, "SHA256|string"):
                    dataset_manifest_from_dict(payload)

    def test_request_parameters_are_required_strings_and_canonically_sorted(self):
        payload = _payload()
        payload["source"]["request_parameters"] = {}
        with self.assertRaisesRegex(DatasetManifestError, "must not be empty"):
            dataset_manifest_from_dict(payload)

        payload = _payload()
        payload["source"]["request_parameters"]["bar_count"] = 4
        with self.assertRaisesRegex(DatasetManifestError, "must be a string"):
            dataset_manifest_from_dict(payload)

        manifest = dataset_manifest_from_dict(_payload())
        self.assertEqual(
            manifest.source.request_parameters,
            (("bar_count", "4"), ("generator_version", "fixture-v1")),
        )

    def test_manifest_rejects_credential_like_parameter_names_and_values(self):
        names = (
            "token",
            "access_token",
            "API-Key",
            "client_secret",
            "password",
            "passphrase",
            "Authorization",
            "auth_header",
            "private_key",
            "cookie",
        )
        for name in names:
            with self.subTest(name=name):
                payload = _payload()
                payload["source"]["request_parameters"][name] = "redacted-value"
                with self.assertRaisesRegex(DatasetManifestError, "credential-like"):
                    dataset_manifest_from_dict(payload)

        for value in ("Bearer abc123", "Basic dXNlcjpwYXNz", "line one\nsecret"):
            with self.subTest(value=value):
                payload = _payload()
                payload["source"]["request_parameters"]["granularity"] = value
                with self.assertRaisesRegex(DatasetManifestError, "credential|single-line"):
                    dataset_manifest_from_dict(payload)

    def test_route_must_be_redacted_and_separate_from_request_parameters(self):
        routes = (
            "https://user:password@example.test/v3/candles",
            "https://example.test/v3/candles?api_key=secret",
            "https://example.test/v3/candles#token",
            "GET /v3/candles Authorization: Bearer secret",
        )
        for route in routes:
            with self.subTest(route=route):
                payload = _payload()
                payload["source"]["route"] = route
                with self.assertRaisesRegex(DatasetManifestError, "route.*credential|query"):
                    dataset_manifest_from_dict(payload)

    def test_timestamps_must_be_valid_and_explicitly_utc(self):
        cases = (
            (("source", "retrieved_at"), "2026-01-02T00:00:00"),
            (("source", "retrieved_at"), "2026-01-02T01:00:00+01:00"),
            (("coverage", "start"), "not-a-time"),
        )
        for path, value in cases:
            with self.subTest(path=path):
                payload = _payload()
                target = payload
                for segment in path[:-1]:
                    target = target[segment]
                target[path[-1]] = value
                with self.assertRaises(DatasetManifestError):
                    dataset_manifest_from_dict(payload)

    def test_retrieval_cannot_precede_coverage(self):
        payload = _payload()
        payload["source"]["retrieved_at"] = "2026-01-01T00:30:00Z"
        with self.assertRaisesRegex(DatasetManifestError, "cannot precede"):
            dataset_manifest_from_dict(payload)

    def test_counts_and_coverage_arithmetic_are_strict(self):
        for count in (0, -1, True, 4.0):
            with self.subTest(count=count):
                payload = _payload()
                payload["coverage"]["row_count"] = count
                with self.assertRaises(DatasetManifestError):
                    dataset_manifest_from_dict(payload)

        payload = _payload()
        payload["coverage"]["end"] = "2026-01-01T01:15:00Z"
        with self.assertRaisesRegex(DatasetManifestError, "inconsistent"):
            dataset_manifest_from_dict(payload)

    def test_known_gaps_must_be_aligned_inside_ordered_and_non_overlapping(self):
        invalid_gap_sets = (
            [
                {
                    "start": "2025-12-31T23:45:00Z",
                    "end": "2026-01-01T00:00:00Z",
                    "reason": "declared closure",
                }
            ],
            [
                {
                    "start": "2026-01-01T00:10:00Z",
                    "end": "2026-01-01T00:20:00Z",
                    "reason": "provider outage",
                }
            ],
            [
                {
                    "start": "2026-01-01T00:30:00Z",
                    "end": "2026-01-01T00:45:00Z",
                    "reason": "gap two",
                },
                {
                    "start": "2026-01-01T00:15:00Z",
                    "end": "2026-01-01T00:30:00Z",
                    "reason": "gap one",
                },
            ],
        )
        for gaps in invalid_gap_sets:
            with self.subTest(gaps=gaps):
                payload = _payload()
                payload["coverage"]["known_gaps"] = gaps
                payload["coverage"]["row_count"] = 3 if len(gaps) == 1 else 2
                with self.assertRaises(DatasetManifestError):
                    dataset_manifest_from_dict(payload)

    def test_fingerprint_changes_when_a_material_claim_changes(self):
        first = dataset_manifest_from_dict(_payload())
        payload = _payload()
        payload["rights"]["retention_basis"] = "fixture retained for seven years"
        second = dataset_manifest_from_dict(payload)
        self.assertNotEqual(first.fingerprint, second.fingerprint)


class DatasetManifestBindingTests(unittest.TestCase):
    def test_exact_bar_and_hash_binding_succeeds(self):
        manifest = dataset_manifest_from_dict(_payload())
        result = validate_dataset_binding(
            manifest,
            _bars(),
            raw_sha256=RAW_HASH,
            normalized_csv_sha256=NORMALIZED_HASH,
        )
        self.assertEqual(result.manifest_identity, manifest.identity)
        self.assertEqual(result.row_count, 4)
        self.assertEqual(result.known_gap_count, 0)

    def test_binding_rejects_hash_mismatches(self):
        manifest = dataset_manifest_from_dict(_payload())
        for field in ("raw_sha256", "normalized_csv_sha256"):
            with self.subTest(field=field):
                arguments = {
                    "raw_sha256": RAW_HASH,
                    "normalized_csv_sha256": NORMALIZED_HASH,
                }
                arguments[field] = "c" * 64
                with self.assertRaisesRegex(DatasetManifestError, "does not match"):
                    validate_dataset_binding(manifest, _bars(), **arguments)

    def test_binding_rejects_row_and_coverage_mismatches(self):
        manifest = dataset_manifest_from_dict(_payload())
        cases = (_bars()[:-1], _bars()[1:])
        for bars in cases:
            with self.subTest(first=bars[0].start_time):
                with self.assertRaisesRegex(DatasetManifestError, "row count"):
                    validate_dataset_binding(
                        manifest,
                        bars,
                        raw_sha256=RAW_HASH,
                        normalized_csv_sha256=NORMALIZED_HASH,
                    )

        shifted = list(_bars())
        shifted[0] = _bar(datetime(2025, 12, 31, 23, 45, tzinfo=timezone.utc))
        with self.assertRaisesRegex(DatasetManifestError, "first bar start"):
            validate_dataset_binding(
                manifest,
                shifted,
                raw_sha256=RAW_HASH,
                normalized_csv_sha256=NORMALIZED_HASH,
            )

    def test_binding_rejects_availability_duration_and_alignment_mismatches(self):
        manifest = dataset_manifest_from_dict(_payload())
        cases = []
        wrong_basis = list(_bars())
        wrong_basis[1] = _bar(
            wrong_basis[1].start_time,
            basis=AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION,
        )
        cases.append((wrong_basis, "availability basis"))
        wrong_duration = list(_bars())
        wrong_duration[1] = _bar(wrong_duration[1].start_time, minutes=10)
        cases.append((wrong_duration, "duration"))
        unaligned = list(_bars())
        unaligned[1] = _bar(
            datetime(2026, 1, 1, 0, 16, tzinfo=timezone.utc),
        )
        cases.append((unaligned, "aligned"))
        for bars, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(DatasetManifestError, message):
                    validate_dataset_binding(
                        manifest,
                        bars,
                        raw_sha256=RAW_HASH,
                        normalized_csv_sha256=NORMALIZED_HASH,
                    )

    def test_declared_gap_must_match_actual_interval_exactly(self):
        payload = _payload()
        payload["coverage"].update(
            {
                "end": "2026-01-01T01:15:00Z",
                "known_gaps": [
                    {
                        "start": "2026-01-01T00:30:00Z",
                        "end": "2026-01-01T00:45:00Z",
                        "reason": "declared provider maintenance",
                    }
                ],
            }
        )
        manifest = dataset_manifest_from_dict(payload)
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = tuple(
            _bar(start + timedelta(minutes=minute)) for minute in (0, 15, 45, 60)
        )
        result = validate_dataset_binding(
            manifest,
            bars,
            raw_sha256=RAW_HASH,
            normalized_csv_sha256=NORMALIZED_HASH,
        )
        self.assertEqual(result.known_gap_count, 1)

        differently_gapped = tuple(
            _bar(start + timedelta(minutes=minute)) for minute in (0, 30, 45, 60)
        )
        with self.assertRaisesRegex(DatasetManifestError, "gaps"):
            validate_dataset_binding(
                manifest,
                differently_gapped,
                raw_sha256=RAW_HASH,
                normalized_csv_sha256=NORMALIZED_HASH,
            )

    def test_observed_undeclared_gap_is_rejected(self):
        manifest = dataset_manifest_from_dict(_payload())
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = tuple(_bar(start + timedelta(minutes=minute)) for minute in (0, 15, 45, 45))
        with self.assertRaises(DatasetManifestError):
            validate_dataset_binding(
                manifest,
                bars,
                raw_sha256=RAW_HASH,
                normalized_csv_sha256=NORMALIZED_HASH,
            )

    def test_retrieval_must_not_precede_actual_bar_availability(self):
        manifest = dataset_manifest_from_dict(_payload())
        bars = list(_bars())
        bars[-1] = _bar(
            bars[-1].start_time,
            available_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(DatasetManifestError, "available_at"):
            validate_dataset_binding(
                manifest,
                bars,
                raw_sha256=RAW_HASH,
                normalized_csv_sha256=NORMALIZED_HASH,
            )

    def test_close_assumption_and_synthetic_availability_cannot_be_delayed(self):
        for basis in (
            AvailabilityBasis.SYNTHETIC,
            AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION,
        ):
            with self.subTest(basis=basis):
                payload = _payload()
                payload["bars"]["availability_basis"] = basis.value
                manifest = dataset_manifest_from_dict(payload)
                bars = [
                    _bar(bar.start_time, basis=basis)
                    for bar in _bars()
                ]
                bars[-1] = _bar(
                    bars[-1].start_time,
                    basis=basis,
                    available_at=bars[-1].timestamp + timedelta(seconds=1),
                )
                with self.assertRaisesRegex(DatasetManifestError, "must equal its end"):
                    validate_dataset_binding(
                        manifest,
                        bars,
                        raw_sha256=RAW_HASH,
                        normalized_csv_sha256=NORMALIZED_HASH,
                    )

    def test_observed_receipt_may_be_later_than_the_bar_end(self):
        payload = _payload()
        payload["source"]["acquisition_basis"] = "api_response"
        payload["bars"]["availability_basis"] = "observed_receipt"
        manifest = dataset_manifest_from_dict(payload)
        bars = tuple(
            _bar(
                bar.start_time,
                basis=AvailabilityBasis.OBSERVED_RECEIPT,
                available_at=bar.timestamp + timedelta(seconds=2),
            )
            for bar in _bars()
        )
        result = validate_dataset_binding(
            manifest,
            bars,
            raw_sha256=RAW_HASH,
            normalized_csv_sha256=NORMALIZED_HASH,
        )
        self.assertEqual(result.row_count, 4)

    def test_non_bar_and_empty_sequences_are_rejected(self):
        manifest = dataset_manifest_from_dict(_payload())
        for bars in ((), ("not-a-bar",)):
            with self.subTest(bars=bars):
                with self.assertRaises(DatasetManifestError):
                    validate_dataset_binding(
                        manifest,
                        bars,
                        raw_sha256=RAW_HASH,
                        normalized_csv_sha256=NORMALIZED_HASH,
                    )


if __name__ == "__main__":
    unittest.main()
