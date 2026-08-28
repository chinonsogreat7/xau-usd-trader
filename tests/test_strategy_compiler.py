import hashlib
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext
from pathlib import Path

from strategy_spec_fixture import valid_strategy_spec
import xau_trader.strategy_compiler as strategy_compiler_module
from xau_trader.domain import AvailabilityBasis, QuoteBar, StrategySpec
from xau_trader.strategy_compiler import (
    CompiledCondition,
    FEATURE_IMPLEMENTATION_HASH,
    FEATURE_MANIFEST_HASH,
    PositionContext,
    SignalAction,
    StrategyCompilationError,
    TruthValue,
    compile_strategy_spec_v1,
)
from xau_trader.strategy_schema import ValidatedStrategySpecV1, validate_strategy_spec_v1


def quote_bars(closes, gap_before=None, delayed_last=False, start=None):
    start = start or datetime(2026, 1, 5, tzinfo=timezone.utc)
    bars = []
    offset = timedelta(0)
    for index, close in enumerate(closes):
        if gap_before is not None and index == gap_before:
            offset += timedelta(hours=1)
        bar_start = start + timedelta(hours=index) + offset
        end = bar_start + timedelta(hours=1)
        available = end + timedelta(minutes=1) if delayed_last and index == len(closes) - 1 else end
        bars.append(
            QuoteBar(
                start_time=bar_start,
                timestamp=end,
                availability_basis=AvailabilityBasis.SYNTHETIC,
                available_at=available,
                bid_open=close - 0.1,
                bid_high=close + 0.4,
                bid_low=close - 0.6,
                bid_close=close - 0.1,
                ask_open=close + 0.1,
                ask_high=close + 0.6,
                ask_low=close - 0.4,
                ask_close=close + 0.1,
            )
        )
    return tuple(bars)


def compiled(document=None):
    return compile_strategy_spec_v1(
        validate_strategy_spec_v1(document or valid_strategy_spec(), require_promotable=True)
    )


def state_at(bars, **kwargs):
    return PositionContext(as_of=bars[-1].timestamp, **kwargs)


