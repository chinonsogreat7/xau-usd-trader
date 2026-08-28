# Market data format

The research engine accepts UTF-8 CSV files with bars in strictly increasing UTC order.
Timestamps must contain an explicit offset; `Z` is accepted. Duplicate or naive timestamps are
rejected instead of being silently repaired.

For the current engine, each bar represents the half-open interval `[start_time, timestamp)`.
`timestamp` is the completed bar's end/decision time. A strategy may use that bar only at this
timestamp, and a resulting order can fill no earlier than the next row's `start_time` and executable
open. A vendor using open timestamps must retain that value as `start_time` and derive the end.

Required columns:

```text
start_time,timestamp,available_at,availability_basis,bid_open,bid_high,bid_low,bid_close,ask_open,ask_high,ask_low,ask_close
```

`available_at` and `availability_basis` are required. `available_at` records when the completed bar
became usable and cannot be earlier than `timestamp`. The basis must be `synthetic`,
`historical_close_assumption`, or `observed_receipt`; it cannot be silently defaulted. The current
historical backtester accepts the first two only when availability equals the bar end. It rejects
observed-receipt bars because it does not yet model event-time arrival and cannot safely assume the
next open remained tradable. Non-negative `volume` is optional.

Bid/ask bars are required because a
mid-price-only backtest hides the spread. Each high must be at least its open and close, each low
must be at most its open and close, and bid cannot exceed ask.

Example:

```csv
start_time,timestamp,available_at,availability_basis,bid_open,bid_high,bid_low,bid_close,ask_open,ask_high,ask_low,ask_close,volume
2026-01-02T09:00:00Z,2026-01-02T10:00:00Z,2026-01-02T10:00:00Z,historical_close_assumption,2640.10,2642.30,2639.20,2641.80,2640.40,2642.60,2639.50,2642.10,1200
```

The loader does not download or repair data. Vendor provenance, instrument definition, timezone,
licence, missing bars, rollover, and financing assumptions must be recorded alongside every
dataset before it can support a promoted experiment.

For the blocked OANDA candidate mapping, vendor candle `time` is the interval start and is
normalized to the H1 completion time by adding one hour. Its `volume` field counts prices created during the
interval; it is not exchange-traded gold volume or available liquidity. Historical
`available_at=timestamp` with `availability_basis=historical_close_assumption` is an explicit
close-time availability assumption, not a measurement of network delivery latency. This dataset
cannot count as forward-paper evidence. Observed OANDA bars instead retain receipt provenance and
are returned inside an account/instrument-attested envelope rather than as backtest-ready history.

## Dataset sidecar manifest v1

Every admitted M15 dataset requires a JSON sidecar accepted by the strict
`xau_trader.dataset_manifest` v1 validator. The schema is closed: every field below is required and
unknown fields are rejected, including unknown fields in nested objects. `known_gaps` may be an
explicitly empty array; `request_parameters` must contain at least one non-empty string key/value
pair.

The manifest is provenance metadata, not a secret store. `account_environment` must be a redacted
description, never an account number. `source.route` must omit URL user information, query strings,
fragments, and authorization headers. Put only non-secret, redacted request fields in
`request_parameters`; credential-like names such as tokens, API keys, passwords, secrets,
authorization, cookies, and private keys are rejected. Never place a credential in a parameter
value, even under an innocuous name.

```text
schema
version
provider { name, legal_entity }
instrument { symbol, product_form }
source {
  route, account_environment, acquisition_basis, request_parameters,
  time_normalization_rule, retrieved_at
}
bars {
  timeframe, duration_seconds, timezone, interval, timestamp,
  availability_basis
}
hashes { raw_sha256, normalized_csv_sha256 }
rights { basis, api_data_agreement_version, retention_basis }
session_calendar { id, version }
coverage { start, end, row_count, known_gaps[] { start, end, reason } }
```

The current contract accepts only `instrument.symbol=XAU_USD`, `timeframe=M15`, 900-second bars,
`timezone=UTC`, half-open intervals, and completed-bar-end timestamps. Acquisition basis must be
one of `api_response`, `user_export`, `provider_download`, or `synthetic_generation`. Availability
basis uses the three CSV values documented above. These closed values describe what this engine can
validate; they do not determine which provider, product form, rights basis, or session calendar is
legally or operationally correct.

