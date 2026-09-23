"""Synthetic two-session fixture; not an Exness schedule or market dataset."""

from dataclasses import replace
from datetime import timedelta

from xau_trader.cli import _synthetic_m15_manifest
from xau_trader.data import generate_demo_m15_bars, sha256_file, write_quote_bars_csv
from xau_trader.dataset_manifest import DatasetCoverage, KnownGap, SessionCalendar
from xau_trader.session_calendar import ScheduledClosure, SessionCalendarArtifact


def session_fixture():
    original = generate_demo_m15_bars(184)
    shift = timedelta(hours=1)
    bars = tuple(original[:92]) + tuple(replace(
        bar, start_time=bar.start_time + shift,
        timestamp=bar.timestamp + shift, available_at=bar.available_at + shift,
    ) for bar in original[92:])
    provisional = _synthetic_m15_manifest(original, "a" * 64)
    calendar = SessionCalendarArtifact(
        calendar_id="synthetic-two-sessions", revision="fixture-v1",
        provider_name=provisional.provider.name,
        provider_legal_entity=provisional.provider.legal_entity,
        instrument="XAU_USD", product_form=provisional.instrument.product_form,
        coverage_start=bars[0].start_time, coverage_end=bars[-1].timestamp,
        source_reference="local-fixture://synthetic-two-sessions-v1",
        retrieved_at=bars[-1].timestamp,
        closures=(ScheduledClosure(
            start=bars[91].timestamp, end=bars[92].start_time,
            reason="Synthetic one-hour closure; not a provider schedule",
        ),),
    )
    return bars, calendar


def write_session_fixture(root):
    bars, calendar = session_fixture()
    market_csv = root / "synthetic-m15.csv"
    calendar_json = root / "synthetic-calendar.json"
    manifest_json = root / "synthetic-m15.manifest.json"
    write_quote_bars_csv(market_csv, bars)
    calendar_json.write_text(calendar.canonical_json + "\n", encoding="utf-8")
    digest = sha256_file(market_csv)
    base = _synthetic_m15_manifest(generate_demo_m15_bars(184), digest)
    manifest = replace(
        base, version=2,
        source=replace(base.source, retrieved_at=bars[-1].timestamp),
        session_calendar=SessionCalendar(
            calendar_id=calendar.calendar_id, version=calendar.revision,
            artifact_uri="local-fixture://synthetic-calendar.json",
            artifact_sha256=sha256_file(calendar_json),
            content_fingerprint=calendar.fingerprint,
        ),
        coverage=DatasetCoverage(
            start=bars[0].start_time, end=bars[-1].timestamp, row_count=len(bars),
            known_gaps=(KnownGap(
                start=calendar.closures[0].start, end=calendar.closures[0].end,
                reason=calendar.closures[0].reason,
            ),),
        ),
    )
    manifest_json.write_text(manifest.canonical_json + "\n", encoding="utf-8")
    return market_csv, manifest_json, calendar_json