class StrategyCompilerTests(unittest.TestCase):
    def test_compiler_is_deterministic_hash_bound_and_data_only(self):
        first = compiled()
        second = compiled()

        self.assertEqual(first.plan_hash, second.plan_hash)
        self.assertEqual(first.canonical_plan_json, second.canonical_plan_json)
        self.assertEqual(first.feature_manifest_hash, FEATURE_MANIFEST_HASH)
        self.assertTrue(first.diagnostic_only)
        self.assertEqual([feature.feature_id for feature in first.features], ["atr", "fast", "slow"])
        self.assertEqual(len(first.plan_hash), 64)

    def test_feature_manifest_binds_the_compiler_source_artifact(self):
        source_hash = hashlib.sha256(
            Path(strategy_compiler_module.__file__).read_bytes()
        ).hexdigest()
        self.assertEqual(FEATURE_IMPLEMENTATION_HASH, source_hash)

    def test_compiler_rejects_raw_legacy_and_unfrozen_inputs(self):
        with self.assertRaisesRegex(StrategyCompilationError, "ValidatedStrategySpecV1"):
            compile_strategy_spec_v1(valid_strategy_spec())

        legacy = StrategySpec
        with self.assertRaises(StrategyCompilationError):
            compile_strategy_spec_v1(legacy)  # type: ignore[arg-type]

        draft = validate_strategy_spec_v1(valid_strategy_spec(frozen=False))
        with self.assertRaisesRegex(StrategyCompilationError, "not_frozen"):
            compile_strategy_spec_v1(draft)

        forged = ValidatedStrategySpecV1(
            canonical_json=draft.canonical_json,
            spec_hash=draft.spec_hash,
            warnings=(),
            compilation_ready=True,
            promotion_ready=True,
        )
        with self.assertRaisesRegex(StrategyCompilationError, "invalid_validated_artifact"):
            compile_strategy_spec_v1(forged)

    def test_plan_fields_cannot_diverge_from_hash_bound_payload(self):
        plan = compiled()
        with self.assertRaisesRegex(ValueError, "hash-bound plan"):
            replace(plan, cooldown_bars=999)

    def test_persisted_plan_must_match_the_current_evaluator_runtime(self):
        plan = compiled()
        payload = json.loads(plan.canonical_plan_json)
        payload["feature_manifest_hash"] = "0" * 64
        old_canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        old_hash = hashlib.sha256(old_canonical.encode("utf-8")).hexdigest()

        with self.assertRaisesRegex(ValueError, "current evaluator runtime"):
            replace(
                plan,
                feature_manifest_hash="0" * 64,
                canonical_plan_json=old_canonical,
                plan_hash=old_hash,
            )

        bypassed_constructor = compiled()
        object.__setattr__(bypassed_constructor, "feature_manifest_hash", "0" * 64)
        with self.assertRaisesRegex(ValueError, "current evaluator runtime"):
            bypassed_constructor.feature_snapshot(quote_bars((1.0, 2.0, 3.0)))

    def test_compiled_nodes_and_collections_are_deeply_immutable(self):
        plan = compiled()
        with self.assertRaisesRegex(ValueError, "immutable tuple"):
            CompiledCondition(plan.entry_long.op, list(plan.entry_long.args))
        with self.assertRaisesRegex(ValueError, "immutable tuple"):
            replace(plan, schedule_days=list(plan.schedule_days))

    def test_sma_cross_emits_long_only_after_warmup(self):
        plan = compiled()
        bars = quote_bars((3.0, 2.0, 1.0, 4.0))

        warmup = plan.evaluate(bars, index=1)
        signal = plan.evaluate(bars)

        self.assertEqual(warmup.action, SignalAction.NO_SIGNAL)
        self.assertIn("warm-up", warmup.reason)
        self.assertEqual(signal.action, SignalAction.ENTER_LONG)
        self.assertEqual(signal.long_state, TruthValue.TRUE)
        self.assertEqual(signal.short_state, TruthValue.FALSE)

    def test_feature_snapshot_uses_manifest_algorithms(self):
        document = valid_strategy_spec()
        document["features"].extend(
            [
                {
                    "id": "ema_test",
                    "kind": "ema",
                    "inputs": [{"ref": "bar.close"}],
                    "params": {"period": {"param": "fast_period"}},
                    "output_type": "decimal",
                    "warmup_bars": 2,
                    "availability": "bar_close",
                },
                {
                    "id": "rsi_test",
                    "kind": "rsi",
                    "inputs": [{"ref": "bar.close"}],
                    "params": {"period": {"param": "fast_period"}},
                    "output_type": "decimal",
                    "warmup_bars": 3,
                    "availability": "bar_close",
                },
                {
                    "id": "high_test",
                    "kind": "rolling_high",
                    "inputs": [{"ref": "bar.close"}],
                    "params": {"period": {"param": "fast_period"}},
                    "output_type": "decimal",
                    "warmup_bars": 2,
                    "availability": "bar_close",
                },
                {
                    "id": "low_test",
                    "kind": "rolling_low",
                    "inputs": [{"ref": "bar.close"}],
                    "params": {"period": {"param": "fast_period"}},
                    "output_type": "decimal",
                    "warmup_bars": 2,
                    "availability": "bar_close",
                },
            ]
        )
        snapshot = dict(compiled(document).feature_snapshot(quote_bars((1.0, 2.0, 3.0))))

        self.assertEqual(snapshot["fast"], Decimal("2.5"))
        self.assertEqual(snapshot["slow"], Decimal("2"))
        self.assertEqual(snapshot["atr"], Decimal("1.5"))
        self.assertEqual(snapshot["ema_test"], Decimal("2.5"))
        self.assertEqual(snapshot["rsi_test"], Decimal("100"))
        self.assertEqual(snapshot["high_test"], Decimal("3"))
        self.assertEqual(snapshot["low_test"], Decimal("2"))

    def test_feature_math_does_not_use_ambient_decimal_context(self):
        plan = compiled()
        previous_precision = getcontext().prec
        try:
            getcontext().prec = 3
            low_precision = dict(plan.feature_snapshot(quote_bars((1.0, 2.0, 3.0))))
            getcontext().prec = 50
            high_precision = dict(plan.feature_snapshot(quote_bars((1.0, 2.0, 3.0))))
        finally:
            getcontext().prec = previous_precision
        self.assertEqual(low_precision, high_precision)

    def test_future_bars_cannot_change_prior_decision_or_input_hash(self):
        plan = compiled()
        prefix = quote_bars((3.0, 2.0, 1.0, 4.0))
        extended = quote_bars((3.0, 2.0, 1.0, 4.0, 10_000.0, 1.0))

        before = plan.evaluate(prefix)
        after = plan.evaluate(extended, index=3)

        self.assertEqual(before.action, after.action)
        self.assertEqual(before.long_state, after.long_state)
        self.assertEqual(before.input_sha256, after.input_sha256)

        future_invalid = list(prefix) + [object()]
        unaffected = plan.evaluate(future_invalid, index=3)  # type: ignore[arg-type]
        self.assertEqual(before.action, unaffected.action)
        self.assertEqual(before.input_sha256, unaffected.input_sha256)

    def test_delayed_or_gapped_bar_cannot_create_a_cross(self):
        plan = compiled()
        delayed = plan.evaluate(quote_bars((3.0, 2.0, 1.0, 4.0), delayed_last=True))
        gapped = plan.evaluate(quote_bars((3.0, 2.0, 1.0, 4.0), gap_before=3))

        self.assertEqual(delayed.action, SignalAction.NO_SIGNAL)
        self.assertIn("unavailable", delayed.reason)
        self.assertEqual(gapped.action, SignalAction.NO_SIGNAL)
        self.assertNotEqual(gapped.long_state, TruthValue.TRUE)

    def test_schedule_and_cooldown_gate_new_entries(self):
        schedule = valid_strategy_spec()
        schedule["schedule"]["windows"] = [{"start_utc": "08:00", "end_utc": "09:00"}]
        schedule_result = compiled(schedule).evaluate(quote_bars((3.0, 2.0, 1.0, 4.0)))
        cooldown_bars = quote_bars((3.0, 2.0, 1.0, 4.0))
        cooldown_result = compiled().evaluate(
            cooldown_bars,
            state_at(cooldown_bars, bars_since_last_entry=1),
        )

        self.assertEqual(schedule_result.action, SignalAction.NO_SIGNAL)
        self.assertIn("schedule", schedule_result.reason)
        self.assertEqual(cooldown_result.action, SignalAction.NO_SIGNAL)
        self.assertIn("cooldown", cooldown_result.reason)

    def test_simultaneous_entries_conflict_and_exit_wins_while_positioned(self):
        conflict = valid_strategy_spec()
        always_true = {"op": "gt", "args": [{"ref": "bar.close"}, {"decimal": "0"}]}
        conflict["entry"]["long"] = always_true
        conflict["entry"]["short"] = always_true
        conflict_result = compiled(conflict).evaluate(quote_bars((1.0, 2.0, 3.0)))
        self.assertEqual(conflict_result.action, SignalAction.CONFLICT)

        exit_document = valid_strategy_spec()
        exit_document["exit"]["signal"]["long"] = always_true
        exit_bars = quote_bars((3.0, 2.0, 1.0, 4.0))
        exit_result = compiled(exit_document).evaluate(
            exit_bars,
            state_at(exit_bars, side="long", bars_in_position=2),
        )
        self.assertEqual(exit_result.action, SignalAction.EXIT_LONG)

    def test_delayed_bar_never_backdates_a_stale_close_action(self):
        delayed = quote_bars((3.0, 2.0, 1.0, 4.0), delayed_last=True)
        no_forced_close = compiled().evaluate(
            delayed,
            state_at(delayed, side="long", bars_in_position=999),
        )
        self.assertEqual(no_forced_close.action, SignalAction.NO_SIGNAL)
        self.assertIn("unavailable", no_forced_close.reason)

        stale_close = valid_strategy_spec()
        stale_close["risk"]["close_on_data_stale"] = True
        stale_plan = compiled(stale_close)
        unexecuted = stale_plan.evaluate(
            delayed,
            state_at(delayed, side="long", bars_in_position=999),
        )
        self.assertNotEqual(stale_plan.plan_hash, compiled().plan_hash)
        self.assertEqual(unexecuted.action, SignalAction.NO_SIGNAL)
        self.assertIn("unavailable", unexecuted.reason)

    def test_close_before_weekend_forces_position_exit(self):
        document = valid_strategy_spec()
        document["schedule"]["close_before_weekend"] = {
            "enabled": True,
            "cutoff_utc": "04:00",
        }
        friday = datetime(2026, 1, 9, tzinfo=timezone.utc)
        friday_bars = quote_bars((3.0, 2.0, 1.0, 4.0), start=friday)
        result = compiled(document).evaluate(
            friday_bars,
            state_at(friday_bars, side="long", bars_in_position=2),
        )
        self.assertEqual(result.action, SignalAction.EXIT_LONG)
        self.assertIn("weekend", result.reason)

    def test_unknown_never_triggers_entry(self):
        document = valid_strategy_spec()
        document["entry"]["long"] = {
            "op": "all",
            "args": [
                {"op": "eq", "args": [{"boolean": True}, {"boolean": True}]},
                {
                    "op": "crosses_above",
                    "args": [{"ref": "features.fast"}, {"ref": "features.slow"}],
                },
            ],
        }
        result = compiled(document).evaluate(quote_bars((1.0, 2.0, 3.0)))
        self.assertEqual(result.action, SignalAction.NO_SIGNAL)
        self.assertEqual(result.long_state, TruthValue.UNKNOWN)

    def test_context_state_is_complete_and_bound_to_the_decision_time(self):
        with self.assertRaisesRegex(ValueError, "bars_in_position"):
            PositionContext(side="long", as_of=datetime(2026, 1, 1, tzinfo=timezone.utc))
        with self.assertRaisesRegex(ValueError, "as_of"):
            PositionContext(side="long", bars_in_position=1)

        bars = quote_bars((3.0, 2.0, 1.0, 4.0, 5.0))
        future_context = state_at(bars, side="long", bars_in_position=2)
        with self.assertRaises(StrategyCompilationError) as caught:
            compiled().evaluate(bars, future_context, index=3)
        self.assertEqual(caught.exception.code, "context_time_mismatch")

    def test_position_dependent_rule_requires_an_as_of_timestamp(self):
        document = valid_strategy_spec()
        document["entry"]["long"] = {
            "op": "eq",
            "args": [{"ref": "position.side"}, {"enum": "flat"}],
        }
        document["entry"]["short"] = None
        plan = compiled(document)
        bars = quote_bars((1.0, 2.0, 3.0))

        with self.assertRaises(StrategyCompilationError) as caught:
            plan.evaluate(bars)
        self.assertEqual(caught.exception.code, "missing_context_time")
        self.assertEqual(
            plan.evaluate(bars, PositionContext(as_of=bars[-1].timestamp)).action,
            SignalAction.ENTER_LONG,
        )

    def test_position_exit_requires_unrealized_r_when_the_rule_uses_it(self):
        document = valid_strategy_spec()
        document["exit"]["signal"]["long"] = {
            "op": "gte",
            "args": [{"ref": "position.unrealized_r"}, {"decimal": "1"}],
        }
        plan = compiled(document)
        bars = quote_bars((1.0, 2.0, 3.0))

        with self.assertRaises(StrategyCompilationError) as caught:
            plan.evaluate(
                bars,
                state_at(bars, side="long", bars_in_position=2),
            )
        self.assertEqual(caught.exception.code, "missing_context_value")

    def test_gap_resets_required_feature_readiness_before_entry(self):
        document = valid_strategy_spec()
        document["entry"]["long"] = {
            "op": "gt",
            "args": [{"ref": "bar.close"}, {"decimal": "0"}],
        }
        document["entry"]["short"] = None
        document["entry"]["confirmation_bars"] = 2
        result = compiled(document).evaluate(
            quote_bars((1.0, 2.0, 3.0, 4.0), gap_before=3)
        )

        self.assertEqual(result.action, SignalAction.NO_SIGNAL)
        self.assertIn("warm-up", result.reason)

    def test_declared_warmup_requires_a_full_contiguous_suffix_after_gap(self):
        document = valid_strategy_spec()
        for feature in document["features"]:
            feature["warmup_bars"] = 10
        document["entry"]["long"] = {
            "op": "gt",
            "args": [{"ref": "bar.close"}, {"decimal": "0"}],
        }
        document["entry"]["short"] = None
        bars = quote_bars(tuple(float(index + 1) for index in range(14)), gap_before=10)

        result = compiled(document).evaluate(bars)

        self.assertEqual(result.action, SignalAction.NO_SIGNAL)
        self.assertIn("warm-up", result.reason)


if __name__ == "__main__":
    unittest.main()
