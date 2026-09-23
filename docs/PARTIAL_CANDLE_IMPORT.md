# Partial-session candle import and research policy

Implemented on 2026-09-23: a separate offline importer preserves exact session
edges, all supplied ticks, and observed partial M15/H1 candle inputs. The user
explicitly selected **include partial candles in indicators and signals**.

This milestone implements an **input-eligibility policy**, not the full strategy
adapter: it does not calculate EMA/ATR, evaluate BUY/SELL decisions, simulate
returns, connect to MT5, or submit orders. Existing replay commands still reject
partial sessions. The new artifact cannot be passed off as their manifest v2.

## Run the import

Use the same strict source/rights/UTC-offset plan as the
[MT5 import guide](MT5_DATA_IMPORT.md), with a finite dated calendar containing
the actual closure boundaries. Do not round closures or invent missing metadata.

```bash
PYTHONPATH=src python3 -m xau_trader import-mt5-session-ticks \
  data/raw/gold-ticks.csv \
  --plan data/raw/gold-import-plan.json \
  --calendar data/raw/gold-calendar.json \
  --output-json reports/gold-session-inputs.json
```

The output must be a new file. No input is overwritten. Exact raw/plan/calendar
byte hashes and canonical metadata fingerprints are recorded and the inputs
are rechecked before publication. Limits remain 64 MiB / 500,000 input ticks,
1 MiB per metadata file, and at most 100,000 nominal M15 slots. Overall coverage
begins/ends on UTC hours; closure edges may have microsecond precision.

The JSON bundle (`xau_trader.mt5_session_bucket_import`, version 1) contains:

- every parsed tick in original order, including equal timestamps and optional
  Last/Volume/Flags values; zero dropped or invented ticks;
- exact open/closed intervals inside every nominal M15 slot, open duration,
  observed bid/ask OHLC, tick counts, first/last ticks and open-segment silence;
- separate `full_open`, `partial_open`, or `closed` schedule classification and
  `observed`, `missing_open_data`, or `closed` observation status;
- the user-selected, fingerprinted M15/H1 inclusion plan and explicit
  `strategy_replay_executed: false`, `broker_connected: false`, `orders_submitted: 0`.

Every scheduled-open fragment must contain a tick to qualify as observed. A
partly observed bucket retains its actual ticks/OHLC but is marked missing and
ineligible. One observed fragment cannot conceal another empty fragment. Empty
open data is reported, not filled or reclassified as a market closure. A tick
inside a declared closure rejects the import because the evidence conflicts.
One tick per fragment is a structural check, **not** proof of a complete feed.

## Selected interpretation

Policy: `include-observed-partial-candles-v1`, an explicit research hypothesis.

| Case | Interpretation |
| --- | --- |
| Observed partial M15 | Include once, with the same bar weight as a full candle. Use actual OHLC without scaling the range/body or padding prices. |
| Observed partial H1 | Combine all scheduled-open M15 children in that nominal UTC hour. Include once, even if fewer than four children trade. Every open child/fragment must be observed. |
| Fully closed slot | No candle and no indicator update. Never fabricate a flat bar. |
| Missing open fragment | Mark its M15/H1 inputs ineligible and set the global missing-data replay block. Do not silently skip it. |
| Availability | Use the nominal M15/H1 end, not the last tick or the early session close. For exports this is a disclosed historical assumption, not measured live receipt time. |
| Indicator history | Keep prior observations across documented closures; no reset, duration weighting or compressed timestamps. |
| Signal rules | Eligible partial inputs must satisfy the same complete strategy rules and warm-up dependencies. Eligibility alone is not a BUY/SELL signal. |
| Entry boundary | Independently check the next nominal M15 boundary against actual closures. Closed-time entries are not queued for reopening. An open boundary alone does not prove an executable quote. |

For example, a supplied 20:58 close leaves **780 seconds** of open time in
20:45–21:00. That observed partial candle is eligible at 21:00, but an entry at
21:00 is blocked while the market is closed. A supplied 22:01 reopening leaves
**840 seconds** in 22:00–22:15 and becomes eligible at 22:15. These are boundary
examples, not a claim that one verified broker session closes and reopens at
both of those times on a particular date.

The policy distinguishes `next_nominal_entry_boundary_open` (calendar fact)
from `next_nominal_entry_input_eligible` (observed input plus open boundary).
Neither bypasses global missing-data checks, warm-up, strategy rules or quote
execution validation. No entry boundary outside the imported coverage is
declared eligible.

This concerns **completed nominal slots shortened by a scheduled closure**.
It does not enable signals on the live, still-forming current candle, infer a
closed market from missing ticks, or guess a broker's dated holiday schedule.

## Verification and next integration

Synthetic regressions cover minute/microsecond boundaries, multiple fragments,
missing observations, preserved tied ticks, H1 aggregation without fabricated
children, price/metadata tampering, deterministic fingerprints, future-price
noninterference, exact reopening and closed-entry boundaries. Existing whole-hour
import/replay and synthetic-only execution restrictions remain unchanged.

Before the larger historical **strategy** test, wire this explicit policy into
the diagnostic pipeline: count accepted nominal observations rather than open
minutes divided by 15/60, aggregate partial H1 dependencies, separate observation
cadence from tradable entry instants, and recompute warm-up from actual H1/M15
dependencies. Reconcile EMA/ATR, zone/lifecycle, confirmation and candidate traces
against hand calculations and prefix/as-of tests. Do not merely disable existing
whole-hour guards. The old 100-M15-bar minimum must not be blindly reused for
hours with fewer observed children.

The existing real one-hour sample is a format probe, not strategy validation.
Complete its truthful provenance and dated session evidence before a production
import; plan bounded multi-file history support if the larger export exceeds
the single-file limits. Broker execution remains separate.
