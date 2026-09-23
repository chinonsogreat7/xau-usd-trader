"""A zero-credential, human-readable local simulator launch.

Each invocation creates its own report directory. Nothing runs in the
background, starts MT5, installs a package, or sends an order.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile

from .paper_execution_io import (
    parse_paper_scenario, run_paper_scenario, synthetic_execution_scenario, write_new_json,
)


def _money(value: str) -> str:
    return format(Decimal(value), ".2f")


def format_local_demo_summary(report: dict, run_directory: Path) -> str:
    """Describe the synthetic run without presenting it as strategy performance."""
    counts = report["counts"]
    lines = [
        "XAU/USD — OFFLINE PAPER DEMO",
        "Synthetic prices and scripted entries. Not our strategy's performance.",
        "Broker connection: NONE | Real orders submitted: 0",
        "Daily loss-count / weekly guard: {}".format(
            "configured (provisional)" if report.get("loss_limits", {}).get("enabled")
            else "disabled in this legacy scripted fixture"
        ),
        "",
        "Closed trades: {} | Rejected entries: {} | Expired/cancelled: {}".format(
            counts["trades"], counts["rejected_intents"], counts["cancelled_intents"],
        ),
    ]
    for trade in report["trades"]:
        lines.append("  {}: {} {} lots | {} -> {} | {} | {} USD after costs".format(
            trade["intent_id"], trade["side"].upper(), trade["lots"],
            trade["entry_price"], trade["exit_price"], trade["exit_reason"], _money(trade["net_pnl"]),
        ))
    lines.extend([
        "",
        "Hypothetical balance: {} -> {} USD".format(
            _money(report["initial_balance"]), _money(report["final_balance"]),
        ),
        "This is NOT your Exness balance, a profit forecast, or a live bot.",
        "",
        "Saved to: {}".format(run_directory.resolve()),
        "  summary.txt  — this readable summary",
        "  report.json  — every simulated fill, risk event, and calculation",
        "  scenario.json — exact synthetic input for replay",
        "",
        "Finished. No process was left running. No MT5 login or deposit is needed.",
    ])
    return "\n".join(lines) + "\n"


def run_local_paper_demo(output_root: Path) -> tuple:
    """Return (new_directory, readable_summary) after a complete local test run."""
    fixture = synthetic_execution_scenario()
    # Match the exact bytes that write_new_json publishes, including whitespace.
    raw = (json.dumps(fixture, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    report = run_paper_scenario(parse_paper_scenario(raw))
    if (
        report.get("broker_connected") is not False
        or type(report.get("real_orders_submitted")) is not int
        or report["real_orders_submitted"] != 0
        or report.get("scenario", {}).get("data_basis") != "synthetic"
    ):
        raise ValueError("The local launcher only displays disconnected synthetic simulations")
    output_root = Path(output_root).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_directory = Path(tempfile.mkdtemp(prefix="paper-demo-{}-".format(stamp), dir=str(output_root)))
    summary = format_local_demo_summary(report, run_directory)
    try:
        write_new_json(run_directory / "scenario.json", fixture)
        write_new_json(run_directory / "report.json", report)
        with (run_directory / "summary.txt").open("x", encoding="utf-8") as handle:
            handle.write(summary)
    except OSError as error:
        # Preserve partial evidence rather than delete user-visible files.
        raise OSError("Could not finish saving the demo. Partial files may be in {}: {}".format(run_directory, error)) from error
    return run_directory, summary
