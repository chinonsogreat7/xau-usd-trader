# Trader XAU/USD

A paper-only research system for converting permitted trading education into cited, deterministic
hypotheses and testing them without risking capital.

The project does **not** claim that a strategy will make money. AI components may propose rules;
they may not approve a strategy, size an order, change a running strategy, or contact a broker.

## Current milestone

The repository now contains the first executable foundation:

- immutable source, citation, explicit bar-start/bar-end, and strategy-candidate types;
- a provider-neutral transcript and strategy-extraction boundary;
- a bid/ask CSV loader with explicit availability time and acquisition basis, strict structural
  validation, cadence diagnostics, and a closed provider-neutral M15 dataset-manifest contract
  that binds provider/product/rights/calendar claims to exact raw and normalized hashes;
- a deterministic next-bar backtester with next-open risk revalidation, spread, slippage,
  commission, financing, staged reversals, forced liquidation, and conservative intrabar drawdown;
- simple no-trade, long, and moving-average baselines;
- a stateful risk gate with spread, daily-loss, drawdown, shorting, and sticky halt controls;
- a locked append-only experiment ledger with strict JSON and knowledge-cutoff validation;
- a provider-neutral, five-minute practice-data attestation plus an offline OANDA v20 request and
  candle-normalization boundary whose results retain request fingerprints and acquisition
  provenance, tested only with fake responses;
- a strict JSON-only StrategySpec v1 validator, canonical spec hashing, source/evidence binding,
  canonical transcript-excerpt hashing, immutable data-only compiler, code-bound feature manifest,
  and pure tri-state rule evaluator;
- a broker-disconnected multi-timeframe diagnostic foundation with strict contiguous M15-to-H1
  aggregation, H1 pivots that remain unavailable until all three right-hand confirmation bars are
  actually available, explicit no-default ATR/impulse policies, finite-lookback supply/demand zone
  formation, and immutable zone invalidation/retest episodes with dynamic point-in-time admission,
  explicit cadence anchors, sealed receipt groups, deterministic overlap selection, exact
  per-observation state snapshots, pivot-derived H1 regime, explicit M15 EMA/reversal-candle
  confirmation, and auditable `BUY`/`SELL`/`NO_TRADE` paper candidates that can only propose the
  next M15 open;
- a strict end-to-end diagnostic replay that binds the dataset and provisional policy fingerprints,
  preserves every warm-up `NO_TRADE`, rejects gaps/partial hours/late or out-of-order evidence,
  and exports a deterministic complete JSON evidence ledger plus one-row-per-M15 decision CSV;
- an offline `check-replay-data` command that reports temporal incompatibilities, gaps, complete-hour
  segment lengths, and optional manifest v1 binding before a strict strategy replay is attempted;
- a separate `replay-session-features` command with manifest v2 and an explicit finite calendar,
  preserving H1, ATR, pivot, and EMA history across exact declared whole-hour closures;
- a calendar-bound `replay-session-diagnostic` command extending that history through zone
  formation, lifecycle, regime, confirmation, and paper candidates under separately versioned,
  provisional closure rules;
- an offline `import-mt5-ticks` path for a narrow complete-bid/ask tick-export format, with an
  explicit fixed-offset import plan, finite calendar, manifest v2, and per-bar observation audit;
  it never invents missing quote sides or assumes imported opens were executable;
- a separate `import-mt5-session-ticks` path preserving exact partial-session M15/H1 observations
  and all source ticks, with the user-selected include-partials input policy; this emits an
  eligibility plan, not calculated indicators, strategy decisions, or executable prices;
- a separate offline quote-event bracket simulator with Decimal accounting, downward risk-based
  sizing, stop/target exits, gap fills, costs, expiry, and loss/staleness/kill-switch controls;
  its standalone CLI accepts scripted synthetic scenarios only; strategy candidates use the
  separate adapter below;
- a separate provisional structural-exit planner that derives confirmed M15 swings, structural
  stops and visible H1 target levels from a validated synthetic candidate replay, without creating
  orders or silently changing exact-next-open timing;
- a synthetic-only strategy-to-execution adapter that recomputes those plans, verifies a supplied
  quote tape against every M15 bid/ask OHLC, and simulates one-shot exact-boundary entry attempts
  with a complete decision trace; this is not yet a real-data or broker trading route; and
- dependency-free automated tests compatible with the workspace's Python 3.9 interpreter.

The first completely selected interpretation is recorded as an
[unapproved provisional diagnostic baseline](docs/PROVISIONAL_DIAGNOSTIC_BASELINE.md). Its policy
bundle is fingerprinted in code; it is a reproducible hypothesis for hand reconciliation, not an
approved strategy or evidence of profitability.

