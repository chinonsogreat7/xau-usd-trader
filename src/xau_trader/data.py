"""Strict CSV market-data input and deterministic demonstration data."""

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import math
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Union

from .domain import AvailabilityBasis, QuoteBar


CSV_FIELDS = (
    "start_time",
    "timestamp",
    "available_at",
    "availability_basis",
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "ask_open",
    "ask_high",
    "ask_low",
    "ask_close",
    "volume",
)

REQUIRED_CSV_FIELDS = {
    "start_time",
    "timestamp",
    "available_at",
    "availability_basis",
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "ask_open",
    "ask_high",
    "ask_low",
    "ask_close",
}


class DataValidationError(ValueError):
    pass


def _parse_timestamp(raw: str) -> datetime:
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DataValidationError("timestamps must contain an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def read_quote_bars_csv(path: Union[str, Path]) -> List[QuoteBar]:
    source = Path(path)
    bars: List[QuoteBar] = []
    try:
        with source.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            available = set(reader.fieldnames or ())
            missing = sorted(REQUIRED_CSV_FIELDS - available)
            if missing:
                raise DataValidationError("missing CSV columns: {}".format(", ".join(missing)))
            for line_number, row in enumerate(reader, start=2):
                try:
                    raw_volume = (row.get("volume") or "").strip()
                    timestamp = _parse_timestamp(row["timestamp"])
                    start_time = _parse_timestamp(row["start_time"])
                    raw_available_at = row["available_at"].strip()
                    if not raw_available_at:
                        raise DataValidationError("available_at must not be empty")
                    bar = QuoteBar(
                        timestamp=timestamp,
                        start_time=start_time,
                        availability_basis=row["availability_basis"],
                        bid_open=float(row["bid_open"]),
                        bid_high=float(row["bid_high"]),
                        bid_low=float(row["bid_low"]),
                        bid_close=float(row["bid_close"]),
                        ask_open=float(row["ask_open"]),
                        ask_high=float(row["ask_high"]),
                        ask_low=float(row["ask_low"]),
                        ask_close=float(row["ask_close"]),
                        volume=float(raw_volume) if raw_volume else None,
                        available_at=_parse_timestamp(raw_available_at),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise DataValidationError(
                        "invalid row {}: {}".format(line_number, exc)
                    ) from exc
                if bars and bar.timestamp <= bars[-1].timestamp:
                    raise DataValidationError(
                        "row {} is not strictly later than the previous row".format(line_number)
                    )
                if bars and bar.start_time < bars[-1].timestamp:
                    raise DataValidationError(
                        "row {} overlaps the previous bar".format(line_number)
                    )
                bars.append(bar)
    except OSError as exc:
        raise DataValidationError("could not read {}: {}".format(source, exc)) from exc
    if not bars:
        raise DataValidationError("CSV contains no quote bars")
    return bars


def write_quote_bars_csv(path: Union[str, Path], bars: Iterable[QuoteBar]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        _write_quote_bars(handle, bars)


def _write_quote_bars(handle, bars: Iterable[QuoteBar]) -> None:
    writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
    writer.writeheader()
    for bar in bars:
        writer.writerow({
            "start_time": bar.start_time.isoformat(),
            "timestamp": bar.timestamp.isoformat(),
            "available_at": bar.available_at.isoformat(),
            "availability_basis": bar.availability_basis.value,
            "bid_open": bar.bid_open,
            "bid_high": bar.bid_high,
            "bid_low": bar.bid_low,
            "bid_close": bar.bid_close,
            "ask_open": bar.ask_open,
            "ask_high": bar.ask_high,
            "ask_low": bar.ask_low,
            "ask_close": bar.ask_close,
            "volume": "" if bar.volume is None else bar.volume,
        })


def quote_bars_csv_bytes(bars: Iterable[QuoteBar]) -> bytes:
    """Serialize with exactly the file writer's schema and newline convention."""
    handle = io.StringIO(newline="")
    _write_quote_bars(handle, bars)
    return handle.getvalue().encode("utf-8")


def sha256_file(path: Union[str, Path]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generate_demo_bars(count: int = 480) -> Sequence[QuoteBar]:
    """Generate deterministic fake bars for plumbing tests, never strategy evidence."""

    if count < 2:
        raise ValueError("count must be at least 2")
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars: List[QuoteBar] = []
    previous_close = 2_000.0
    for index in range(count):
        regime = 0.08 * index if index < count // 2 else 0.08 * (count - index)
        mid_close = 2_000.0 + regime + 4.0 * math.sin(index / 13.0) + 1.2 * math.sin(index / 3.0)
        mid_open = previous_close
        padding = 0.45 + 0.10 * abs(math.sin(index / 5.0))
        mid_high = max(mid_open, mid_close) + padding
        mid_low = min(mid_open, mid_close) - padding
        spread = 0.28 + 0.04 * (1.0 + math.sin(index / 7.0))
        bars.append(
            QuoteBar.from_mid(
                start_time=start + timedelta(hours=index),
                timestamp=start + timedelta(hours=index + 1),
                availability_basis=AvailabilityBasis.SYNTHETIC,
                available_at=start + timedelta(hours=index + 1),
                mid_open=mid_open,
                mid_high=mid_high,
                mid_low=mid_low,
                mid_close=mid_close,
                spread=spread,
                volume=1_000.0 + 100.0 * abs(math.sin(index / 9.0)),
            )
        )
        previous_close = mid_close
    return tuple(bars)


def generate_demo_m15_bars(count: int = 480) -> Sequence[QuoteBar]:
    """Generate complete-hour M15 data for diagnostic replay plumbing.

    The series is deterministic and deliberately synthetic.  ``count`` must be
    a multiple of four so callers cannot mistake a partial final hour for a
    valid M15-to-H1 aggregation input.
    """

    if isinstance(count, bool) or not isinstance(count, int):
        raise TypeError("count must be an integer")
    if count < 4:
        raise ValueError("count must be at least 4")
    if count % 4:
        raise ValueError("count must contain complete four-bar UTC hours")

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars: List[QuoteBar] = []
    previous_close = 2_000.0
    for index in range(count):
        # A slow triangular drift plus two deterministic waves creates varied
        # candle bodies without pretending to model a real XAU/USD process.
        half = count // 2
        drift = 0.035 * index if index < half else 0.035 * (count - index)
        mid_close = (
            2_000.0
            + drift
            + 3.2 * math.sin(index / 19.0)
            + 0.9 * math.sin(index / 4.0)
        )
        mid_open = previous_close
        padding = 0.20 + 0.08 * abs(math.sin(index / 6.0))
        mid_high = max(mid_open, mid_close) + padding
        mid_low = min(mid_open, mid_close) - padding
        spread = 0.26 + 0.03 * (1.0 + math.sin(index / 11.0))
        bar_start = start + timedelta(minutes=15 * index)
        bar_end = bar_start + timedelta(minutes=15)
        bars.append(
            QuoteBar.from_mid(
                start_time=bar_start,
                timestamp=bar_end,
                availability_basis=AvailabilityBasis.SYNTHETIC,
                available_at=bar_end,
                mid_open=mid_open,
                mid_high=mid_high,
                mid_low=mid_low,
                mid_close=mid_close,
                spread=spread,
                volume=250.0 + 30.0 * abs(math.sin(index / 8.0)),
            )
        )
        previous_close = mid_close
    return tuple(bars)


def analyze_cadence(bars: Sequence[QuoteBar], expected_minutes: float = 60.0) -> Dict[str, object]:
    if not math.isfinite(expected_minutes) or expected_minutes <= 0:
        raise ValueError("expected_minutes must be a finite positive number")
    expected_seconds = expected_minutes * 60.0
    deltas = [
        (current.timestamp - previous.timestamp).total_seconds()
        for previous, current in zip(bars, bars[1:])
    ]
    durations = [
        (bar.timestamp - bar.start_time).total_seconds()
        for bar in bars
    ]
    tolerance = 1e-6
    shorter = sum(delta < expected_seconds - tolerance for delta in deltas)
    gaps = sum(delta > expected_seconds + tolerance for delta in deltas)
    duration_mismatches = sum(
        abs(duration - expected_seconds) > tolerance for duration in durations
    )
    return {
        "expected_minutes": expected_minutes,
        "observed_intervals": len(deltas),
        "short_interval_count": shorter,
        "gap_count": gaps,
        "duration_mismatch_count": duration_mismatches,
        "max_gap_minutes": (max(deltas) / 60.0 if deltas else 0.0),
        "exact_cadence": shorter == 0 and gaps == 0 and duration_mismatches == 0,
        "session_calendar_applied": False,
    }
