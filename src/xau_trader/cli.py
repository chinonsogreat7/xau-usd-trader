"""Command-line entry points for validation and deterministic research demos."""

import argparse
import json
import os
from pathlib import Path
import tempfile
from typing import Optional, Sequence

from .backtest import BacktestConfig, Backtester
from .data import (
    analyze_cadence,
    generate_demo_bars,
    generate_demo_m15_bars,
    read_quote_bars_csv,
    sha256_file,
    write_quote_bars_csv,
)
from .dataset_manifest import (
    AcquisitionBasis,
    BarConvention,
    BarTimeframe,
    DatasetCoverage,
    DatasetHashes,
    DatasetInstrument,
    DatasetManifest,
    DatasetProvider,
    DatasetRights,
    DatasetSource,
    IntervalConvention,
    SCHEMA_NAME as DATASET_MANIFEST_SCHEMA,
    SCHEMA_VERSION as DATASET_MANIFEST_VERSION,
    SessionCalendar,
    TimestampConvention,
    load_dataset_manifest,
    validate_dataset_binding,
)
from .decision_trace import write_diagnostic_trace_artifacts
from .diagnostic_replay import (
    DIAGNOSTIC_REPLAY_ENGINE_VERSION,
    DIAGNOSTIC_REPLAY_SCHEMA_VERSION,
    run_diagnostic_replay,
)
from .domain import QuoteBar
from .research_baseline import provisional_diagnostic_baseline_v1
from .risk import RiskEngine, RiskPolicy
from .strategy_compiler import compile_strategy_spec_v1
from .strategy_schema import parse_strategy_spec_json, validate_strategy_spec_v1
from .strategies import SmaCrossStrategy


def _add_backtest_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fast-window", type=int, default=12)
    parser.add_argument("--slow-window", type=int, default=48)
    parser.add_argument("--long-only", action="store_true")
    parser.add_argument("--initial-cash", type=float, default=10_000.0)
    parser.add_argument("--units", type=float, default=1.0)
    parser.add_argument("--slippage-bps", type=float, default=0.5)
    parser.add_argument("--commission-per-unit", type=float, default=0.0)
    parser.add_argument("--financing-per-unit-per-bar", type=float, default=0.0)
    parser.add_argument("--max-spread-bps", type=float, default=25.0)
    parser.add_argument("--max-units", type=float, default=1.0)
    parser.add_argument("--max-drawdown", type=float, default=0.05)
    parser.add_argument("--max-daily-loss", type=float, default=0.02)
    parser.add_argument("--expected-minutes", type=float, default=60.0)
    parser.add_argument(
        "--no-risk-gate",
        action="store_true",
        help="Disable the research risk gate; never use this for paper execution.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xau-trader",
        description="Paper-only XAU/USD strategy research scaffold",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="run the baseline on deterministic fake data")
    demo.add_argument("--bars", type=int, default=480)
    _add_backtest_options(demo)

    backtest = subparsers.add_parser("backtest", help="run the baseline on a bid/ask CSV")
    backtest.add_argument("csv", type=Path)
    _add_backtest_options(backtest)

    validate = subparsers.add_parser("validate-data", help="validate a bid/ask CSV")
    validate.add_argument("csv", type=Path)
    validate.add_argument("--expected-minutes", type=float, default=60.0)

    export = subparsers.add_parser("export-demo-data", help="write deterministic fake CSV data")
    export.add_argument("destination", type=Path)
    export.add_argument("--bars", type=int, default=480)

    replay_demo = subparsers.add_parser(
        "export-demo-replay-data",
        help="write a deterministic synthetic M15 CSV and its strict manifest",
    )
    replay_demo.add_argument("destination", type=Path)
    replay_demo.add_argument("--manifest", type=Path, required=True)
    replay_demo.add_argument("--bars", type=int, default=480)
    replay_demo.add_argument(
        "--overwrite",
        action="store_true",
        help="replace both synthetic fixture outputs if they already exist",
    )

    replay = subparsers.add_parser(
        "replay-diagnostic",
        help="replay the provisional policy into paper-only decision traces",
    )
    replay.add_argument("csv", type=Path)
    replay.add_argument("--manifest", type=Path, required=True)
    replay.add_argument(
        "--raw-data",
        type=Path,
        help=(
            "exact pre-normalization source artifact; defaults to the M15 CSV "
            "when that file itself is the acquired artifact"
        ),
    )
    replay.add_argument("--trace-json", type=Path, required=True)
    replay.add_argument("--decisions-csv", type=Path, required=True)
    replay.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing trace outputs",
    )

    validate_strategy = subparsers.add_parser(
        "validate-strategy",
        help="strictly validate and hash a StrategySpec v1 JSON document",
    )
    validate_strategy.add_argument("json", type=Path)
    validate_strategy.add_argument(
        "--promotable",
        action="store_true",
        help="also require frozen, fully resolved promotion-ready content",
    )

    compile_strategy = subparsers.add_parser(
        "compile-strategy",
        help="compile a frozen StrategySpec into a diagnostic-only data plan",
    )
    compile_strategy.add_argument("json", type=Path)
    return parser