The declarative strategy language in [`docs/STRATEGY_SPEC.md`](docs/STRATEGY_SPEC.md) now has a
strict standard-library implementation. The older free-text Python `StrategySpec` remains only as
the backward-compatible alias `ExtractedStrategyCandidate`; the closed compiler never accepts it
or arbitrary Python strategy objects.

The compiled evaluator is deliberately diagnostic-only and is not connected to the P&L
backtester. The existing backtester cannot yet enforce v1 stop/target state, fixed-fractional
sizing, cooldown anchors, or quote-event ordering, so connecting them would give a false sense of
runtime parity.

The current `RiskEngine` is a historical-replay control, not a production paper-execution risk
service. Broker order submission is intentionally absent until decisions can be bound to an exact
practice account, instrument, quantity, quote, policy version, expiry, and reconciled state.

The new [offline bracket simulator](docs/PAPER_EXECUTION.md) exercises the missing position-management
mechanics separately. It does not change those compiler/backtester limitations or implement the
supply/demand strategy's structural exits. Its hypothetical P&L is an engineering fixture, not a
strategy-performance result.

The [structural exit planner](docs/STRUCTURAL_EXITS.md) calculates provisional structural
stop/target evidence separately. The [strategy paper adapter](docs/STRATEGY_PAPER.md) now joins
recomputed plans to supplied coherent synthetic quotes, nearest-target reward/risk checks,
position sizing, and simulated fills. Explicit [loss limits](docs/LOSS_LIMITS.md) now support
total losing closes per UTC day and weekly equity drawdown, with threshold-bound sizing and
latched halts. Their semantics remain provisional and their state is not persistent.
Real-data timing validation and broker execution are still pending. The original one-click demo remains
scripted and is not this strategy-driven adapter.

## Provider status

The user has created an Exness MT5 demo account, installed desktop MT5 on Mac, and reported a
successful terminal login. That does not connect this code to the account. A one-hour desktop
MT5 XAUUSDm format probe passed with 16,066 complete bid/ask ticks; this is not a production
dataset import or strategy validation. The
[MT5 import guide](docs/MT5_DATA_IMPORT.md) covers installation, the supported format, and the
provider/timezone/calendar evidence required. Real-data provenance and a dated historical
schedule still need validation, and this repository has no Exness connector or order-submission path.

OANDA is a **blocked candidate, not a selected broker**. No current official source establishes a
Nigerian-resident route to an OANDA entity that serves v20. Conflicting OANDA navigation and older
help content are insufficient, and both entity eligibility and data rights must be confirmed in
writing. The repository ships no OANDA credential loader or network transport; only immutable GET
request plans, mandatory five-minute preflight binding, and normalization tested with fake data.

Do not configure credentials in this project yet. The
[`provider decision`](docs/decisions/0001-practice-market-data-provider.md) and
[`confirmation request`](docs/OANDA_CONFIRMATION_REQUEST.md) apply if revisiting OANDA; OANDA
approval is not a prerequisite for an authorized local MT5 export. Never send an API token or
trading password through chat or commit it to this repository.

## Run it

No packages need to be downloaded for the current milestone.

On this Mac, double-click **Run Paper Demo.command** in the project folder for a
readable offline demonstration. Or run:

```bash
PYTHONPATH=src python3 -m xau_trader run-paper-demo
```

Each run creates a new folder under `reports/` with a readable summary, complete
simulation report, and replayable synthetic scenario. It exits when finished;
it is not a live bot or a connection to the account. See
[how to run the project](docs/RUNNING_THE_BOT.md) for the recommended future
Windows/MT5 demo setup and the distinction between a computer and a phone.

