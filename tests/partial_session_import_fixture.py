"""Generated MT5-shaped quotes and fictional partial-session import evidence.

No file produced here is broker history or evidence of an Exness schedule. The
optional 22:01 reopening exercises a second partially open nominal candle.
"""

import hashlib
import json

from mt5_import_fixture import write_mt5_import_fixture
from xau_trader.session_calendar import parse_session_calendar_bytes


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def write_partial_session_import_fixture(root, *, reopening_minute=0):
    """Write generated test inputs into an existing test/demo-only directory.

    The default reproduces the importer test's original 20:58–22:00 fictional
    closure byte for byte. ``reopening_minute=1`` changes its end to 22:01 and
    moves the first reopening observation to 22:01:00.100; nothing is filled.
    All provider, rights, clock and source claims remain explicitly synthetic.
    """
    if type(reopening_minute) is not int or reopening_minute not in (0, 1):
        raise ValueError("reopening_minute must be 0 or 1 for the generated fixture")
    raw, plan_path, calendar_path = write_mt5_import_fixture(root)
    calendar = json.loads(calendar_path.read_text())
    calendar.update({
        "coverage_start": "2024-01-01T20:00:00Z",
        "coverage_end": "2024-01-01T23:00:00Z",
        "retrieved_at": "2024-01-01T23:00:00Z",
        "closures": [{"start": "2024-01-01T20:58:00Z",
                      "end": "2024-01-01T22:{:02d}:00Z".format(reopening_minute),
                      "reason": "Generated partial-hour test closure, not a provider schedule"}],
    })
    calendar_path.write_bytes(_canonical(calendar) + b"\n")
    lines = ["<DATE>\t<TIME>\t<BID>\t<ASK>\t<LAST>\t<VOLUME>\t<FLAGS>"]
    times = ["20:00:00.100", "20:15:00.100", "20:30:00.100", "20:45:00.100",
             "20:57:59.999", "20:57:59.999", "22:{:02d}:00.100".format(reopening_minute),
             "22:15:00.100", "22:30:00.100", "22:45:00.100"]
    for index, time in enumerate(times):
        lines.append("2024.01.01\t{}\t{}.0\t{}.5\t0\t{}\t6".format(
            time, 2000 + index, 2000 + index, index))
    raw.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    plan = json.loads(plan_path.read_text())
    plan["coverage"] = {"start": calendar["coverage_start"], "end": calendar["coverage_end"]}
    plan["source"]["retrieved_at"] = "2024-01-02T00:00:00Z"
    plan["raw_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
    calendar_bytes = calendar_path.read_bytes()
    plan["session_calendar"]["artifact_sha256"] = hashlib.sha256(calendar_bytes).hexdigest()
    plan["session_calendar"]["content_fingerprint"] = parse_session_calendar_bytes(calendar_bytes).fingerprint
    plan_path.write_bytes(_canonical(plan) + b"\n")
    return raw, plan_path, calendar_path
