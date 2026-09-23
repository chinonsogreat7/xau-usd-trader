# Session feature replay

`replay-session-features` computes H1 bars, H1 Wilder ATR(14), confirmed H1 pivots, and M15 EMA(8)
across explicitly declared whole-hour closures. It is an offline feature calculation using the
provisional diagnostic policy bundle. It does not generate zones, confirmations, candidate
decisions, orders, or returns.

## Command and inputs

```bash
PYTHONPATH=src python3 -m xau_trader replay-session-features \
  data/processed/xau-m15.csv \
  --manifest data/processed/xau-m15.manifest-v2.json \
  --calendar data/raw/xau-calendar.json \
  --raw-data data/raw/source.csv \
  --trace-json /tmp/xau-session-features.json
```

The paths above refer to locally prepared inputs; this command does not download market data or
a calendar. `export-demo-replay-data` still exports a continuous synthetic dataset with manifest
v1, so its outputs do not supply the v2/calendar pair required here.

| Argument | Meaning |
| --- | --- |
| `csv` | Required normalized M15 bid/ask CSV with explicit UTC start, end, and availability. |
| `--manifest` | Required dataset manifest v2 binding exact raw and normalized bytes and calendar identity. |
| `--calendar` | Required local JSON calendar artifact, schema `xau_trader.session_calendar`, version 1. |
| `--raw-data` | Optional exact source artifact before normalization. Omit only when the CSV itself is the acquired source. |
| `--trace-json` | Required new output path for the complete feature trace. Existing files and input/output collisions are rejected. |

There is no `--overwrite` or `--decisions-csv` option. The command hashes inputs before and after
reading, rejects changed inputs, and publishes the trace only after validation and serialization.
Exit status `0` means the feature trace completed; invalid inputs or output collisions return `2`.
Short histories can complete with unseeded features, so success does not imply warm-up or strategy
readiness.

## Calendar evidence and binding

Manifest v2 retains the v1 dataset contract and expands `session_calendar` to exactly
`id`, `version`, `artifact_uri`, `artifact_sha256`, and `content_fingerprint`. The URI is redacted
metadata and is never fetched. The raw hash binds the exact local calendar file bytes; the
canonical fingerprint binds the parsed content. Both are required lowercase SHA256 digests.
Formatting changes can alter the raw hash while leaving the canonical fingerprint unchanged.

The calendar supplies an ID/revision, provider name and legal entity, `XAU_USD` product form,
finite UTC coverage, redacted source reference, retrieval time, and explicit closure intervals
with reasons. Calendar identity, provider, instrument, and product must match the dataset.
Calendar coverage must contain dataset coverage, and calendar retrieval must be no later than
dataset retrieval. A published calendar may describe a future period.
These checks do not prove the schedule was available at each historical feature time; the
supplied calendar is assumed known, separate from feature evidence's receipt-time gates.

Closures are half-open intervals, ordered, nonoverlapping, nonadjacent, and contained within
calendar coverage. Every closure in the calendar must start and end on a whole UTC hour for this
feature path. Each observed gap must equal a declared closure exactly; the dataset's `known_gaps`
must also equal its observed gaps. A missing open-session bar, a bar inside a closure, a partial
UTC hour, or an undeclared gap fails validation. No schedule, weekend, holiday, or DST rule is
inferred. The complete schema is in [DATA_FORMAT.md](DATA_FORMAT.md).

## Feature behavior across a closure

| Feature | Implemented behavior |
| --- | --- |
| Timeline and input | Keep every supplied open bar at its actual UTC timestamp; no synthetic gap fills, trimming, or timestamp compression. |
| H1 | Aggregate four actual M15 bars from the same complete UTC hour. No H1 candle covers a closed hour. |
| ATR(14) | Keep the Wilder seed and history. The previous traded close participates in true range, including a reopening price jump. |
| EMA(8) | Keep the SMA seed and recursive EMA state across the closure. |
| H1 pivots | Use three actual H1 bars on each side; a pivot becomes available only after its right-side dependencies are available. |
| Availability | Retain receipt times and expose a feature no earlier than its latest dependency receipt. |

The input uses one availability basis. Synthetic and historical-close bars require availability
equal to bar end. Observed receipts may be later, but receipt times must be non-decreasing in bar
order. These are feature evidence rules, not permission to trade at a reopening price.

## Output and readiness

The JSON trace records source bars, derived H1 bars, ATR/pivot/EMA events, observed closures, the
calendar, policy bundle, dataset/input/calendar/policy fingerprints, and a deterministic report
fingerprint. Its schema is `xau_trader.session_feature_replay`, version 1. Its status is
`session_features_complete`; `strategy_candidates_generated` and `full_strategy_replay_supported`
are both `false`, and `execution` is `null`.

Warm-up fields report whether ATR and EMA were seeded and whether seven H1 bars exist for a pivot
window. A window does not guarantee a pivot. `full_strategy_readiness_assessed` remains `false`.
The CLI prints counts, these warm-up fields, calendar binding, and the trace path.

Retain the sidecar manifest, original calendar file, normalized CSV, and raw source artifact with
the trace. The trace records the manifest fingerprint and canonical calendar content; the raw
calendar hash binding appears in the CLI summary and manifest. Keeping the sidecar and original
bytes allows that binding to be checked again.

The synthetic regression contains two 23-hour sessions separated by one one-hour closure:

| Evidence | Count |
| --- | ---: |
| Actual M15 bars | 184 |
| Derived H1 bars | 46 |
| EMA(8) events | 177 |
| ATR(14) events | 33 |
| Observed scheduled gaps | 1 |

Those counts show that feature history crosses the synthetic break. They do not establish that a
provider schedule is correct or that a strategy is ready. Calendar hashes verify identity and
consistency, not the truth of calendar evidence or permission to use data.

`replay-diagnostic` and `check-replay-data` remain strict manifest v1 consumers. That strategy
replay still requires gap-free input, at least 100 M15 bars, complete UTC hours, and its original
96-bar pre-roll. A separate [session diagnostic command](SESSION_DIAGNOSTIC_REPLAY.md) now runs
zones, lifecycle, regime, confirmations, and paper candidates across declared closures under a
calendar-bound provisional policy. It requires at least 100 actual M15 bars and both JSON and CSV
outputs; feature-only success does not substitute for that full replay or its validation.

This command and its output flags remain feature-only. No real dataset or Exness calendar has
been verified, no broker connector or order path is added, and no profit result is produced.
