import json
import unittest
from copy import deepcopy

from strategy_spec_fixture import valid_strategy_spec
from xau_trader.strategy_schema import (
    StrategySpecValidationError,
    ValidatedStrategySpecV1,
    parse_strategy_spec_json,
    validate_strategy_spec_v1,
)


def reverse_maps(value):
    if isinstance(value, dict):
        return {key: reverse_maps(value[key]) for key in reversed(tuple(value))}
    if isinstance(value, list):
        return [reverse_maps(item) for item in value]
    return value


class StrategySchemaTests(unittest.TestCase):
    def test_valid_spec_is_canonical_hash_bound_and_promotion_ready(self):
        validated = validate_strategy_spec_v1(valid_strategy_spec(), require_promotable=True)
        canonical = validated.as_dict()

        self.assertTrue(validated.compilation_ready)
        self.assertTrue(validated.promotion_ready)
        self.assertEqual(len(validated.spec_hash), 64)
        self.assertEqual(canonical["parameters"]["stop_multiple"]["value"], "1.5")
        self.assertEqual(canonical["sizing"]["max_units"], "1")
        self.assertEqual(
            canonical["provenance"]["sources"][0]["evidence"][0]["extractor_confidence"],
            "0.9",
        )
        self.assertEqual(validated.canonical_json, json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ))

    def test_hash_is_independent_of_map_order_but_changes_with_rules(self):
        document = valid_strategy_spec()
        first = validate_strategy_spec_v1(document)
        reordered = validate_strategy_spec_v1(reverse_maps(document))
        mutated = valid_strategy_spec()
        mutated["entry"]["cooldown_bars"] = 2

        self.assertEqual(first.spec_hash, reordered.spec_hash)
        self.assertNotEqual(first.spec_hash, validate_strategy_spec_v1(mutated).spec_hash)

    def test_unknown_missing_and_bool_as_integer_are_rejected_with_pointer(self):
        unknown = valid_strategy_spec()
        unknown["execution"]["broker_command"] = "BUY"
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(unknown)
        self.assertEqual(caught.exception.code, "unknown_field")
        self.assertEqual(caught.exception.pointer, "/execution/broker_command")

        missing = valid_strategy_spec()
        del missing["risk"]["max_quote_age_ms"]
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(missing)
        self.assertEqual(caught.exception.code, "missing_field")
        self.assertEqual(caught.exception.pointer, "/risk/max_quote_age_ms")

        boolean_revision = valid_strategy_spec()
        boolean_revision["revision"] = True
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(boolean_revision)
        self.assertEqual(caught.exception.code, "wrong_type")
        self.assertEqual(caught.exception.pointer, "/revision")

    def test_parser_rejects_duplicate_keys_and_nonfinite_numbers(self):
        with self.assertRaises(StrategySpecValidationError) as caught:
            parse_strategy_spec_json('{"spec_version":"1.0","spec_version":"2.0"}')
        self.assertEqual(caught.exception.code, "duplicate_key")

        with self.assertRaises(StrategySpecValidationError) as caught:
            parse_strategy_spec_json('{"value":NaN}')
        self.assertEqual(caught.exception.code, "invalid_json_number")

        deeply_nested = '{"a":' * 70 + "0" + "}" * 70
        with self.assertRaises(StrategySpecValidationError) as caught:
            parse_strategy_spec_json(deeply_nested)
        self.assertEqual(caught.exception.code, "document_too_deep")

    def test_decimal_json_number_and_custom_object_are_rejected(self):
        numeric_decimal = valid_strategy_spec()
        numeric_decimal["sizing"]["risk_fraction_of_equity"] = 0.01
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(numeric_decimal)
        self.assertEqual(caught.exception.code, "wrong_type")

        custom = valid_strategy_spec()
        custom["tags"] = [object()]
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(custom)
        self.assertEqual(caught.exception.code, "wrong_type")

    def test_arbitrary_conditions_operators_and_multi_key_operands_are_rejected(self):
        raw_condition = valid_strategy_spec()
        raw_condition["entry"]["long"] = "lambda bars: __import__('os')"
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(raw_condition)
        self.assertEqual(caught.exception.code, "wrong_type")
        self.assertEqual(caught.exception.pointer, "/entry/long")

        operation = valid_strategy_spec()
        operation["entry"]["long"]["op"] = "exec"
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(operation)
        self.assertEqual(caught.exception.code, "invalid_enum")

        operand = valid_strategy_spec()
        operand["entry"]["long"]["args"][0] = {
            "ref": "features.fast",
            "decimal": "1",
        }
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(operand)
        self.assertEqual(caught.exception.code, "invalid_operand")

    def test_operator_type_and_cross_time_variance_are_enforced(self):
        wrong_type = valid_strategy_spec()
        wrong_type["entry"]["long"] = {
            "op": "gt",
            "args": [{"boolean": True}, {"integer": 1}],
        }
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(wrong_type)
        self.assertEqual(caught.exception.code, "incompatible_type")

        constant_cross = valid_strategy_spec()
        constant_cross["entry"]["long"] = {
            "op": "crosses_above",
            "args": [{"decimal": "1"}, {"integer": 2}],
        }
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(constant_cross)
        self.assertEqual(caught.exception.code, "invalid_cross")

    def test_feature_references_cycles_and_warmup_fail_closed(self):
        missing = valid_strategy_spec()
        missing["features"][0]["inputs"] = [{"ref": "features.missing"}]
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(missing)
        self.assertEqual(caught.exception.code, "unresolved_reference")

        cycle = valid_strategy_spec()
        cycle["features"][0]["inputs"] = [{"ref": "features.slow"}]
        cycle["features"][1]["inputs"] = [{"ref": "features.fast"}]
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(cycle)
        self.assertEqual(caught.exception.code, "feature_cycle")

        warmup = valid_strategy_spec()
        warmup["features"][1]["warmup_bars"] = 2
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(warmup)
        self.assertEqual(caught.exception.code, "understated_warmup")

    def test_parameter_ids_cannot_collide_after_normalization(self):
        document = valid_strategy_spec()
        document["parameters"][" fast_period "] = deepcopy(
            document["parameters"]["fast_period"]
        )
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(document)
        self.assertEqual(caught.exception.code, "duplicate_id")

    def test_video_material_evidence_and_pointers_must_resolve(self):
        missing_coverage = valid_strategy_spec()
        supports = missing_coverage["provenance"]["sources"][0]["evidence"][0]["supports"]
        supports.remove("/sizing")
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(missing_coverage)
        self.assertEqual(caught.exception.code, "missing_evidence")
        self.assertEqual(caught.exception.pointer, "/sizing")

        unresolved_pointer = valid_strategy_spec()
        unresolved_pointer["assumptions"][0]["affects"] = ["/execution/not_a_field"]
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(unresolved_pointer)
        self.assertEqual(caught.exception.code, "unresolved_pointer")

        child_only = valid_strategy_spec()
        child_only["provenance"]["sources"][0]["evidence"][0]["supports"] = [
            "/entry",
            "/exit",
            "/features",
            "/market/bar_duration",
            "/parameters",
            "/sizing/type",
            "/schedule",
            "/execution/decision_point",
        ]
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(child_only)
        self.assertEqual(caught.exception.code, "missing_evidence")
        self.assertEqual(caught.exception.pointer, "/sizing")

    def test_evidence_time_and_source_time_order_are_strict(self):
        evidence = valid_strategy_spec()
        item = evidence["provenance"]["sources"][0]["evidence"][0]
        item["end_ms"] = item["start_ms"]
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(evidence)
        self.assertEqual(caught.exception.code, "invalid_evidence_time")

        source_time = valid_strategy_spec()
        source_time["provenance"]["sources"][0]["published_at"] = "2026-01-03T00:00:00Z"
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(source_time)
        self.assertEqual(caught.exception.code, "invalid_time_order")

        future_evidence = valid_strategy_spec()
        future_evidence["provenance"]["sources"][0]["ingested_at"] = (
            "2026-12-01T00:00:00Z"
        )
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(future_evidence, require_promotable=True)
        self.assertEqual(caught.exception.code, "future_provenance")
        self.assertEqual(
            caught.exception.pointer, "/provenance/sources/0/ingested_at"
        )

    def test_source_uri_errors_are_stable_and_http_requires_a_host(self):
        for uri in (
            "http://[",
            "http:relative",
            "https:///missing-host",
            "https://example.com:bad",
            "https://example.com:99999",
        ):
            with self.subTest(uri=uri):
                document = valid_strategy_spec()
                document["provenance"]["sources"][0]["uri"] = uri
                with self.assertRaises(StrategySpecValidationError) as caught:
                    validate_strategy_spec_v1(document)
                self.assertEqual(caught.exception.code, "invalid_uri")
                self.assertEqual(caught.exception.pointer, "/provenance/sources/0/uri")

    def test_draft_can_preserve_unresolved_parameter_but_cannot_promote(self):
        draft = valid_strategy_spec(frozen=False)
        draft["parameters"]["fast_period"]["value"] = None
        draft["unresolved_items"] = [
            {
                "id": "missing-fast-period",
                "question": "Which fast period did the presenter intend?",
                "affects": ["/parameters/fast_period/value"],
                "severity": "non_blocking",
            }
        ]
        validated = validate_strategy_spec_v1(draft)
        self.assertFalse(validated.compilation_ready)
        self.assertFalse(validated.promotion_ready)
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(draft, require_promotable=True)
        self.assertEqual(caught.exception.code, "not_frozen")

    def test_schedule_overlap_fok_partial_and_risk_inversion_are_rejected(self):
        overlap = valid_strategy_spec()
        overlap["schedule"]["windows"] = [
            {"start_utc": "08:00", "end_utc": "12:00"},
            {"start_utc": "11:00", "end_utc": "13:00"},
        ]
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(overlap)
        self.assertEqual(caught.exception.code, "overlapping_schedule")

        unaligned_weekend = valid_strategy_spec()
        unaligned_weekend["schedule"]["close_before_weekend"] = {
            "enabled": True,
            "cutoff_utc": "23:30",
        }
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(unaligned_weekend)
        self.assertEqual(caught.exception.code, "invalid_schedule")

        disabled_with_cutoff = valid_strategy_spec()
        disabled_with_cutoff["schedule"]["close_before_weekend"]["cutoff_utc"] = "16:00"
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(disabled_with_cutoff)
        self.assertEqual(caught.exception.code, "invalid_schedule")

        partial = valid_strategy_spec()
        partial["execution"]["allow_partial_fill"] = True
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(partial)
        self.assertEqual(caught.exception.code, "invalid_execution_semantics")

        inverted = valid_strategy_spec()
        inverted["risk"]["min_stop_distance_price"] = "100"
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(inverted)
        self.assertEqual(caught.exception.code, "invalid_risk_policy")

    def test_parent_lineage_must_be_immediate(self):
        later = valid_strategy_spec()
        later["revision"] = 3
        later["provenance"]["parent"] = {
            "strategy_id": later["strategy_id"],
            "revision": 1,
        }
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(later)
        self.assertEqual(caught.exception.code, "invalid_parent")

    def test_position_history_cross_and_excess_decimal_precision_are_rejected(self):
        position_cross = valid_strategy_spec()
        position_cross["entry"]["long"] = {
            "op": "crosses_above",
            "args": [{"ref": "position.unrealized_r"}, {"decimal": "1"}],
        }
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(position_cross)
        self.assertEqual(caught.exception.code, "invalid_cross")

        huge = valid_strategy_spec()
        huge["parameters"]["stop_multiple"]["value"] = "1" * 100
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(huge)
        self.assertEqual(caught.exception.code, "decimal_too_precise")

    def test_feature_period_values_and_research_space_are_bounded(self):
        for invalid_period in (0, -1, 10_001):
            with self.subTest(value=invalid_period):
                document = valid_strategy_spec()
                parameter = document["parameters"]["fast_period"]
                parameter["value"] = invalid_period
                parameter["research_bounds"] = None
                with self.assertRaises(StrategySpecValidationError) as caught:
                    validate_strategy_spec_v1(document)
                self.assertEqual(caught.exception.code, "out_of_range")

        for bounds in (
            {"min": 0, "max": 4, "step": 1},
            {"min": 2, "max": 10_001, "step": 1},
        ):
            with self.subTest(bounds=bounds):
                document = valid_strategy_spec()
                document["parameters"]["fast_period"]["research_bounds"] = bounds
                with self.assertRaises(StrategySpecValidationError) as caught:
                    validate_strategy_spec_v1(document)
                self.assertEqual(caught.exception.code, "out_of_range")

        invalid_multiple_space = valid_strategy_spec()
        invalid_multiple_space["parameters"]["stop_multiple"]["research_bounds"] = {
            "min": "0",
            "max": "2",
            "step": "0.25",
        }
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(invalid_multiple_space)
        self.assertEqual(caught.exception.code, "out_of_range")

    def test_multi_bar_confirmation_rejects_event_and_current_position_rules(self):
        event_rule = valid_strategy_spec()
        event_rule["entry"]["confirmation_bars"] = 2
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(event_rule)
        self.assertEqual(caught.exception.code, "invalid_confirmation")

        unavailable_flat_state = valid_strategy_spec()
        unavailable_flat_state["entry"]["long"] = {
            "op": "gt",
            "args": [{"ref": "position.unrealized_r"}, {"decimal": "0"}],
        }
        unavailable_flat_state["entry"]["short"] = None
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(unavailable_flat_state)
        self.assertEqual(caught.exception.code, "invalid_reference")

        position_rule = valid_strategy_spec()
        position_rule["entry"]["long"] = {
            "op": "eq",
            "args": [{"ref": "position.side"}, {"enum": "flat"}],
        }
        position_rule["entry"]["short"] = None
        position_rule["entry"]["confirmation_bars"] = 2
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(position_rule)
        self.assertEqual(caught.exception.code, "invalid_confirmation")

    def test_video_evidence_covers_features_and_referenced_parameter_values(self):
        missing_features = valid_strategy_spec()
        missing_features["provenance"]["sources"][0]["evidence"][0]["supports"].remove(
            "/features"
        )
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(missing_features)
        self.assertEqual(caught.exception.code, "missing_evidence")
        self.assertEqual(caught.exception.pointer, "/features")

        missing_parameters = valid_strategy_spec()
        missing_parameters["provenance"]["sources"][0]["evidence"][0]["supports"].remove(
            "/parameters"
        )
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(missing_parameters)
        self.assertEqual(caught.exception.code, "missing_evidence")
        self.assertTrue(caught.exception.pointer.startswith("/parameters/"))

    def test_current_profile_rejects_unsupported_required_volume(self):
        document = valid_strategy_spec()
        document["market"]["required_bar_fields"].append("volume")
        with self.assertRaises(StrategySpecValidationError) as caught:
            validate_strategy_spec_v1(document)
        self.assertEqual(caught.exception.code, "invalid_market_scope")

    def test_validated_artifact_detects_hash_forgery(self):
        validated = validate_strategy_spec_v1(valid_strategy_spec())
        with self.assertRaisesRegex(ValueError, "spec_hash"):
            ValidatedStrategySpecV1(
                canonical_json=validated.canonical_json,
                spec_hash="0" * 64,
                warnings=(),
                compilation_ready=True,
                promotion_ready=True,
            )


if __name__ == "__main__":
    unittest.main()
