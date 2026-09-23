# Real XAU/USD data intake checkpoint

Initial source checkpoint: 2026-09-05. Latest intake update: 2026-09-23.
Status: calendar-bound feature and end-to-end paper-candidate replay plus a narrow offline MT5
quote-tick importer implemented. The first real Exness raw-file format probe passed; full
provenance/calendar-bound intake and real-data execution remain pending.
See [the first-sample findings](EXNESS_FIRST_SAMPLE.md) for the verified scope and next work.
The provider references below retain the initial checkpoint's
findings and have not been reverified by this implementation work.

The immediate objective is a small reproducible comparison of the diagnostic decisions against
real candles. AlphaLedger integration is deferred and is not a dependency of this milestone.

## Constraint identified in the strict replay path

The provisional policy needs 96 M15 bars of history and at least 100 input bars to include
post-pre-roll decisions. The original `replay-diagnostic` path requires contiguous, complete UTC hours.

[Dukascopy's official schedule](https://www.dukascopy.com/swiss/english/forex/forex-trading-accounts/link/)
lists a daily XAU/USD break at 21:00–22:00 GMT in summer and 22:00–23:00 in winter, with additional
holiday closures. This implies a normal continuous session of 23 hours, or 92 M15 bars. Buying or
downloading more days alone will not satisfy the current continuous-history requirement.

The strict path therefore cannot replay a normal multi-session Dukascopy gold sample. Resetting
at each daily break would repeatedly restart warm-up. Filling the break with artificial candles
would change ATR, EMA, pivots and supply/demand rules. Neither approach establishes the intended
strategy's behaviour. Other feeds must be checked against their own schedules and actual data;
the Dukascopy calendar must not be assumed universal. The new `replay-session-diagnostic` path
handles explicitly declared whole-hour closures without either workaround; admission still
requires a verified, dataset-bound finite calendar and an authorized source sample.

## Source candidates checked

These are historical-data candidates, not broker selections or completed data entitlements.

| Source | Available evidence | Remaining work |
| --- | --- | --- |
| Dukascopy Bank SA | Its [historical export](https://www.dukascopy.com/swiss/english/marketwatch/historical/) offers free CSV data and explicitly describes backtesting uses. Gold trading hours are documented above. | Its [general terms](https://www.dukascopy.com/swiss/english/legal-pages/terms-of-use/) permit personal downloads but also restrict database construction. Confirm the applicable data terms for keeping raw/derived research artifacts, bind a verified calendar, and reconcile the implemented session-aware strategy hypothesis. |
| HistData | Its [FAQ](https://www.histdata.com/f-a-q/) lists XAU/USD. Generic ASCII tick data contains bid and ask; ordinary candle files are bid-only. Its [format specification](https://www.histdata.com/f-a-q/data-files-detailed-specification/) uses fixed EST, UTC−05:00, without DST. | Confirm source identity, applicable retention terms and session evidence; implement a source-specific tick importer and aggregate both sides into M15. A successful download has not been verified. |
| An existing broker export supplied by the owner | The user's one-hour XAUUSDm export passed the narrow MT5 complete-quote parser on 2026-09-23. | Complete account-specific contract, source/rights and dated calendar evidence; check partial-hour session handling before a larger import. Full Exness replay compatibility is not yet established. |

No dataset was acquired, no account was created, and no provider was contacted during the
initial source-candidate check. The subsequent user-supplied Exness sample has passed raw
format checks only. No complete Exness schedule has been verified. The implementation adds
no broker connector, orders, or profitability evidence.

## Exness MT5 desktop format probe

The user has connected desktop MT5 to their Exness demo account and exported the first sample.
That terminal login does not connect or activate this Python bot. For subsequent exports,
follow the [offline MT5 import guide](MT5_DATA_IMPORT.md), sign into the demo account yourself,
and keep Algo Trading off. Do not share credentials or account numbers.

`import-mt5-ticks` now normalizes a supported local complete-bid/ask tick export using an explicit
import plan and a finite calendar. It produces a M15 CSV, manifest v2, and an observation report
without network access or orders. The parser rejects missing/zero bid or ask, out-of-coverage
rows, empty open M15 intervals, undeclared gaps, and partial-hour closures. It does not synthesize
ask candles from a chart spread, forward-fill ticks, or guess historical daylight-saving offsets.

Begin with a completed one-hour format probe and retain the unedited export plus symbol, timezone,
rights, and schedule evidence. Passing aggregation only shows that supplied ticks populate every
open bucket. The first observed tick may be later than the nominal bar start; the resulting open
price is **not** proven executable at the strategy's next-open time. Tick completeness and P&L
validity remain unverified. A later session strategy replay needs at least 100 actual M15 bars,
so the initial one-hour probe cannot satisfy its warm-up requirement.

## Inspect a normalized sample

Keep the exact source artifact in `data/raw/` and the normalized CSV in `data/processed/`; both
directories are ignored by Git. The accepted normalized format is in [DATA_FORMAT.md](DATA_FORMAT.md).
The normalized CSV loader does not automatically recognize vendor formats; the separate MT5
import command accepts only the explicitly documented tick format.

```bash
PYTHONPATH=src python3 -m xau_trader check-replay-data data/processed/xau-m15.csv
```

This read-only command reports every detected temporal issue and missing interval, the current
policy's warm-up requirement, and each continuous segment's complete-hour row count. Segment
counts are informational; no rows are removed or changed. Even a long enough individual segment
does not make a file with gaps compatible.

When the provenance is complete:

```bash
PYTHONPATH=src python3 -m xau_trader check-replay-data data/processed/xau-m15.csv \
  --manifest data/processed/xau-m15.manifest.json --raw-data data/raw/source.csv
```

Omit `--raw-data` only when the normalized CSV itself is the exact acquired source. The manifest
must be v1 and bind those exact bytes, coverage and gap declarations. A valid binding checks the consistency
of recorded claims, not independently verified rights. Exit codes: `0` means compatible inputs
with a valid binding; `1` means technical incompatibility or a missing manifest; `2` means invalid
arguments/files, a changed input, or failed manifest binding.

For a synthetic command-line smoke test only:

```bash
PYTHONPATH=src python3 -m xau_trader export-demo-replay-data \
  data/processed/preflight-demo.csv --manifest data/processed/preflight-demo.manifest.json --bars 100
PYTHONPATH=src python3 -m xau_trader check-replay-data \
  data/processed/preflight-demo.csv --manifest data/processed/preflight-demo.manifest.json
```

The regression suite also uses two synthetic 92-bar sessions separated by a one-hour break. It
confirms that 184 total bars with a truthful manifest still fail continuous replay compatibility.

## Implemented session feature path

`replay-session-features` accepts manifest v2 plus a local, explicit finite calendar bound by raw
file hash and canonical content fingerprint. It rejects undeclared gaps and bars during closures.
For this feature path, every closure must start and end on a whole UTC hour, and H1 bars require
four actual M15 bars in that hour. Real timestamps and indicator history are retained across
closures; gaps are never filled with fabricated candles.

The same synthetic two-session shape produces 46 H1 bars, 177 EMA(8) events, and 33 ATR(14) events
from 184 M15 bars. Confirmed pivots use three actual H1 bars on each side. These are feature
regressions, not a completed market-data or strategy validation. The trace explicitly records
`full_strategy_readiness_assessed: false`.

See [the feature replay guide](SESSION_FEATURE_REPLAY.md) for the command, calendar requirements,
and output. `check-replay-data` and `replay-diagnostic` remain strict v1 consumers; neither accepts
a v2 manifest or treats feature completion as full strategy readiness.

## Implemented session diagnostic path

`replay-session-diagnostic` uses the same manifest v2/calendar binding, but runs the entire offline
candidate pipeline under `provisional_session_diagnostic_baseline_v1`. Calendar content and the
`xauusd-session-strategy-v1` interpretation participate in every relevant policy identity.
Indicator, impulse, origin-search, and pivot windows count actual open bars. Zones and active
touch episodes persist across closures until the next applicable observed exit/invalidation.
Confirmation expiry counts elapsed M15 intervals, including closed time. Closing-bar signals
are cancelled rather than queued for reopening; an uncovered next open is also `NO_TRADE`.

The current synthetic fixture produces 46 H1 bars, 12 zones, 4 confirmations, and 184 decisions
from 184 actual M15 bars. The first 96 decisions are pre-roll and the remaining 88 are
post-pre-roll, regardless of the intervening break. This demonstrates pipeline behavior only;
it does not validate a provider schedule or measure strategy returns.

The command requires both a new JSON trace path and a new decision CSV path and never overwrites
existing artifacts. See [the session diagnostic guide](SESSION_DIAGNOSTIC_REPLAY.md). The older
strict commands and feature-only command retain their respective scopes.

## Remaining validation and acquisition dependencies

Before using session decisions as evidence about an actual feed:

1. Acquire an authorized sample and verify its exact provider/product, rights, time convention,
   and dated finite calendar, including applicable holiday/DST changes. Bind those artifacts with
   manifest v2 and independently reconcile the complete event trace. Historical schedule
   availability is an assumption in this replay: its `as_of` gates market evidence, not calendar
   retrieval, so calendar hashes alone cannot establish that a schedule was known at decision time.
2. Review the provisional session interpretation, particularly persistent zones/touch episodes,
   elapsed-time confirmation expiry, and closing-bar cancellation. These choices are implemented
   and versioned but are not source-verified or approved strategy rules.
3. Integrate the [new partial-session import/inclusion plan](PARTIAL_CANDLE_IMPORT.md)
   into a separately versioned strategy replay. Both existing session replay commands still
   reject partial UTC hours; the new import artifact does not disable those restrictions.
4. Resolve execution, structural stops/targets, sizing, costs and account-risk semantics before
   extending these diagnostic candidates to a separate P&L simulator or demo-order connector.

The real Exness raw-file format probe passed, but production dataset/calendar admission and
a bot-to-broker connection remain unvalidated. Input preflight, feature completion, and
synthetic candidate replay do not establish profitability or permission to submit trades.

## Manual comparison record for the first authorized replay

For each zone formation, invalidation, retest, regime change and candidate, record the bar time,
event/zone key, relevant candle values, independently calculated expectation, replay result and
match/mismatch explanation. Retain the source, data hashes, policy fingerprint and code revision.
Unexplained differences remain failures; this milestone produces decision evidence, not returns.
