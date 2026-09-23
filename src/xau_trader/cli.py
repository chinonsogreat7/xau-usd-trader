"""Command-line entry points for validation and deterministic research demos."""

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
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
    validate_calendar_binding,
    validate_dataset_binding,
)
from .decision_trace import write_diagnostic_trace_artifacts
from .diagnostic_replay import (
    run_diagnostic_replay,
)
from .domain import QuoteBar
from .mt5_import import import_mt5_tick_file
from .mt5_session_import import import_mt5_session_tick_file
from .local_demo import run_local_paper_demo
from .paper_execution import PaperLossLimits
from .paper_execution_io import (
    load_paper_scenario, parse_paper_scenario, run_paper_scenario,
    synthetic_execution_scenario, write_new_json,
)
from .research_baseline import (
    provisional_diagnostic_baseline_v1, provisional_session_diagnostic_baseline_v1,
)
from .replay_preflight import inspect_replay_bars
from .risk import RiskEngine, RiskPolicy
from .session_calendar import load_session_calendar
from .session_features import run_session_feature_replay
from .session_timing import session_semantics
from .strategy_compiler import compile_strategy_spec_v1
from .strategy_schema import parse_strategy_spec_json, validate_strategy_spec_v1
from .strategies import SmaCrossStrategy
from .structural_exits import StructuralExitPolicy


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


def _finite_decimal_argument(value: str) -> Decimal:
    try:
        converted = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("must be a finite decimal") from error
    if not converted.is_finite():
        raise argparse.ArgumentTypeError("must be a finite decimal")
    return converted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xau-trader",
        description="Paper-only XAU/USD strategy research scaffold",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    local_demo = subparsers.add_parser(
        "run-paper-demo", help="run a readable offline demo and save a new report folder automatically",
    )
    local_demo.add_argument("--output-root", type=Path, default=Path("reports"))

    paper_demo = subparsers.add_parser(
        "demo-paper-execution", help="simulate scripted synthetic bracket trades; no broker connection",
    )
    paper_demo.add_argument("--report-json", type=Path, required=True)
    paper_export = subparsers.add_parser(
        "export-paper-scenario", help="export the editable synthetic bracket-test scenario",
    )
    paper_export.add_argument("destination", type=Path)
    paper_export.add_argument("--max-losses-per-day", type=int)
    paper_export.add_argument("--max-weekly-drawdown-fraction", type=_finite_decimal_argument)
    paper_run = subparsers.add_parser(
        "simulate-paper", help="simulate a strict synthetic quote/intent scenario; no broker connection",
    )
    paper_run.add_argument("scenario", type=Path)
    paper_run.add_argument("--report-json", type=Path, required=True)

    strategy_paper = subparsers.add_parser(
        "replay-strategy-paper",
        help="bind provisional strategy signals to synthetic quotes; strict manifest v1 only, no broker",
    )
    strategy_paper.add_argument("csv", type=Path)
    strategy_paper.add_argument("--manifest", type=Path, required=True)
    strategy_paper.add_argument("--quote-scenario", type=Path, required=True)
    strategy_paper.add_argument("--raw-data", type=Path)
    strategy_paper.add_argument("--m15-left-wing", type=int, required=True)
    strategy_paper.add_argument("--m15-right-wing", type=int, required=True)
    strategy_paper.add_argument("--stop-atr-multiple", type=_finite_decimal_argument, required=True)
    strategy_paper.add_argument("--report-json", type=Path, required=True)

    demo = subparsers.add_parser("demo", help="run the baseline on deterministic fake data")
    demo.add_argument("--bars", type=int, default=480)
    _add_backtest_options(demo)

    backtest = subparsers.add_parser("backtest", help="run the baseline on a bid/ask CSV")
    backtest.add_argument("csv", type=Path)
    _add_backtest_options(backtest)

    validate = subparsers.add_parser("validate-data", help="validate a bid/ask CSV")
    validate.add_argument("csv", type=Path)
    validate.add_argument("--expected-minutes", type=float, default=60.0)

    preflight = subparsers.add_parser(
        "check-replay-data",
        help="inspect M15 replay compatibility and input provenance without running a replay",
    )
    preflight.add_argument("csv", type=Path)
    preflight.add_argument("--manifest", type=Path)
    preflight.add_argument(
        "--raw-data", type=Path,
        help="exact source artifact before normalization; requires --manifest",
    )

    tick_import = subparsers.add_parser(
        "import-mt5-ticks", help="normalize a local complete-quote MT5 tick export; no trading",
    )
    tick_import.add_argument("raw", type=Path)
    tick_import.add_argument("--plan", type=Path, required=True)
    tick_import.add_argument("--calendar", type=Path, required=True)
    tick_import.add_argument("--output-csv", type=Path, required=True)
    tick_import.add_argument("--manifest", type=Path, required=True)
    tick_import.add_argument("--report-json", type=Path, required=True)

    session_tick_import = subparsers.add_parser(
        "import-mt5-session-ticks",
        help="preserve MT5 ticks with partial-session diagnostics; no strategy or trading",
    )
    session_tick_import.add_argument("raw", type=Path)
    session_tick_import.add_argument("--plan", type=Path, required=True)
    session_tick_import.add_argument("--calendar", type=Path, required=True)
    session_tick_import.add_argument("--output-json", type=Path, required=True)

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

    session_replay = subparsers.add_parser(
        "replay-session-features",
        help="replay calendar-bound H1/ATR/pivot/EMA features only; no trade signals",
    )
    session_replay.add_argument("csv", type=Path)
    session_replay.add_argument("--manifest", type=Path, required=True)
    session_replay.add_argument("--calendar", type=Path, required=True)
    session_replay.add_argument("--raw-data", type=Path)
    session_replay.add_argument("--trace-json", type=Path, required=True)

    session_strategy = subparsers.add_parser(
        "replay-session-diagnostic",
        help="replay the provisional calendar-bound strategy; research candidates only, no orders",
    )
    session_strategy.add_argument("csv", type=Path)
    session_strategy.add_argument("--manifest", type=Path, required=True)
    session_strategy.add_argument("--calendar", type=Path, required=True)
    session_strategy.add_argument("--raw-data", type=Path)
    session_strategy.add_argument("--trace-json", type=Path, required=True)
    session_strategy.add_argument("--decisions-csv", type=Path, required=True)

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