Here is a complete synthetic-fixture example. Its values identify a local test-data process, not a
broker dataset or permission to use one.

```json
{
  "schema": "xau_trader.dataset_manifest",
  "version": 1,
  "provider": {
    "name": "Local deterministic generator",
    "legal_entity": "Research project owner"
  },
  "instrument": {
    "symbol": "XAU_USD",
    "product_form": "synthetic bid/ask spot-price series"
  },
  "source": {
    "route": "local-generator://xau-m15-fixture-v1",
    "account_environment": "local research process with no brokerage account",
    "acquisition_basis": "synthetic_generation",
    "request_parameters": {
      "bar_count": "4",
      "generator_version": "fixture-v1"
    },
    "time_normalization_rule": "native UTC quarter-hour boundaries; no conversion",
    "retrieved_at": "2026-01-02T00:00:00Z"
  },
  "bars": {
    "timeframe": "M15",
    "duration_seconds": 900,
    "timezone": "UTC",
    "interval": "start_inclusive_end_exclusive",
    "timestamp": "completed_bar_end",
    "availability_basis": "synthetic"
  },
  "hashes": {
    "raw_sha256": "72fad6a79606ae897b6b7f5962debf3a25498785a32ec02c66dca06a67911c0b",
    "normalized_csv_sha256": "b807c111baa09ce6fb1e0819ed50b083edba0684b73a00608fe713d4a2b11d36"
  },
  "rights": {
    "basis": "self-generated research fixture",
    "api_data_agreement_version": "project-owned-fixture-policy-v1",
    "retention_basis": "project-owned fixture retained with tests"
  },
  "session_calendar": {
    "id": "synthetic-continuous-utc",
    "version": "fixture-v1"
  },
  "coverage": {
    "start": "2026-01-01T00:00:00Z",
    "end": "2026-01-01T01:00:00Z",
    "row_count": 4,
    "known_gaps": []
  }
}
```

`raw_sha256` identifies the exact bytes acquired from the declared source before normalization.
`normalized_csv_sha256` identifies the exact UTF-8 CSV bytes loaded by this project after field and
time normalization. They may be equal when the acquired file already is the normalized file, but
neither value may be recomputed from parsed floats or silently substituted for the other. A caller
must hash and load the same byte snapshot to avoid a file changing between those operations.

Binding validation receives the manifest, the actual loaded `QuoteBar` sequence, and both
caller-computed hashes. It checks the hashes, row count, first start, final end, exact 15-minute UTC
alignment and duration, availability basis, retrieval time, and every observed missing interval.
Declared gaps must be ordered, M15-aligned, strictly inside coverage, and match observed gaps
exactly; the validator does not infer weekends, holidays, outages, or repairs. Coverage duration
must equal `row_count * 900` plus the declared gap durations. The canonical manifest JSON produces
a stable SHA256 fingerprint and dataset identity.

For `synthetic` and `historical_close_assumption`, every loaded bar must have
`available_at=timestamp`. `observed_receipt` may be later than the bar end, but the manifest's
retrieval time cannot precede any recorded receipt time.

A manifest with truthful declared gaps can pass provenance binding, but the current diagnostic
replay rejects any non-empty `known_gaps` array. Its multi-timeframe kernel still requires strict
contiguity and has no session-segmentation implementation; a weekend or scheduled closure must not
be smuggled through as if it were a continuous trading interval.

The provisional diagnostic bundle also requires at least 100 contiguous M15 bars, beginning and
ending on complete UTC hours. The first 96 decision rows are retained but labeled `pre_roll`; only
the later rows enter post-pre-roll action counts. Lifecycle state is explicitly empty at the
dataset start, so the replay makes no claim about zones formed before the supplied history.

Manifest v1 records a calendar ID and version as claims; it does not yet bind a calendar artifact
URI and hash. A future replay that trusts scheduled gaps must add that content-addressed calendar
artifact before relaxing the current no-gap restriction.

The raw-response audit and credentialed export remain unimplemented and blocked independently of
the offline sidecar validator.

`validate-data` performs structural validation and reports cadence gaps against an expected
interval. It deliberately labels the result `structurally_valid`, not strategy-ready: weekend and holiday
interpretation requires a versioned session calendar from the selected broker.