def _run_backtest(args: argparse.Namespace, bars: Sequence[QuoteBar]) -> dict:
    strategy = SmaCrossStrategy(
        fast_window=args.fast_window,
        slow_window=args.slow_window,
        long_only=args.long_only,
    )
    config = BacktestConfig(
        initial_cash=args.initial_cash,
        units=args.units,
        slippage_bps=args.slippage_bps,
        commission_per_unit=args.commission_per_unit,
        financing_per_unit_per_bar=args.financing_per_unit_per_bar,
    )
    risk_engine = None
    if not args.no_risk_gate:
        risk_engine = RiskEngine(
            RiskPolicy(
                max_drawdown_fraction=args.max_drawdown,
                max_daily_loss_fraction=args.max_daily_loss,
                max_spread_bps=args.max_spread_bps,
                max_units=args.max_units,
            )
        )
    result = Backtester(config=config, risk_engine=risk_engine).run(bars, strategy)
    summary = result.summary()
    summary["cadence"] = analyze_cadence(bars, args.expected_minutes)
    return summary


def _resolved(path: Path) -> Path:
    """Resolve a CLI path without requiring the target to exist."""

    return path.expanduser().resolve(strict=False)


def _require_distinct_outputs(
    outputs: Sequence[Path],
    protected_inputs: Sequence[Path],
) -> None:
    resolved_outputs = tuple(_resolved(path) for path in outputs)
    if len(set(resolved_outputs)) != len(resolved_outputs):
        raise ValueError("output paths must be different")
    protected = {_resolved(path) for path in protected_inputs}
    overlap = tuple(path for path in resolved_outputs if path in protected)
    if overlap:
        raise ValueError("an output path cannot replace an input or manifest")


def _staging_path(target: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=".{}-".format(target.name),
        suffix=".tmp",
        dir=str(target.parent),
    )
    os.close(descriptor)
    return Path(name)