def _load_replay_snapshot(
    csv_path: Path,
    manifest_path: Optional[Path] = None,
    raw_path: Optional[Path] = None,
) -> tuple:
    """Read the same input snapshot for preflight and replay, checking for changes."""

    raw_path = csv_path if raw_path is None else raw_path
    same_raw = _resolved(raw_path) == _resolved(csv_path)
    manifest_before = None if manifest_path is None else sha256_file(manifest_path)
    normalized_before = sha256_file(csv_path)
    raw_before = normalized_before if same_raw else sha256_file(raw_path)
    manifest = None if manifest_path is None else load_dataset_manifest(manifest_path)
    bars = read_quote_bars_csv(csv_path)
    manifest_after = None if manifest_path is None else sha256_file(manifest_path)
    normalized_after = sha256_file(csv_path)
    raw_after = (
        normalized_after
        if _resolved(raw_path) == _resolved(csv_path)
        else sha256_file(raw_path)
    )
    if manifest_before != manifest_after:
        raise ValueError("manifest changed while it was being read")
    if normalized_before != normalized_after:
        raise ValueError("normalized CSV changed while it was being read")
    if raw_before != raw_after:
        raise ValueError("raw data changed while inputs were being read")
    return bars, manifest, normalized_before, raw_before


def _check_replay_data(args: argparse.Namespace) -> dict:
    if args.raw_data is not None and args.manifest is None:
        raise ValueError("--raw-data requires --manifest")
    bars, manifest, csv_hash, raw_hash = _load_replay_snapshot(
        args.csv, args.manifest, args.raw_data,
    )
    technical = inspect_replay_bars(bars, provisional_diagnostic_baseline_v1())
    provenance = {"status": "missing_manifest"}
    inputs_ready = False
    if manifest is not None:
        if manifest.version != 1:
            raise ValueError(
                "check-replay-data assesses strict v1 strategy inputs; "
                "use replay-session-features or replay-session-diagnostic with --calendar for manifest v2"
            )
        binding = validate_dataset_binding(
            manifest, bars, raw_sha256=raw_hash, normalized_csv_sha256=csv_hash,
        )
        provenance = {
            "status": "binding_valid",
            "manifest_identity": binding.manifest_identity,
            "manifest_fingerprint": binding.manifest_fingerprint,
            "raw_sha256": raw_hash,
            "known_gap_count": binding.known_gap_count,
            "rights_claims_independently_verified": False,
        }
        inputs_ready = technical["compatible"] and binding.known_gap_count == 0
    return {
        "status": "replay_inputs_ready" if inputs_ready else "replay_inputs_blocked",
        "ready_for_diagnostic_replay": inputs_ready,
        "diagnostic_only": True,
        "csv": {"path": str(args.csv), "sha256": csv_hash},
        "technical": technical,
        "provenance": provenance,
        "notes": [
            "This check inspects inputs; it does not run the strategy or calculate returns.",
            "Manifest binding checks recorded claims and hashes, not permission to use the data.",
            "Segment counts do not remove gaps, create candles, or approve a shortened history.",
        ],
    }


