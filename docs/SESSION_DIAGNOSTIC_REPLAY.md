# Session diagnostic replay

`replay-session-diagnostic` runs the complete offline paper-candidate pipeline across explicit
whole-hour market closures. It derives H1/ATR/pivots, impulses and zones, zone lifecycle and
retests, regimes, M15 confirmations, and one `BUY`/`SELL`/`NO_TRADE` research decision per M15 bar.
It does not submit or simulate orders, calculate position sizes, manage an account, or report P&L.

## Command and required evidence

```bash
PYTHONPATH=src python3 -m xau_trader replay-session-diagnostic \
  data/processed/xau-m15.csv \
  --manifest data/processed/xau-m15.manifest-v2.json \
  --calendar data/raw/xau-calendar.json \
  --raw-data data/raw/source.csv \
  --trace-json /tmp/xau-session-trace.json \
  --decisions-csv /tmp/xau-session-decisions.csv
```

Supply a normalized M15 bid/ask CSV, manifest v2, and the exact local calendar JSON file referenced
by that manifest. Omit `--raw-data` only when the CSV itself is the acquired source artifact.
Both output arguments are required and must identify new, distinct paths. Existing files,
input/output collisions, and changed inputs are rejected. There is no `--overwrite` option.
The command does not download data or fetch the calendar URI.

The shared [data contract](DATA_FORMAT.md) binds raw and normalized data hashes, coverage, known
gaps, provider/product/rights claims, and the calendar's exact file hash and canonical fingerprint.
Calendar identity and provider/product fields must match the manifest; its finite coverage must
contain the dataset. Hash checks establish consistency of recorded evidence, not the truth of a
schedule or permission to use the data.

Point-in-time qualification applies to market evidence, not calendar retrieval: replay `as_of`
does not prove that the supplied schedule was known at that historical time. The session metadata
records `calendar_knowledge: supplied_schedule_assumed_known_not_verified_point_in_time`.

Every closure must begin and end on a whole UTC hour. Every observed gap must exactly equal a
declared closure. Missing open-session bars, bars inside closures, and partial UTC hours fail
validation. No holiday, DST, weekend, or broker schedule is inferred.

## Provisional closure interpretation

The command selects `provisional_session_diagnostic_baseline_v1(calendar)`, not the original
strict baseline. Its ID is `xauusd-supply-demand-session-provisional`; every relevant policy binds
the calendar fingerprint and `xauusd-session-strategy-v1` semantics. These are explicitly
unapproved research choices, not rules verified from a broker or the source video.

| Area | Behavior |
| --- | --- |
| Clock and windows | Keep actual timestamps. Indicator/impulse/origin/pivot windows count actual open bars; never fill or compress a closure or restart seeds. |
| Zones | Preserve across closures; apply invalidation at the applicable observed H1 close. |
| Active touch episode | Preserve across closures until the next observed price exit or invalidation. |
| Confirmation expiry | Count elapsed M15 intervals, including closed time. The baseline's two-interval window does not pause. |
| Closing-bar entry | Emit `NO_TRADE` with `scheduled_closure_next_open`; record the next scheduled opening time but do not propose an entry there. |
| Unverified next open | Emit `NO_TRADE` with `calendar_coverage_exhausted` when the calendar does not cover a complete next M15 interval. The coverage-end timestamp is not an executable open. |
| Availability | Preserve dependency receipt gates and next-open deadlines; delayed evidence cannot backdate a candidate. |

The input must contain at least 100 actual M15 bars in complete UTC hours. The first 96 actual
rows are retained as `pre_roll`; closures do not consume warm-up bars. Subsequent rows are
`post_pre_roll`. Lifecycle state is explicitly empty at dataset start, so zones formed before
the supplied history remain unknown.

## Outputs and validation

The JSON trace contains the complete source and derived evidence, policy/dataset identities,
calendar and session metadata, lifecycle snapshots, and decisions. The decision CSV has one row
per M15 bar, including warm-up rows, with `calendar_fingerprint` and `session_semantics` columns.
Export checks the result against exact replay recomputation before publishing the artifacts.
Retain the original calendar, sidecar manifest, raw artifact, and normalized CSV alongside them.
The trace embeds market data; retention and sharing rights must cover the trace as well.

The CLI reports `session_diagnostic_replay_complete` and evidence counts on success (exit `0`);
invalid inputs or output collisions return `2`. Success establishes only a reproducible offline
research run. A candidate has no quantity, fill, stop, target, position, broker instruction, or
profit claim.

The current deterministic synthetic fixture has two 23-hour sessions and a one-hour closure:

| Evidence | Count |
| --- | ---: |
| Actual M15 / derived H1 bars | 184 / 46 |
| Zones / M15 confirmations | 12 / 4 |
| Decisions | 184 |
| Pre-roll / post-pre-roll decisions | 96 / 88 |
| BUY / SELL / NO_TRADE decisions | 0 / 0 / 184 |

This fixture produced no entry candidates; it did not simulate losing or winning trades.
Those counts are regression checks, not evidence that a strategy makes money. No real Exness
dataset/calendar or connector has been validated, and this implementation sends no demo trades.
Next: acquire an authorized sample, verify its calendar, hand-reconcile the full trace, then
resolve execution, stop/target, sizing, costs and risk semantics before a separate P&L simulator
or demo-order connector. See [DATA_INTAKE.md](DATA_INTAKE.md).

The older `replay-diagnostic` and `check-replay-data` commands remain strict manifest v1 consumers.
[`replay-session-features`](SESSION_FEATURE_REPLAY.md) remains feature-only, with its original
output scope and readiness flags.