Run the test suite or older baseline example:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m xau_trader demo --bars 240
```

The demo data are deterministic and synthetic. Its result proves only that the plumbing runs.

Run the new simulated trade-management demonstration (use a new output filename each time):

```bash
PYTHONPATH=src python3 -m xau_trader demo-paper-execution --report-json /tmp/xau-paper-report.json
```

This scripts a target exit, a stop-gap loss, spread/conflict rejections, and an expired entry.
It saves every simulated fill and account event, but does not contact Exness or run the candidate
strategy. See [the paper execution guide](docs/PAPER_EXECUTION.md) to export and edit the scenario.

Export and validate the fake CSV workflow:

```bash
PYTHONPATH=src python3 -m xau_trader export-demo-data /tmp/xau-demo.csv --bars 240
PYTHONPATH=src python3 -m xau_trader validate-data /tmp/xau-demo.csv
PYTHONPATH=src python3 -m xau_trader backtest /tmp/xau-demo.csv
```

The accepted market-data schema is documented in
[`docs/DATA_FORMAT.md`](docs/DATA_FORMAT.md).

For a local MT5 tick export, use the separate
[`offline import guide`](docs/MT5_DATA_IMPORT.md). It requires both quote sides on every tick,
an explicit historical UTC offset, a provenance plan, and a calendar. Ordinary bid-only chart
exports are not compatible. Import success describes supplied observations, not complete history,
executable next-open prices, strategy performance, or readiness to place demo trades.

For closures inside a candle, use the separate
[partial-session importer and inclusion policy](docs/PARTIAL_CANDLE_IMPORT.md).
It retains short observed candles and marks them eligible under the selected research rule,
without rounding the calendar. Full partial-session strategy replay is not wired up yet.

Strictly validate or compile a StrategySpec v1 JSON file:

```bash
PYTHONPATH=src python3 -m xau_trader validate-strategy strategy.json
PYTHONPATH=src python3 -m xau_trader validate-strategy strategy.json --promotable
PYTHONPATH=src python3 -m xau_trader compile-strategy strategy.json
```

Compilation requires a frozen, fully resolved spec and emits hash metadata only. It does not
approve the strategy, predict profit, run a backtest, or create a broker action. See the
[`structured learning workflow`](docs/STRATEGY_WORKFLOW.md).

Run the new M15 diagnostic path first on the included deterministic fixture plumbing:

```bash
PYTHONPATH=src python3 -m xau_trader export-demo-replay-data \
  /tmp/xau-m15.csv --manifest /tmp/xau-m15.manifest.json --bars 480

PYTHONPATH=src python3 -m xau_trader replay-diagnostic \
  /tmp/xau-m15.csv \
  --manifest /tmp/xau-m15.manifest.json \
  --trace-json /tmp/xau-trace.json \
  --decisions-csv /tmp/xau-decisions.csv
```

The first command truthfully labels both artifacts as locally generated synthetic data. The second
validates their hashes and provenance, replays the provisional policy, and writes research evidence
only. It does not simulate entries, create orders, calculate P&L, or establish profitability.
For the current bundle, the first 96 M15 decisions are explicitly labeled `pre_roll`; at least 100
whole-hour bars are required so the artifact contains post-pre-roll decisions. The replay also
records that zone lifecycle state starts empty at the dataset boundary, so it does not pretend to
know zones formed before the file begins.

For an authorized real dataset, provide the exact M15 CSV and its sidecar manifest. If the acquired
raw artifact differs from the normalized CSV, also pass `--raw-data path/to/raw-artifact`; otherwise
the command explicitly treats the CSV itself as both raw and normalized bytes. Output files are
collision-protected unless `--overwrite` is supplied. See [`docs/DATA_FORMAT.md`](docs/DATA_FORMAT.md)
for the closed manifest v1 contract.

Inspect a normalized CSV before generating evidence artifacts:

```bash
PYTHONPATH=src python3 -m xau_trader check-replay-data data/processed/xau-m15.csv
PYTHONPATH=src python3 -m xau_trader check-replay-data data/processed/xau-m15.csv \
  --manifest data/processed/xau-m15.manifest.json --raw-data data/raw/source.csv
```

Without a manifest, the command reports technical compatibility but cannot report complete inputs.
Exit status is `0` for technically compatible, manifest-bound inputs, `1` for incomplete/incompatible
inputs, and `2` for malformed files, changed inputs, or failed manifest binding. It does not execute
the strategy, repair data, or verify the truth of a provider's rights claims.

For feature calculations across scheduled closures, supply a manifest v2 and its local calendar:

```bash
PYTHONPATH=src python3 -m xau_trader replay-session-features \
  data/processed/xau-m15.csv \
  --manifest data/processed/xau-m15.manifest-v2.json \
  --calendar data/raw/xau-calendar.json \
  --raw-data data/raw/source.csv \
  --trace-json /tmp/xau-session-features.json
```

This command produces H1 bars, ATR(14), confirmed H1 pivots, and M15 EMA(8). It retains real
timestamps and supplied history, without filling closures with synthetic bars. It requires a new
output path and has no `--overwrite` or decision-CSV option. See the
[session feature replay guide](docs/SESSION_FEATURE_REPLAY.md) for the calendar contract and flags.

For the full offline candidate trace across those same declared closures:

```bash
PYTHONPATH=src python3 -m xau_trader replay-session-diagnostic \
  data/processed/xau-m15.csv \
  --manifest data/processed/xau-m15.manifest-v2.json \
  --calendar data/raw/xau-calendar.json \
  --raw-data data/raw/source.csv \
  --trace-json /tmp/xau-session-trace.json \
  --decisions-csv /tmp/xau-session-decisions.csv