def _load_session_snapshot(args: argparse.Namespace) -> tuple:
    inputs = (args.csv, args.manifest, args.calendar, args.raw_data or args.csv)
    # Enclose all four input reads in a common before/after hash check. Calendar
    # URIs in the manifest are metadata only: never fetch them over the network.
    before = tuple(sha256_file(path) for path in inputs)
    bars, manifest, csv_hash, raw_hash = _load_replay_snapshot(
        args.csv, args.manifest, args.raw_data,
    )
    calendar = load_session_calendar(args.calendar)
    after = tuple(sha256_file(path) for path in inputs)
    if before != after or csv_hash != before[0] or raw_hash != before[3]:
        raise ValueError("session replay inputs changed while they were being read")
    binding = validate_dataset_binding(
        manifest, bars, raw_sha256=raw_hash, normalized_csv_sha256=csv_hash,
    )
    calendar_binding = validate_calendar_binding(
        manifest, calendar, artifact_sha256=before[2],
    )
    return bars, manifest, calendar, binding, calendar_binding


def _replay_strategy_paper(args: argparse.Namespace) -> dict:
    """Publish a fully bound, synthetic-only strategy/execution experiment."""
    from .strategy_paper import run_strategy_paper_replay

    raw_path = args.raw_data or args.csv
    inputs = (args.csv, args.manifest, raw_path, args.quote_scenario)
    _require_distinct_outputs((args.report_json,), inputs)
    if args.report_json.exists() or args.report_json.is_symlink():
        raise FileExistsError("output already exists: {}".format(args.report_json))
    structural_policy = StructuralExitPolicy(
        m15_left_wing=args.m15_left_wing,
        m15_right_wing=args.m15_right_wing,
        stop_atr_multiple=args.stop_atr_multiple,
    )
    before = tuple(sha256_file(path) for path in inputs)
    bars, manifest, csv_hash, raw_hash = _load_replay_snapshot(
        args.csv, args.manifest, raw_path,
    )
    scenario = load_paper_scenario(args.quote_scenario)
    after = tuple(sha256_file(path) for path in inputs)
    if (before != after or csv_hash != before[0] or raw_hash != before[2]
            or scenario.input_sha256 != before[3]):
        raise ValueError("strategy paper inputs changed while they were being read")
    if manifest.version != 1:
        raise ValueError(
            "replay-strategy-paper supports strict continuous synthetic manifest v1 only; "
            "calendar-bound manifest v2 execution is not supported"
        )
    if manifest.source.acquisition_basis != AcquisitionBasis.SYNTHETIC_GENERATION:
        raise ValueError("replay-strategy-paper requires a synthetic_generation manifest")
    if manifest.instrument.symbol != scenario.instrument.symbol:
        raise ValueError("manifest symbol must match the paper instrument")
    if scenario.intents:
        raise ValueError(
            "quote scenario intents must be empty; this command derives all intents from the strategy"
        )
    binding = validate_dataset_binding(
        manifest, bars, raw_sha256=raw_hash, normalized_csv_sha256=csv_hash,
    )
    if binding.known_gap_count:
        raise ValueError("strategy paper replay does not support declared dataset gaps")
    replay = run_diagnostic_replay(
        bars, dataset_fingerprint=binding.manifest_fingerprint,
        policy_bundle=provisional_diagnostic_baseline_v1(),
    )
    result = run_strategy_paper_replay(
        replay, quotes=scenario.quotes, instrument=scenario.instrument,
        execution_policy=scenario.policy, structural_policy=structural_policy,
        loss_limits=scenario.loss_limits,
    ).as_dict()
    report = {
        "schema": "xau-strategy-paper-replay-report",
        "version": 1,
        "status": result["status"],
        "data_basis": "synthetic",
        "broker_connected": False,
        "real_orders_submitted": 0,
        "promotion_eligible": False,
        "hypothesis_unapproved": result["hypothesis_unapproved"],
        "source_risk_policy_complete": result["source_risk_policy_complete"],
        "loss_limits_configured": result["loss_limits_configured"],
        "loss_limits": result["loss_limits"],
        "dataset": {
            "manifest_identity": binding.manifest_identity,
            "manifest_fingerprint": binding.manifest_fingerprint,
            "manifest_input_sha256": before[1],
            "manifest": json.loads(manifest.canonical_json),
            "raw_sha256": raw_hash,
            "normalized_csv_sha256": csv_hash,
            "row_count": binding.row_count,
            "known_gap_count": binding.known_gap_count,
            "rights_claims_independently_verified": False,
        },
        "quote_scenario": {
            "name": scenario.name,
            "input_sha256": scenario.input_sha256,
            "canonical_sha256": hashlib.sha256(scenario.canonical_json.encode("utf-8")).hexdigest(),
            "inputs": json.loads(scenario.canonical_json),
            "hand_scripted_intents_accepted": False,
        },
        "strategy_execution": result,
        "warning": (
            "Synthetic prices and provisional unapproved strategy rules test integration mechanics only. "
            "This is not a broker account result, live runner, or evidence of profitability. "
            "Historical data and calendar-bound execution are not supported by this command."
        ),
    }
    report["fingerprint"] = hashlib.sha256(json.dumps(
        report, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    write_new_json(args.report_json, report)
    return {
        "status": report["status"],
        "data_basis": "synthetic",
        "broker_connected": False,
        "real_orders_submitted": 0,
        "promotion_eligible": False,
        "hypothesis_unapproved": report["hypothesis_unapproved"],
        "source_risk_policy_complete": report["source_risk_policy_complete"],
        "loss_limits_configured": report["loss_limits_configured"],
        "counts": result["counts"],
        "fingerprint": report["fingerprint"],
        "report_json": str(args.report_json),
        "warning": report["warning"],
    }


def _session_output_preflight(args: argparse.Namespace, outputs: Sequence[Path]) -> None:
    inputs = (args.csv, args.manifest, args.calendar, args.raw_data or args.csv)
    _require_distinct_outputs(outputs, inputs)
    for target in outputs:
        if target.exists() or target.is_symlink():
            raise FileExistsError("output already exists: {}".format(target))


def _replay_session_features(args: argparse.Namespace) -> dict:
    _session_output_preflight(args, (args.trace_json,))
    bars, manifest, calendar, binding, calendar_binding = _load_session_snapshot(args)
    report = run_session_feature_replay(
        bars, calendar=calendar, policy_bundle=provisional_diagnostic_baseline_v1(),
        dataset_fingerprint=binding.manifest_fingerprint,
    )
    # Serialize completely before creating anything; publish exclusively so an
    # existing artifact (including a late collision) can never be overwritten.
    payload = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    args.trace_json.parent.mkdir(parents=True, exist_ok=True)
    staged = _staging_path(args.trace_json)
    try:
        staged.write_text(payload, encoding="utf-8")
        os.link(str(staged), str(args.trace_json))
    finally:
        _discard_staging(staged)
    return {
        "status": report["status"], "diagnostic_only": True, "execution": None,
        "strategy_candidates_generated": False, "full_strategy_replay_supported": False,
        "manifest_identity": binding.manifest_identity,
        "calendar_binding": calendar_binding,
        "fingerprint": report["fingerprint"], "counts": report["counts"],
        "warmup": report["warmup"], "trace_json": str(args.trace_json),
        "warnings": report["warnings"],
    }


def _replay_session_diagnostic(args: argparse.Namespace) -> dict:
    _session_output_preflight(args, (args.trace_json, args.decisions_csv))
    bars, manifest, calendar, binding, calendar_binding = _load_session_snapshot(args)
    result = run_diagnostic_replay(
        bars, dataset_fingerprint=binding.manifest_fingerprint,
        policy_bundle=provisional_session_diagnostic_baseline_v1(calendar),
    )
    args.trace_json.parent.mkdir(parents=True, exist_ok=True)
    args.decisions_csv.parent.mkdir(parents=True, exist_ok=True)
    outputs = write_diagnostic_trace_artifacts(
        result, args.trace_json, args.decisions_csv, overwrite=False,
    )
    summary = _replay_summary(result, binding, manifest, outputs)
    summary["status"] = "session_diagnostic_replay_complete"
    summary["session"] = {
        "calendar_binding": calendar_binding, "semantics": session_semantics(),
    }
    summary["warnings"].extend((
        "Closure behavior is a named provisional hypothesis, not a verified broker rule.",
        "Calendar hashes verify recorded evidence, not its truth or data-use rights.",
    ))
    return summary


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
            "engine_version": result.engine_version,
            "schema_version": result.schema_version,
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
        if args.command == "run-paper-demo":
            _, summary = run_local_paper_demo(args.output_root)
            print(summary, end="")
            return 0

        if args.command == "export-paper-scenario":
            fixture = synthetic_execution_scenario()
            configured = args.max_losses_per_day is not None
            if configured != (args.max_weekly_drawdown_fraction is not None):
                raise ValueError("provide both --max-losses-per-day and --max-weekly-drawdown-fraction")
            if configured:
                limits = PaperLossLimits(args.max_losses_per_day, args.max_weekly_drawdown_fraction)
                fixture.update(version=2, loss_limits=dict(
                    max_losses_per_day=limits.max_losses_per_day,
                    max_weekly_drawdown_fraction=format(limits.max_weekly_drawdown_fraction, "f"),
                ))
                parse_paper_scenario(json.dumps(fixture).encode("utf-8"))
            write_new_json(args.destination, fixture)
            print(json.dumps({
                "status": "synthetic_paper_scenario_exported", "path": str(args.destination),
                "broker_connected": False,
                "loss_limits_configured": configured,
                "warning": "Scripted execution fixture only; not a strategy or Exness specification.",
            }, indent=2, sort_keys=True))
            return 0

        if args.command in ("demo-paper-execution", "simulate-paper"):
            if args.command == "simulate-paper":
                _require_distinct_outputs((args.report_json,), (args.scenario,))
                scenario = load_paper_scenario(args.scenario)
            else:
                scenario = parse_paper_scenario(json.dumps(synthetic_execution_scenario()).encode("utf-8"))
            report = run_paper_scenario(scenario)
            write_new_json(args.report_json, report)
            summary_keys = (
                "status", "engine_version", "broker_connected", "real_orders_submitted",
                "promotion_eligible", "counts", "initial_balance", "final_balance",
                "final_equity", "net_pnl", "total_commission", "total_financing", "warning",
                "loss_limits",
            )
            summary = {key: report[key] for key in summary_keys}
            summary.update({key: report[key] for key in ("losses_today", "loss_count_halt", "weekly_halt")
                            if key in report})
            summary["report_json"] = str(args.report_json)
            print(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False))
            return 0

        if args.command == "replay-strategy-paper":
            summary = _replay_strategy_paper(args)
            print(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False))
            return 0

        if args.command == "import-mt5-ticks":
            report = import_mt5_tick_file(
                args.raw, plan_path=args.plan, calendar_path=args.calendar,
                output_csv=args.output_csv, output_manifest=args.manifest,
                report_json=args.report_json,
            )
            print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
            return 0

        if args.command == "import-mt5-session-ticks":
            report = import_mt5_session_tick_file(
                args.raw, plan_path=args.plan, calendar_path=args.calendar,
                output_json=args.output_json,
            )
            print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
            return 0

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

        if args.command == "check-replay-data":
            report = _check_replay_data(args)
            print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
            return 0 if report["ready_for_diagnostic_replay"] else 1

        if args.command == "replay-session-features":
            report = _replay_session_features(args)
            print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
            return 0

        if args.command == "replay-session-diagnostic":
            report = _replay_session_diagnostic(args)
            print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
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

            bars, manifest, normalized_before, raw_before = _load_replay_snapshot(
                args.csv, args.manifest, raw_path,
            )

            if manifest.version != 1:
                raise ValueError(
                    "replay-diagnostic supports strict manifest v1 only; "
                    "use replay-session-features or replay-session-diagnostic with --calendar for manifest v2"
                )

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