def _discard_staging(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _write_synthetic_fixture_pair(
    csv_target: Path,
    manifest_target: Path,
    bars: Sequence[QuoteBar],
    *,
    overwrite: bool,
) -> tuple:
    """Stage both fixture artifacts before committing either destination."""

    csv_stage = _staging_path(csv_target)
    manifest_stage = None
    created = []
    try:
        write_quote_bars_csv(csv_stage, bars)
        csv_sha256 = sha256_file(csv_stage)
        manifest = _synthetic_m15_manifest(bars, csv_sha256)
        manifest_stage = _staging_path(manifest_target)
        manifest_stage.write_text(manifest.canonical_json + "\n", encoding="utf-8")
        if overwrite:
            os.replace(str(csv_stage), str(csv_target))
            os.replace(str(manifest_stage), str(manifest_target))
        else:
            os.link(str(csv_stage), str(csv_target))
            created.append(csv_target)
            os.link(str(manifest_stage), str(manifest_target))
            created.append(manifest_target)
        return manifest, csv_sha256
    except BaseException:
        if not overwrite:
            for target in reversed(created):
                _discard_staging(target)
        raise
    finally:
        _discard_staging(csv_stage)
        _discard_staging(manifest_stage)


def _synthetic_m15_manifest(
    bars: Sequence[QuoteBar],
    csv_sha256: str,
) -> DatasetManifest:
    """Describe the local deterministic fixture without vendor-like claims."""

    return DatasetManifest(
        schema=DATASET_MANIFEST_SCHEMA,
        version=DATASET_MANIFEST_VERSION,
        provider=DatasetProvider(
            name="xau-trader deterministic fixture generator",
            legal_entity="Local user-operated research workspace; no external provider",
        ),
        instrument=DatasetInstrument(
            symbol="XAU_USD",
            product_form="Synthetic bid/ask quote-bar fixture; not a tradable product",
        ),
        source=DatasetSource(
            route="xau-trader export-demo-replay-data",
            account_environment="Offline synthetic fixture with no brokerage account",
            acquisition_basis=AcquisitionBasis.SYNTHETIC_GENERATION,
            request_parameters=(
                ("bar_count", str(len(bars))),
                ("generator_version", "generate_demo_m15_bars_v1"),
            ),
            time_normalization_rule=(
                "Generator emits native UTC quarter-hour starts and completed-bar ends"
            ),
            retrieved_at=bars[-1].timestamp,
        ),
        bars=BarConvention(
            timeframe=BarTimeframe.M15,
            duration_seconds=15 * 60,
            timezone="UTC",
            interval=IntervalConvention.HALF_OPEN,
            timestamp=TimestampConvention.COMPLETED_BAR_END,
            availability_basis=bars[0].availability_basis,
        ),
        hashes=DatasetHashes(
            raw_sha256=csv_sha256,
            normalized_csv_sha256=csv_sha256,
        ),
        rights=DatasetRights(
            basis="Locally generated deterministic synthetic research fixture",
            api_data_agreement_version="Project-owned synthetic fixture policy v1",
            retention_basis="Project-local synthetic test artifact",
        ),
        session_calendar=SessionCalendar(
            calendar_id="continuous-synthetic-utc",
            version="fixture-v1",
        ),
        coverage=DatasetCoverage(
            start=bars[0].start_time,
            end=bars[-1].timestamp,
            row_count=len(bars),
            known_gaps=(),
        ),
    )


def _decision_counts(decisions: Sequence[object]) -> dict:
    counts = {"buy": 0, "sell": 0, "no_trade": 0}
    for decision in decisions:
        counts[decision.action.value] += 1
    return counts


def _replay_summary(result, binding, manifest: DatasetManifest, outputs: Sequence[Path]) -> dict:
    all_action_counts = _decision_counts(result.decisions)
    post_pre_roll_action_counts = _decision_counts(result.post_pre_roll_decisions)
    counts = {
        "m15_bars": len(result.m15_bars),
        "h1_bars": len(result.h1_bars),
        "atr_events": len(result.h1_atr),
        "pivot_events": len(result.h1_pivots),
        "regime_events": len(result.h1_regimes),
        "impulse_events": len(result.h1_impulses),
        "zones": len(result.zones),
        "ema_events": len(result.m15_ema),
        "candle_pattern_events": len(result.m15_candles),
        "confirmation_events": len(result.m15_confirmations),
        "lifecycle_events": len(result.lifecycle.events),
        "lifecycle_transitions": len(result.lifecycle.transitions),
        "decisions": len(result.decisions),
        "pre_roll_decisions": result.readiness.pre_roll_decision_count,
        "post_pre_roll_decisions": result.readiness.post_pre_roll_decision_count,
        "all_buy": all_action_counts["buy"],
        "all_sell": all_action_counts["sell"],
        "all_no_trade": all_action_counts["no_trade"],
        "post_pre_roll_buy": post_pre_roll_action_counts["buy"],
        "post_pre_roll_sell": post_pre_roll_action_counts["sell"],
        "post_pre_roll_no_trade": post_pre_roll_action_counts["no_trade"],
    }
    return {
        "status": "diagnostic_replay_complete",
        "diagnostic_only": True,
        "execution": None,
        "dataset": {
            "manifest_identity": binding.manifest_identity,
            "manifest_fingerprint": binding.manifest_fingerprint,
            "raw_sha256": manifest.hashes.raw_sha256,
            "normalized_csv_sha256": manifest.hashes.normalized_csv_sha256,
        },
        "replay": {
            "fingerprint": result.fingerprint,
            "evidence_fingerprint": result.evidence_fingerprint,
            "engine_version": DIAGNOSTIC_REPLAY_ENGINE_VERSION,
            "schema_version": DIAGNOSTIC_REPLAY_SCHEMA_VERSION,
            "input_bars_fingerprint": result.input_bars_fingerprint,
            "policy_bundle_fingerprint": result.policy_bundle_fingerprint,
            "readiness": {
                "history_boundary": result.readiness.history_boundary.value,
                "pre_roll_m15_bars": result.readiness.pre_roll_m15_bars,
                "minimum_input_m15_bars": result.readiness.minimum_input_m15_bars,
                "post_pre_roll_m15_start": (
                    result.readiness.post_pre_roll_m15_start.isoformat().replace(
                        "+00:00", "Z"
                    )
                ),
            },
        },
        "counts": counts,
        "outputs": {
            "trace_json": {
                "path": str(outputs[0]),
                "sha256": sha256_file(outputs[0]),
            },
            "decisions_csv": {
                "path": str(outputs[1]),
                "sha256": sha256_file(outputs[1]),
            },
        },
        "warnings": [
            "The provisional policy is unapproved and these are research candidates, not trades.",
            "Only post_pre_roll_* action counts are dataset-relative evaluated outcomes.",
            "Lifecycle state is explicitly empty at the dataset start; older zones are unknown.",
            "No order, fill, position, stop, target, account, P&L, or profitability result exists.",
        ],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            summary = _run_backtest(args, generate_demo_bars(args.bars))
            summary["data"] = "deterministic synthetic demonstration data"
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 0

        if args.command == "backtest":
            bars = read_quote_bars_csv(args.csv)
            summary = _run_backtest(args, bars)
            summary["data_sha256"] = sha256_file(args.csv)
            summary["bars"] = len(bars)
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 0

        if args.command == "validate-data":
            bars = read_quote_bars_csv(args.csv)
            report = {
                "path": str(args.csv),
                "sha256": sha256_file(args.csv),
                "bars": len(bars),
                "first_start_time": bars[0].start_time.isoformat(),
                "last_end_time": bars[-1].timestamp.isoformat(),
                "status": "structurally_valid",
                "cadence": analyze_cadence(bars, args.expected_minutes),
                "warning": "Cadence gaps require a versioned broker session calendar.",
            }
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0

        if args.command == "export-demo-data":
            bars = generate_demo_bars(args.bars)
            write_quote_bars_csv(args.destination, bars)
            print(
                json.dumps(
                    {
                        "path": str(args.destination),
                        "bars": len(bars),
                        "sha256": sha256_file(args.destination),
                        "warning": "This file is synthetic and cannot validate a strategy.",
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        if args.command == "export-demo-replay-data":
            _require_distinct_outputs(
                (args.destination, args.manifest),
                (),
            )
            for target in (args.destination, args.manifest):
                if target.exists() and target.is_dir():
                    raise IsADirectoryError("output target is a directory: {}".format(target))
                if target.exists() and not args.overwrite:
                    raise FileExistsError("output already exists: {}".format(target))
            bars = generate_demo_m15_bars(args.bars)
            args.destination.parent.mkdir(parents=True, exist_ok=True)
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest, csv_sha256 = _write_synthetic_fixture_pair(
                args.destination,
                args.manifest,
                bars,
                overwrite=args.overwrite,
            )
            print(
                json.dumps(
                    {
                        "status": "synthetic_replay_fixture_exported",
                        "diagnostic_only": True,
                        "bars": len(bars),
                        "csv": {
                            "path": str(args.destination),
                            "sha256": csv_sha256,
                        },
                        "manifest": {
                            "path": str(args.manifest),
                            "identity": manifest.identity,
                            "fingerprint": manifest.fingerprint,
                        },
                        "warning": (
                            "Synthetic data proves only that the replay plumbing runs."
                        ),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        if args.command == "replay-diagnostic":
            raw_path = args.csv if args.raw_data is None else args.raw_data
            _require_distinct_outputs(
                (args.trace_json, args.decisions_csv),
                (args.csv, args.manifest, raw_path),
            )

            manifest_bytes_before = sha256_file(args.manifest)
            normalized_before = sha256_file(args.csv)
            raw_before = (
                normalized_before
                if _resolved(raw_path) == _resolved(args.csv)
                else sha256_file(raw_path)
            )
            manifest = load_dataset_manifest(args.manifest)
            bars = read_quote_bars_csv(args.csv)
            manifest_bytes_after = sha256_file(args.manifest)
            normalized_after = sha256_file(args.csv)
            raw_after = (
                normalized_after
                if _resolved(raw_path) == _resolved(args.csv)
                else sha256_file(raw_path)
            )
            if manifest_bytes_before != manifest_bytes_after:
                raise ValueError("manifest changed while it was being read")
            if normalized_before != normalized_after:
                raise ValueError("normalized CSV changed while it was being read")
            if raw_before != raw_after:
                raise ValueError("raw data changed while inputs were being read")

            binding = validate_dataset_binding(
                manifest,
                bars,
                raw_sha256=raw_before,
                normalized_csv_sha256=normalized_before,
            )
            if binding.known_gap_count:
                raise ValueError(
                    "diagnostic replay does not yet support datasets with declared gaps"
                )
            bundle = provisional_diagnostic_baseline_v1()
            result = run_diagnostic_replay(
                bars,
                dataset_fingerprint=binding.manifest_fingerprint,
                policy_bundle=bundle,
            )
            args.trace_json.parent.mkdir(parents=True, exist_ok=True)
            args.decisions_csv.parent.mkdir(parents=True, exist_ok=True)
            outputs = write_diagnostic_trace_artifacts(
                result,
                args.trace_json,
                args.decisions_csv,
                overwrite=args.overwrite,
            )
            print(
                json.dumps(
                    _replay_summary(result, binding, manifest, outputs),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        if args.command == "validate-strategy":
            raw = args.json.read_text(encoding="utf-8")
            document = parse_strategy_spec_json(raw)
            validated = validate_strategy_spec_v1(
                document,
                require_promotable=args.promotable,
            )
            print(
                json.dumps(
                    {
                        "path": str(args.json),
                        "spec_hash": validated.spec_hash,
                        "compilation_ready": validated.compilation_ready,
                        "promotion_ready": validated.promotion_ready,
                        "warnings": list(validated.warnings),
                        "status": "valid_strategy_spec_v1",
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        if args.command == "compile-strategy":
            raw = args.json.read_text(encoding="utf-8")
            document = parse_strategy_spec_json(raw)
            validated = validate_strategy_spec_v1(document, require_promotable=True)
            plan = compile_strategy_spec_v1(validated)
            print(
                json.dumps(
                    {
                        "path": str(args.json),
                        "strategy_id": plan.strategy_id,
                        "revision": plan.revision,
                        "spec_hash": plan.spec_hash,
                        "plan_hash": plan.plan_hash,
                        "compiler_version": plan.compiler_version,
                        "feature_manifest_hash": plan.feature_manifest_hash,
                        "profile_id": plan.profile_id,
                        "diagnostic_only": plan.diagnostic_only,
                        "warning": (
                            "This plan is not connected to broker execution or the P&L backtester."
                        ),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 2
