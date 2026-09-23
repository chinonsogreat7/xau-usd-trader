"""Deterministic fake MT5-shaped quotes; never broker data or a broker schedule."""

import csv
import io
from datetime import timedelta

from session_feature_fixture import session_fixture
from xau_trader.data import sha256_file
from xau_trader.mt5_import_plan import mt5_import_plan_from_dict


def write_mt5_import_fixture(root, *, offset_minutes=0):
    bars, calendar = session_fixture()
    raw = root / "synthetic-mt5-ticks.csv"
    plan_path = root / "synthetic-import-plan.json"
    calendar_path = root / "synthetic-calendar.json"
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter="\t")
    writer.writerow(("<DATE>", "<TIME>", "<BID>", "<ASK>", "<LAST>", "<VOLUME>", "<FLAGS>"))
    for bar in bars:
        for displacement, side in ((timedelta(milliseconds=100), "open"),
                                   (timedelta(minutes=5), "high"),
                                   (timedelta(minutes=10), "low"),
                                   (timedelta(minutes=15, milliseconds=-1), "close")):
            local = bar.start_time + displacement + timedelta(minutes=offset_minutes)
            writer.writerow((
                local.strftime("%Y.%m.%d"),
                local.strftime("%H:%M:%S") + ".{:03d}".format(local.microsecond // 1000),
                getattr(bar, "bid_" + side), getattr(bar, "ask_" + side), 0, 0, 6,
            ))
    raw.write_bytes(stream.getvalue().encode("utf-8"))
    calendar_path.write_text(calendar.canonical_json + "\n", encoding="utf-8")
    plan = mt5_import_plan_from_dict({
        "schema": "xau_trader.mt5_tick_import_plan", "version": 1,
        "provider": {"name": calendar.provider_name, "legal_entity": calendar.provider_legal_entity},
        "instrument": {"symbol": "XAU_USD", "product_form": calendar.product_form},
        "source": {
            "route": "local-fixture://mt5-complete-quotes",
            "account_environment": "synthetic research fixture without a trading account",
            "acquisition_basis": "synthetic_generation", "source_symbol": "XAUUSD_fixture",
            "retrieved_at": calendar.coverage_end.isoformat(),
        },
        "rights": {
            "basis": "self-generated synthetic test quotes",
            "api_data_agreement_version": "project-owned-fixture-v1",
            "retention_basis": "project-owned synthetic regression fixture",
        },
        "timestamp": {"utc_offset_minutes": offset_minutes,
                      "evidence_reference": "local-fixture://native-utc-test-clock"},
        "coverage": {"start": calendar.coverage_start.isoformat(),
                     "end": calendar.coverage_end.isoformat()},
        "raw_sha256": sha256_file(raw),
        "session_calendar": {
            "id": calendar.calendar_id, "version": calendar.revision,
            "artifact_uri": "local-fixture://synthetic-calendar.json",
            "artifact_sha256": sha256_file(calendar_path),
            "content_fingerprint": calendar.fingerprint,
        },
    })
    plan_path.write_text(plan.canonical_json + "\n", encoding="utf-8")
    return raw, plan_path, calendar_path
