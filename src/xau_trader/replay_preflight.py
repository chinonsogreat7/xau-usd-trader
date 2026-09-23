"""Inspect temporal and history compatibility with the diagnostic replay.

This cheap, read-only check neither executes the strategy nor validates dataset
rights, manifest provenance, or profitability. Segment history counts describe
complete UTC hours contained within each segment; no input is trimmed or repaired.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Sequence

from .diagnostic_replay import (
    diagnostic_minimum_input_m15_bars,
    diagnostic_pre_roll_m15_bars,
)
from .domain import AvailabilityBasis, QuoteBar
from .multitimeframe import M15_DURATION
from .research_baseline import ResearchPolicyBundle


def _utc(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo == timezone.utc


def _hour_boundary(value: datetime) -> bool:
    return value.minute == 0 and value.second == 0 and value.microsecond == 0


def _segment(bars: Sequence[QuoteBar], minimum: int) -> Dict[str, Any]:
    start, end = bars[0].start_time, bars[-1].timestamp
    complete_start = start.replace(minute=0, second=0, microsecond=0)
    if complete_start != start:
        complete_start += timedelta(hours=1)
    complete_end = end.replace(minute=0, second=0, microsecond=0)
    complete_count = max(0, (complete_end - complete_start) // M15_DURATION)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "row_count": len(bars),
        "complete_hour_row_count": complete_count,
        "meets_minimum_history": complete_count >= minimum,
    }


def inspect_replay_bars(
    bars: Sequence[QuoteBar], policy_bundle: ResearchPolicyBundle
) -> Dict[str, Any]:
    """Report technical input compatibility for replay of the entire sequence.

    ``compatible`` addresses temporal structure and configured history only.
    A sufficiently long segment does not make a gapped input compatible. Its
    ``meets_minimum_history`` flag is only a count, not replay approval.
    """

    pre_roll = diagnostic_pre_roll_m15_bars(policy_bundle)
    minimum = diagnostic_minimum_input_m15_bars(policy_bundle)
    source = tuple(bars)
    if any(not isinstance(bar, QuoteBar) for bar in source):
        raise TypeError("M15 input must contain QuoteBar values")

    issues: List[Dict[str, Any]] = []
    gaps: List[Dict[str, Any]] = []
    segments: List[Dict[str, Any]] = []

    def issue(code: str, message: str, **details: Any) -> None:
        issues.append(dict(code=code, message=message, **details))

    if not source:
        issue("empty_input", "M15 input is empty.")

    segmentable = bool(source)
    for index, bar in enumerate(source):
        invalid_times = [
            field for field in ("start_time", "timestamp", "available_at")
            if not _utc(getattr(bar, field))
        ]
        if invalid_times:
            issue("invalid_utc", "Bar timestamps must be normalized to UTC.",
                  row_index=index, fields=invalid_times)
        interval_valid = _utc(bar.start_time) and _utc(bar.timestamp)
        if not interval_valid:
            segmentable = False
        else:
            if bar.timestamp - bar.start_time != M15_DURATION:
                issue("invalid_duration", "Each M15 bar must span exactly 15 minutes.",
                      row_index=index)
                segmentable = False
            if (bar.start_time.minute % 15 or bar.start_time.second
                    or bar.start_time.microsecond):
                issue("invalid_alignment", "M15 bars must align to UTC 15-minute boundaries.",
                      row_index=index)
                segmentable = False
            if index and _utc(source[index - 1].timestamp):
                if bar.start_time < source[index - 1].timestamp:
                    issue("invalid_order", "Bars must be chronological without overlaps or duplicates.",
                          row_index=index)
                    segmentable = False

        if not isinstance(bar.availability_basis, AvailabilityBasis):
            issue("invalid_availability_basis", "Availability basis must be an AvailabilityBasis.",
                  row_index=index)
        if _utc(bar.available_at):
            if _utc(bar.timestamp) and bar.available_at < bar.timestamp:
                issue("availability_before_close", "Availability cannot precede the bar end.",
                      row_index=index)
            if (index and _utc(source[index - 1].available_at)
                    and bar.available_at < source[index - 1].available_at):
                issue("availability_order", "Receipt times must be non-decreasing in bar order.",
                      row_index=index)
            if (bar.availability_basis in (
                    AvailabilityBasis.SYNTHETIC,
                    AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION,
                ) and bar.available_at != bar.timestamp):
                issue("availability_must_equal_close",
                      "Synthetic and historical-close availability must equal the bar end.",
                      row_index=index)

    bases = {bar.availability_basis for bar in source
             if isinstance(bar.availability_basis, AvailabilityBasis)}
    if len(bases) > 1:
        issue("mixed_availability_basis", "One replay must use a single availability basis.")
    if source:
        if _utc(source[0].start_time) and not _hour_boundary(source[0].start_time):
            issue("partial_hour_start", "Replay input must begin on a UTC hour boundary.")
        if _utc(source[-1].timestamp) and not _hour_boundary(source[-1].timestamp):
            issue("partial_hour_end", "Replay input must end on a UTC hour boundary.")
        if len(source) % 4:
            issue("incomplete_hours", "Replay input must contain four M15 bars per UTC hour.")

    if segmentable:
        segment_start = 0
        for index in range(1, len(source)):
            previous_end, next_start = source[index - 1].timestamp, source[index].start_time
            if next_start > previous_end:
                gaps.append({
                    "start": previous_end.isoformat(),
                    "end": next_start.isoformat(),
                    "duration_seconds": (next_start - previous_end).total_seconds(),
                })
                segments.append(_segment(source[segment_start:index], minimum))
                segment_start = index
        segments.append(_segment(source[segment_start:], minimum))
        if gaps:
            issue("data_gaps", "The replay requires one contiguous M15 sequence; gaps are not repaired.",
                  gap_count=len(gaps))
        longest = max(segment["complete_hour_row_count"] for segment in segments)
        if longest < minimum:
            issue("insufficient_history",
                  "No contiguous segment contains the required complete-hour M15 history.",
                  required_rows=minimum, longest_complete_hour_rows=longest)
    elif source:
        issue("segments_unavailable",
              "Contiguous segments cannot be counted until M15 intervals and chronological order are valid.")

    compatible = not issues
    return {
        "schema": "xau_trader.replay_preflight",
        "version": 1,
        "status": "compatible" if compatible else "incompatible",
        "compatible": compatible,
        "compatibility_scope": "technical_replay_input_only",
        "policy_bundle_fingerprint": policy_bundle.fingerprint,
        "row_count": len(source),
        "requirements": {
            "pre_roll_m15_bars": pre_roll,
            "minimum_input_m15_bars": minimum,
            "requires_contiguous_data": True,
        },
        "issues": issues,
        "gaps": gaps,
        "contiguous_segments": segments,
    }