```

Both outputs are required and must be new paths. This separate provisional bundle preserves
zones and active retest episodes across closures, counts indicator/formation windows in actual
open bars, and lets confirmation expiry continue in elapsed time. A closing-bar candidate is
cancelled, never queued for reopening; an unverified next open beyond calendar coverage also
produces `NO_TRADE`. The first 96 actual M15 rows remain pre-roll even across a break. See the
[session diagnostic replay guide](docs/SESSION_DIAGNOSTIC_REPLAY.md) for exact scope and artifacts.
Historical availability of the supplied schedule is assumed, not verified; point-in-time market
evidence gates do not prove that the calendar was known at those times.

The current synthetic regression uses 184 M15 bars across two 23-hour sessions and one one-hour
closure. It produces 46 H1 bars, 12 zones, 4 confirmations, and 184 paper decisions: 96 pre-roll
and 88 post-pre-roll. These are plumbing checks, not performance results. The feature-only
command remains feature-only; `replay-diagnostic` and `check-replay-data` remain strict manifest
v1 consumers with unchanged gap-free requirements. No real dataset or Exness calendar has been
verified, and no broker connector or order path has been added. See the
[data intake checkpoint](docs/DATA_INTAKE.md).

Keep the first licensed run deliberately small for manual reconciliation. The complete JSON trace
embeds bid/ask bars and repeated lifecycle snapshots, so it can be large and can itself constitute
stored or derived market data. Confirm the provider's retention and derived-data rights before
creating or sharing it; large-history streaming/chunked trace storage is not implemented yet.

## Project boundaries

- Instrument default: `XAU_USD`.
- Research timeframe default: one hour.
- Execution environment: paper/simulated only.
- Content: creator-provided, user-supplied, owned, licensed, or otherwise explicitly permitted.
- Strategy changes: new immutable version followed by all evaluation gates.
- Live money, client funds, personalized signals, copy trading, and performance marketing: out of
  scope.

Read these before expanding the system:

- [`docs/PROJECT_CHARTER.md`](docs/PROJECT_CHARTER.md)
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- [`docs/EVALUATION.md`](docs/EVALUATION.md)
- [`docs/RISK_POLICY.md`](docs/RISK_POLICY.md)

## Next milestone

1. Complete historical UTC offset, source identity, rights and calendar evidence for the
   supplied MT5 sample. Its format probe passed. Partial-session import and the user-selected
   inclusion plan are implemented; the next code step is an explicitly versioned full-strategy
   adapter with partial H1/warm-up/cadence and entry-boundary tests. Do not pass that new bundle
   to the old replay commands or disable their guards. See the
   [partial-session guide](docs/PARTIAL_CANDLE_IMPORT.md). Independently compare the
   derived candles and tick-observation audit; sparse ticks do not prove executable next-open prices.
   For an already normalized strict v1 dataset, use `check-replay-data`. For feeds with whole-hour closures,
   use a verified finite calendar
   and manifest v2 with `replay-session-diagnostic`. Independently reconcile the implemented
   features, zones, lifecycle, elapsed confirmation expiry, and cancelled closing-bar candidates.
   The session interpretation remains an unapproved hypothesis; record mismatches before any
   P&L simulation. See [data intake](docs/DATA_INTAKE.md).
2. Ingest an authorized timestamped transcript for the cited video, store immutable
   content/transcript hashes, and distinguish verified source claims from operator hypotheses.
3. Persist a reviewed, versioned transcript/evidence index around canonical excerpt hashes, then
   test the first real draft with adversarial missing/ambiguous-rule cases.
4. Resolve and freeze structural stop/target, cost, quote-ordering, sizing, and account-risk
   semantics, then build a separate event-driven compiled-plan simulator that enforces stops, targets, position
   state, fixed-fractional sizing, quote sequence, TTL, and risk ceilings before any compiled rule
   can affect paper state.
5. Before a broker connector, verify the selected provider's exact entity, eligibility, supported
   demo-automation route, gold product, and data-use rights. If revisiting OANDA, obtain and retain
   its outstanding entity, Nigerian eligibility, v20 practice API, and data/ML confirmations.
6. Only after that gate, add an isolated practice-only network transport, validated local approval
   record, secret-manager integration, credentialed export, raw-response audit metadata, and cursor
   pagination; bind every resulting snapshot through the dataset manifest already implemented.
7. Acquire a small licensed dataset, reconcile several trades by hand, and record every baseline
   and candidate run automatically in the experiment ledger.
