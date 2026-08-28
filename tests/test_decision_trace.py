import csv
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest

from xau_trader.data import generate_demo_m15_bars
from xau_trader.decision_trace import (
    DECISION_CSV_COLUMNS,
    TRACE_SCHEMA,
    TRACE_SCHEMA_VERSION,
    diagnostic_decisions_csv_bytes,
    diagnostic_trace_dict,
    diagnostic_trace_json_bytes,
    write_diagnostic_decisions_csv,
    write_diagnostic_trace_artifacts,
    write_diagnostic_trace_json,
)
from xau_trader.diagnostic_replay import (
    DIAGNOSTIC_REPLAY_ENGINE_VERSION,
    DIAGNOSTIC_REPLAY_SCHEMA_VERSION,
    DiagnosticReplayDataError,
    run_diagnostic_replay,
)
from xau_trader.research_baseline import provisional_diagnostic_baseline_v1


class DecisionTraceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_diagnostic_replay(
            generate_demo_m15_bars(120),
            dataset_fingerprint="a" * 64,
            policy_bundle=provisional_diagnostic_baseline_v1(),
        )

    def test_complete_json_schema_has_explicit_streams_and_lifecycle_union(self):
        payload = diagnostic_trace_dict(self.result)

        self.assertEqual(payload["schema"], TRACE_SCHEMA)
        self.assertEqual(payload["schema_version"], TRACE_SCHEMA_VERSION)
        self.assertIs(payload["diagnostic_only"], True)
        self.assertIsNone(payload["execution"])
        self.assertEqual(
            payload["dataset"], {"fingerprint": self.result.dataset_fingerprint}
        )
        self.assertEqual(
            payload["replay"]["fingerprint"], self.result.fingerprint
        )
        self.assertEqual(
            payload["replay"]["evidence_fingerprint"],
            self.result.evidence_fingerprint,
        )
        self.assertEqual(
            payload["replay"]["engine_version"],
            DIAGNOSTIC_REPLAY_ENGINE_VERSION,
        )
        self.assertEqual(
            payload["replay"]["schema_version"],
            DIAGNOSTIC_REPLAY_SCHEMA_VERSION,
        )
        self.assertEqual(
            payload["replay"]["policy_bundle_fingerprint"],
            self.result.policy_bundle_fingerprint,
        )
        self.assertEqual(
            payload["readiness"],
            {
                "history_boundary": "empty_state_at_dataset_start",
                "pre_roll_m15_bars": 96,
                "minimum_input_m15_bars": 100,
                "post_pre_roll_m15_start": "2024-01-02T00:00:00.000000Z",
                "pre_roll_decision_count": 96,
                "post_pre_roll_decision_count": 24,
            },
        )

        for stream_name in (
            "m15_bars",
            "h1_bars",
            "h1_atr",
            "h1_pivots",
            "h1_regimes",
            "h1_impulses",
            "zones",
            "m15_ema",
            "m15_candles",
            "m15_confirmations",
            "availability_groups",
        ):
            self.assertIn(stream_name, payload["streams"])
            self.assertEqual(
                len(payload["streams"][stream_name]),
                payload["counts"][stream_name],
            )

        self.assertEqual(
            len(payload["lifecycle"]["transitions"]),
            payload["counts"]["lifecycle_transitions"],
        )
        transition = payload["lifecycle"]["transitions"][0]
        self.assertIsInstance(transition["states_before"], list)
        self.assertIsInstance(transition["states_after"], list)
        self.assertIn("observation", transition)
        self.assertEqual(
            {
                event["event_type"]
                for event in payload["lifecycle"]["events"]
            },
            {
                "retest_episode_started",
                "retest_episode_ended",
                "zone_invalidated",
            },
        )
        self.assertIsInstance(payload["streams"]["h1_impulses"][0]["key"], list)
        self.assertIsInstance(payload["streams"]["zones"][0]["impulse_key"], list)
        self.assertIsInstance(payload["decisions"][0]["reasons"], list)
        self.assertTrue(
            all(
                decision["readiness"] == "pre_roll"
                for decision in payload["decisions"][:96]
            )
        )
        self.assertTrue(
            all(
                decision["readiness"] == "post_pre_roll"
                for decision in payload["decisions"][96:]
            )
        )
        self.assertEqual(payload["counts"]["decisions"], 120)
        self.assertEqual(payload["counts"]["pre_roll_decisions"], 96)
        self.assertEqual(payload["counts"]["post_pre_roll_decisions"], 24)
        self.assertEqual(
            sum(payload["counts"]["post_pre_roll_actions"].values()), 24
        )
        self.assertEqual(
            sum(payload["counts"]["decision_actions"].values()), 120
        )

    def test_json_is_deterministic_strict_and_uses_utc_z(self):
        first = diagnostic_trace_json_bytes(self.result)
        second = diagnostic_trace_json_bytes(self.result)

        self.assertEqual(first, second)
        self.assertTrue(first.endswith(b"\n"))
        self.assertFalse(first.endswith(b"\n\n"))
        text = first.decode("utf-8")
        self.assertNotIn("+00:00", text)
        self.assertIn("T00:00:00.000000Z", text)
        self.assertEqual(json.loads(text), diagnostic_trace_dict(self.result))

    def test_json_rejects_non_finite_numbers(self):
        bad_atr = replace(self.result.h1_atr[0], value=float("nan"))
        bad_result = replace(
            self.result,
            h1_atr=(bad_atr,) + self.result.h1_atr[1:],
        )

        with self.assertRaises(ValueError):
            diagnostic_trace_json_bytes(bad_result)

    def test_decision_csv_has_fixed_columns_ordered_arrays_and_empty_nulls(self):
        artifact = diagnostic_decisions_csv_bytes(self.result)
        self.assertNotIn(b"\r", artifact)
        rows = list(
            csv.DictReader(io.StringIO(artifact.decode("utf-8"), newline=""))
        )

        self.assertEqual(tuple(rows[0].keys()), DECISION_CSV_COLUMNS)
        self.assertEqual(len(rows), len(self.result.decisions))
        self.assertEqual(rows[0]["diagnostic_only"], "true")
        self.assertEqual(rows[0]["execution"], "")
        self.assertEqual(
            rows[0]["replay_engine_version"],
            DIAGNOSTIC_REPLAY_ENGINE_VERSION,
        )
        self.assertEqual(
            rows[0]["replay_schema_version"],
            str(DIAGNOSTIC_REPLAY_SCHEMA_VERSION),
        )
        self.assertEqual(
            rows[0]["evidence_fingerprint"], self.result.evidence_fingerprint
        )
        self.assertEqual(rows[0]["readiness"], "pre_roll")
        self.assertEqual(rows[95]["readiness"], "pre_roll")
        self.assertEqual(rows[96]["readiness"], "post_pre_roll")
        self.assertEqual(rows[0]["proposed_entry_at"], "")
        self.assertEqual(rows[0]["direction"], "")
        self.assertIsInstance(json.loads(rows[0]["reasons"]), list)
        self.assertIsInstance(json.loads(rows[0]["selected_zone_keys"]), list)
        self.assertTrue(rows[0]["m15_bar_start"].endswith("Z"))

    def test_safe_writers_refuse_collisions_and_allow_explicit_overwrite(self):
        expected_json = diagnostic_trace_json_bytes(self.result)
        expected_csv = diagnostic_decisions_csv_bytes(self.result)
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "trace.json"
            csv_path = Path(directory) / "decisions.csv"
            paths = write_diagnostic_trace_artifacts(
                self.result, json_path, csv_path
            )

            self.assertEqual(paths, (json_path, csv_path))
            self.assertEqual(json_path.read_bytes(), expected_json)
            self.assertEqual(csv_path.read_bytes(), expected_csv)
            with self.assertRaises(FileExistsError):
                write_diagnostic_trace_json(self.result, json_path)
            with self.assertRaises(FileExistsError):
                write_diagnostic_decisions_csv(self.result, csv_path)
            write_diagnostic_trace_artifacts(
                self.result,
                json_path,
                csv_path,
                overwrite=True,
            )
            self.assertEqual(json_path.read_bytes(), expected_json)
            self.assertEqual(csv_path.read_bytes(), expected_csv)

    def test_pair_preflight_does_not_create_other_artifact_on_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "trace.json"
            csv_path = Path(directory) / "decisions.csv"
            csv_path.write_bytes(b"keep-me")

            with self.assertRaises(FileExistsError):
                write_diagnostic_trace_artifacts(
                    self.result,
                    json_path,
                    csv_path,
                )
            self.assertFalse(json_path.exists())
            self.assertEqual(csv_path.read_bytes(), b"keep-me")

            with self.assertRaises(ValueError):
                write_diagnostic_trace_artifacts(
                    self.result,
                    json_path,
                    json_path,
                )
            self.assertFalse(json_path.exists())

    def test_all_export_boundaries_reject_a_forged_derived_stream(self):
        changed_atr = replace(
            self.result.h1_atr[0],
            value=self.result.h1_atr[0].value + 0.01,
        )
        forged = replace(
            self.result,
            h1_atr=(changed_atr,) + self.result.h1_atr[1:],
        )

        for serialize in (
            diagnostic_trace_dict,
            diagnostic_trace_json_bytes,
            diagnostic_decisions_csv_bytes,
        ):
            with self.subTest(serialize=serialize.__name__):
                with self.assertRaisesRegex(
                    DiagnosticReplayDataError,
                    "exact recomputation",
                ):
                    serialize(forged)

        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "trace.json"
            csv_path = Path(directory) / "decisions.csv"
            with self.assertRaisesRegex(
                DiagnosticReplayDataError,
                "exact recomputation",
            ):
                write_diagnostic_trace_artifacts(
                    forged,
                    json_path,
                    csv_path,
                )
            self.assertFalse(json_path.exists())
            self.assertFalse(csv_path.exists())

    def test_trace_contains_no_execution_domain_fields(self):
        payload = diagnostic_trace_dict(self.result)
        forbidden = {
            "broker",
            "order",
            "fill",
            "quantity",
            "stop",
            "target",
            "pnl",
            "profit",
        }

        def visit(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    self.assertNotIn(key.lower(), forbidden)
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(payload)


if __name__ == "__main__":
    unittest.main()
