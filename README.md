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
  and exports a deterministic complete JSON evidence ledger plus one-row-per-M15 decision CSV; and
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

## Provider status

OANDA is a **blocked candidate, not a selected broker**. No current official source establishes a
Nigerian-resident route to an OANDA entity that serves v20. Conflicting OANDA navigation and older
help content are insufficient, and both entity eligibility and data rights must be confirmed in
writing. The repository ships no OANDA credential loader or network transport; only immutable GET
request plans, mandatory five-minute preflight binding, and normalization tested with fake data.

Do not configure credentials yet. Read the
[`provider decision`](docs/decisions/0001-practice-market-data-provider.md) and use the
[`confirmation request`](docs/OANDA_CONFIRMATION_REQUEST.md) first. Never send an API token through
chat or commit it to this repository.

## Run it

No packages need to be downloaded for the current milestone.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m xau_trader demo --bars 240
```

The demo data are deterministic and synthetic. Its result proves only that the plumbing runs.

Export and validate the fake CSV workflow:

```bash
PYTHONPATH=src python3 -m xau_trader export-demo-data /tmp/xau-demo.csv --bars 240
PYTHONPATH=src python3 -m xau_trader validate-data /tmp/xau-demo.csv
PYTHONPATH=src python3 -m xau_trader backtest /tmp/xau-demo.csv
```

The accepted market-data schema is documented in
[`docs/DATA_FORMAT.md`](docs/DATA_FORMAT.md).

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

1. Acquire a small licensed, gap-free M15 XAU/USD sample with the exact raw artifact and completed
   manifest, run the diagnostic replay, manually reconcile every formation, lifecycle, regime,
   confirmation, and candidate decision, and record mismatches before any P&L simulation.
2. Ingest an authorized timestamped transcript for the cited video, store immutable
   content/transcript hashes, and distinguish verified source claims from operator hypotheses.
3. Persist a reviewed, versioned transcript/evidence index around canonical excerpt hashes, then
   test the first real draft with adversarial missing/ambiguous-rule cases.
4. Resolve and freeze structural stop/target, cost, quote-ordering, sizing, and account-risk
   semantics, then build a separate event-driven compiled-plan simulator that enforces stops, targets, position
   state, fixed-fractional sizing, quote sequence, TTL, and risk ceilings before any compiled rule
   can affect paper state.
5. Obtain and retain written OANDA confirmation for the exact entity, Nigerian eligibility, v20
   practice API, `XAU_USD` product form, and data/ML rights—or select another provider through the
   same gate.
6. Only after that gate, add an isolated practice-only network transport, validated local approval
   record, secret-manager integration, credentialed export, raw-response audit metadata, and cursor
   pagination; bind every resulting snapshot through the dataset manifest already implemented.
7. Acquire a small licensed dataset, reconcile several trades by hand, and record every baseline
   and candidate run automatically in the experiment ledger.
