# Trader XAU/USD — MVP Architecture

Status: initial design

Scope: XAU/USD research and paper trading only

Implementation checkpoint: the repository now includes historical research, a strict JSON-only
StrategySpec v1 validator, canonical transcript-evidence binding, a code/hash-bound data-only
compiler, and a pure diagnostic rule evaluator. It also includes a read-only practice-data
attestation contract and offline OANDA request/normalization boundary tested only with fake
responses. A separate manifest-bound M15 diagnostic replay now composes the provisional
supply/demand interpretation through immutable `BUY`/`SELL`/`NO_TRADE` research candidates and
deterministic JSON/CSV evidence artifacts. It does not create signals for execution, fills, trades,
positions, or P&L. The compiled evaluator is not connected to the P&L backtester. The project ships
no credential loader, network transport, order manager, durable execution risk service, or
broker-submission API; those sections remain target requirements, and provider use is blocked by
ADR 0001.

## 1. Purpose

The MVP converts trading education into explicit, testable hypotheses and measures those hypotheses on historical and forward paper data. It is a research system with a guarded paper-execution loop, not an autonomous money-making system.

The language model may extract and rank candidate strategies. It must not generate executable code for the runtime, change an active strategy, approve a strategy, size an order, or call a broker.

## 2. Safety invariants

These constraints are architectural, not UI warnings:

1. **Paper only.** The executor accepts only a `PAPER` environment, the broker allowlist contains practice endpoints only, and live credentials are not present in the deployment.
2. **Risk before execution.** Every order intent passes through the deterministic risk engine. The broker adapter rejects intents without a matching risk decision.
3. **Immutable promotion.** The runtime loads only an immutable, approved `PromotionManifest` containing the hashes of the strategy, code, data policy, and cost model.
4. **No online self-modification.** Outcomes can create a new draft candidate. They cannot mutate or replace the running version.
5. **Reproducibility.** Every reported result identifies the strategy revision, dataset snapshot, feature code revision, execution assumptions, and complete trial history.
6. **Fail closed.** Stale data, a missing spread, broken reconciliation, an invalid clock, an unavailable risk service, or an unknown instrument prevents new orders.

Live trading is deliberately outside the MVP. Adding it requires a separate design and explicit security, legal, broker, operational, and capital-risk review.

## 3. Architectural style

Start as a **modular monolith** with three independently runnable processes:

- `research-worker`: content ingestion, extraction, compilation, backtests, and evaluation.
- `paper-worker`: market-data consumption, feature calculation, signals, risk checks, and paper execution.
- `operator-ui`: review queues, experiment results, promotion approvals, risk state, and reconciliation.

They share a typed domain library but have separate entry points and permissions. This retains strong boundaries without the operational cost of early microservices. Modules can be split later if load or team ownership requires it.

## 4. Component map

```text
Approved content / user-supplied transcript
                    |
                    v
          [Content Ingestion] ------> immutable source artifacts
                    |
                    v
          [Strategy Extractor] ------> draft StrategySpec + evidence
                    |
                    v
         [Validator / Compiler] -----> deterministic strategy plan
                    |
                    v
Market Data ---> [Backtest Engine] ---> [Evaluation + Trial Ledger]
     |                                      |
     |                                      v
     |                              [Strategy Registry]
     |                                      |
     |                             explicit promotion
     |                                      |
     v                                      v
[Live Feature Engine] ---> [Signal Engine] ---> SignalIntent
                                                   |
                                                   v
                                            [Risk Engine]
                                                   |
                                                   v
                                            [Order Manager]
                                                   |
                                                   v
                                        [Paper Broker Adapter]
                                                   |
                                                   v
                         fills / positions / P&L / incidents
                                                   |
                         [Telemetry + Outcome Learner / Ranker]
                                                   |
                                      new draft candidates only
```

### 4.1 Content ingestion

Responsibilities:

- Accept an operator-curated URL plus a user-supplied or otherwise authorized transcript/media asset.
- Record source identity, creator, title, publication time, ingestion time, language, rights/consent metadata, and cryptographic hashes.
- Preserve transcript timestamps and an immutable raw artifact.
- Quarantine unsupported or unlicensed sources.

The MVP must not depend on bulk downloading or bypassing a platform's controls. Source quality is an input for review, not evidence that a strategy works.

### 4.2 Strategy extraction

Responsibilities:

- Split transcripts into timestamped, traceable chunks.
- Produce a typed `StrategySpec` draft using constrained structured output.
- Cite the exact transcript intervals supporting each material rule.
- Mark missing entry, exit, sizing, timing, or risk details as unresolved rather than inventing them.
- Emit an extraction run ID, model/prompt revision, and confidence per extracted claim.

The extractor has write access only to the draft-candidate area.

### 4.3 Validator and compiler

Responsibilities:

- Validate `StrategySpec` against the schema in `STRATEGY_SPEC.md`.
- Resolve references, type-check operators, detect feature cycles, and calculate warm-up requirements.
- Reject arbitrary code, unknown operators, future-data references, unresolved rules, and unfrozen parameters.
- Compile the declarative rule tree into a deterministic in-memory plan used by both backtest and paper runtimes.

The compiler has no broker dependency or credentials.

Current implementation note: compilation produces a `diagnostic_only` plan. Its pure evaluator
covers closed feature/rule semantics and refuses mismatched compiler/feature-manifest versions, but
no backtest or paper runtime accepts it until the event-driven stop, sizing, quote, and position
state machine exists.

### 4.4 Market-data and feature layer

Responsibilities:

- Normalize timestamps to UTC and retain source timestamps.
- Store event time separately from `available_at`, the earliest time the system could have known a value.
- Preserve bid, ask, midpoint, spread, missing intervals, market closures, and source identity.
- Create bars with explicit half-open interval semantics: `[open_time, close_time)`.
- Calculate each feature once in a shared library used by historical and paper paths.
- Version data-quality rules, session calendars, and feature definitions.

Historical research uses immutable Parquet snapshots queried with DuckDB. Operational state begins in PostgreSQL or SQLite and moves to PostgreSQL before multi-process deployment.

### 4.5 Backtest engine

Use a small event-driven simulator as the source of record. Vectorized calculations may screen ideas, but cannot produce promotion evidence.

The simulator must model:

- the natural bid/ask side of entries and exits;
- spread, configurable slippage, commissions, and financing where applicable;
- signal-to-order latency and next-observable-event fills;
- market closures, gaps, rejected orders, partial-fill policy, and quantity rounding;
- stop-loss and take-profit behavior, including a documented conservative rule when bar data cannot determine intrabar ordering;
- the same order, position, and accounting state machine used by the paper order manager.

### 4.6 Evaluation and trial ledger

Every attempted strategy and parameter set receives a trial record, including failures and discarded ideas. Evaluation performs:

1. structural and semantic validation;
2. unit and invariant tests;
3. time-ordered train and walk-forward validation;
4. locked, one-time holdout evaluation;
5. cost, latency, spread, and gap stress tests;
6. parameter-neighborhood and regime stability checks;
7. comparison with simple declared baselines;
8. forward paper observation.

A versioned `PromotionPolicy` defines thresholds such as drawdown limits, minimum observations, net performance, stability, and allowable risk. Thresholds must not be changed after seeing a candidate's locked holdout result.

### 4.7 Strategy registry and promotion

Registry states are:

```text
DRAFT -> VALIDATED -> HOLDOUT_PASSED -> PAPER_APPROVED -> PAPER_RETIRED
```

Transitions are append-only events. `PAPER_APPROVED` requires an explicit operator action and produces a `PromotionManifest` with:

- strategy ID, revision, and canonical spec hash;
- compiled-plan hash and application commit;
- feature-library and session-calendar versions;
- promotion-policy and cost-model versions;
- permitted instrument, timeframe, account, and environment;
- risk-limit snapshot;
- approver and approval time.

The paper worker does not load `DRAFT`, `VALIDATED`, or mutable “latest” references.

### 4.8 Signal engine

At a decision time, the signal engine consumes only a complete `FeatureSnapshot` whose values were available by that time. It emits a `SignalIntent`; it never emits an order.

Signal evaluation is pure:

```text
(StrategyPlan, FeatureSnapshot, PositionSnapshot) -> SignalIntent | NoSignal
```

Replaying the same inputs and plan must produce the same result.

### 4.9 Risk engine

The deterministic risk engine owns sizing and final permission. It enforces, at minimum:

- the strategy's per-trade risk ceiling;
- account-wide exposure and open-position ceilings;
- maximum daily loss and maximum trades per day;
- allowed sessions and instrument;
- current spread, price freshness, and market-status limits;
- stop-distance, quantity-step, and available-paper-margin checks;
- cooldowns, duplicate-signal protection, and the global kill switch.

It returns an immutable `RiskDecision` with `APPROVE`, `RESIZE`, or `REJECT`, reason codes, input hashes, and expiry time.

### 4.10 Order manager and paper broker adapter

The order manager turns an approved risk decision into an idempotent `OrderIntent`, tracks its lifecycle, and reconciles local state against the paper broker. Client order IDs are deterministic from strategy revision, account, signal, and attempt number.

The broker adapter exposes a minimal interface:

```text
get_account() -> AccountSnapshot
get_positions() -> list[PositionSnapshot]
submit(OrderIntent, RiskDecision) -> BrokerOrderAck
cancel(client_order_id) -> BrokerOrderAck
stream_prices(instrument) -> MarketEvent
stream_transactions() -> ExecutionReport
```

The adapter validates that the endpoint and account are registered as practice resources at startup and before submission.

### 4.11 Outcome learner

The initial learner is an offline evaluator/ranker, not a reinforcement-learning trader. It may:

- update uncertainty and reliability estimates by strategy family and market regime;
- prioritize which drafts deserve more research or paper allocation;
- detect degradation between expected and observed costs or behavior;
- propose a new candidate revision with an explicit parent and rationale.

It may not change active rules, parameters, risk limits, allocations, or promotion status. Every proposal re-enters the complete validation path.

### 4.12 Observability and operator controls

Capture structured logs and metrics keyed by correlation ID, strategy revision, signal ID, order ID, and broker transaction ID. Required views and alerts include:

- data freshness, gaps, outliers, and feed disconnections;
- feature parity between replay and paper paths;
- signal counts, suppressions, and risk rejection reasons;
- order latency, slippage, spread, rejects, and reconciliation breaks;
- realized/unrealized paper P&L and drawdown;
- promotion history, current manifest, and kill-switch state;
- expected-versus-realized outcome drift.

Operator controls are authenticated, auditable, and default to disabling new orders while allowing exits and reconciliation.

## 5. Domain interfaces

All interfaces are versioned, validated typed objects. Times are UTC RFC 3339 values; quantities and monetary values use decimal strings rather than binary floating point.

| Object | Producer | Consumer | Required identity/time fields |
|---|---|---|---|
| `ContentAsset` | ingestion | extractor | `source_id`, `published_at`, `ingested_at`, `content_hash` |
| `StrategySpec` | extractor/operator | compiler | `strategy_id`, `revision`, `frozen_at`, `spec_version` |
| `CompiledStrategyPlan` | compiler | backtest/signal engine | `spec_hash`, `compiler_version`, `plan_hash` |
| `MarketEvent` | data adapter | bar/feature engine | `instrument`, `event_time`, `available_at`, `sequence` |
| `FeatureSnapshot` | feature engine | signal engine | `decision_time`, `data_cutoff`, `feature_set_hash` |
| `SignalIntent` | signal engine | risk engine | `signal_id`, `strategy_revision`, `created_at`, `expires_at` |
| `RiskDecision` | risk engine | order manager/adapter | `decision_id`, `signal_id`, `status`, `expires_at`, `input_hash` |
| `OrderIntent` | order manager | paper adapter | `client_order_id`, `decision_id`, `environment=PAPER` |
| `ExecutionReport` | paper adapter | order manager/ledger | `broker_transaction_id`, `client_order_id`, `event_time` |
| `PromotionManifest` | registry | paper worker | hashes, approved scope, approver, `approved_at` |

Events are append-only and idempotent. Consumers persist the last processed source sequence and deduplicate by stable event ID.

## 6. End-to-end data flow

### 6.1 Research path

1. An operator admits a source and records its rights and provenance.
2. Ingestion stores the raw transcript/media metadata and hash.
3. The extractor produces a draft `StrategySpec` with evidence and unresolved items.
4. An operator resolves ambiguity explicitly; the extractor must not guess.
5. The validator freezes parameters and the compiler produces a plan.
6. The evaluator creates a dataset manifest before querying data.
7. The backtester replays time-ordered data and writes trades, metrics, artifacts, and assumptions to a trial.
8. Successful candidates proceed through walk-forward and one-time holdout gates.
9. An operator may approve the exact version for paper use.

### 6.2 Paper path

1. The paper worker verifies the approved manifest and practice broker configuration.
2. Market events are normalized, checked for freshness, and converted to closed bars.
3. The shared feature library emits a `FeatureSnapshot`.
4. The approved plan emits a signal intent.
5. The risk engine rejects, resizes, or approves the intent.
6. The order manager submits an idempotent order to the paper adapter.
7. Execution reports update orders, positions, accounting, and telemetry.
8. Reconciliation periodically compares all local state with the broker.
9. Outcomes are appended to the research dataset. They cannot alter the running strategy.

## 7. Anti-leakage rules

The following rules apply to extraction, features, optimization, and evaluation:

1. **Two clocks:** store the market/source event time and `available_at`. A computation at time `t` may read only records with `available_at <= t`.
2. **Publication boundary:** store video `published_at`, extraction time, and strategy `frozen_at`. Market history predating those times can help develop the hypothesis, but is not independent forward proof.
3. **Closed-bar semantics:** a bar close is unavailable until its close time plus the configured feed delay. A close-based signal cannot fill on that same close; its earliest fill is the next observable quote/event.
4. **Time splits only:** never randomly shuffle market observations. Use expanding or rolling walk-forward splits.
5. **Purging:** remove observations between train and evaluation windows by at least the maximum feature lookback, label horizon, and overlapping holding period. Add an embargo when downstream labels overlap.
6. **Fold-local fitting:** fit scalers, imputers, thresholds, feature selectors, regime classifiers, and strategy parameters using the training portion of each fold only.
7. **Locked holdout:** the extractor, optimizer, and operator do not inspect the final holdout. Run it once for a frozen candidate. Repeated access turns it into development data and requires a new future holdout.
8. **Point-in-time joins:** economic releases, calendars, and external signals must retain their original publication and revision times. Revised values cannot be joined as if they were known earlier.
9. **Complete trial history:** count every hypothesis and parameter trial. Discarded failures remain visible so multiple testing and selection effects can be assessed.
10. **Cost realism:** promotion metrics are net of the declared spread, bid/ask side, slippage, latency, financing, and rejection assumptions.
11. **Training cutoff lineage:** a learned ranker records the exact outcomes available at its training cutoff. It cannot train on the outcome it is asked to predict.
12. **Feature parity:** backtest and paper execution call the same feature and strategy-plan code. Periodic replay must reproduce live snapshots exactly or raise an incident.

## 8. Storage and experiment lineage

Use the simplest stack that preserves lineage:

- Parquet + DuckDB for immutable market and research datasets.
- PostgreSQL for candidates, registry events, signals, risk decisions, orders, positions, reconciliation, and audit records. SQLite is acceptable for a single-process prototype only.
- Content-addressed artifact storage for transcripts, compiled plans, reports, and plots.
- MLflow or an equivalent tracker for runs, parameters, metrics, artifacts, dataset manifests, and application commit hashes.
- Git for code and documentation; database migrations are versioned with the application.

Raw source artifacts and market snapshots are append-only. Corrections create a new dataset version rather than replacing prior evidence.

## 9. Failure handling

| Failure | Required behavior |
|---|---|
| Stale/missing price or spread | Block new signals and orders; alert |
| Feature calculation error | Block the affected strategy; preserve inputs |
| Risk service unavailable | Reject new orders |
| Broker timeout | Query by client order ID before retrying |
| Duplicate event | Idempotently ignore |
| Local/broker state mismatch | Disable new entries; reconcile and alert |
| Daily loss or exposure breach | Disable new entries; manage existing exits per policy |
| Manifest/hash mismatch | Refuse strategy startup |
| Clock drift beyond tolerance | Disable new entries and alert |
| Kill switch active | Reject all new entries; retain observation/reconciliation |

## 10. Initial technology choices

- Python 3.9+ for the dependency-free bootstrap; review a Python 3.12 baseline before adding the
  fuller third-party stack below
- Pydantic for domain contracts and JSON Schema generation
- Polars or pandas for transformations; DuckDB/Parquet for historical queries
- FastAPI for operator and internal APIs
- PostgreSQL plus Alembic for operational persistence
- MLflow for experiment lineage
- A small event-driven simulator and shared order/accounting state machine
- A scheduler such as Prefect only when plain idempotent jobs are no longer sufficient
- Streamlit or a small web UI for the first operator console
- pytest and property-based tests for time, sizing, accounting, and idempotency invariants
- Docker Compose for a reproducible local environment

Avoid distributed queues, Kubernetes, feature stores, reinforcement learning, and multiple brokers until the vertical paper path is reliable.

## 11. Build sequence

1. Implement domain types, time semantics, immutable market snapshots, and one hand-authored baseline strategy.
2. Build the event-driven backtester, accounting state machine, and leakage test suite.
3. Define `StrategySpec`, the validator/compiler, and a labeled set of 10–20 transcript examples.
4. Add extraction with evidence citations and a human ambiguity queue.
5. Add the complete trial ledger, walk-forward evaluation, locked holdout, and promotion manifests.
6. Connect one practice broker through the paper adapter and add risk, idempotency, reconciliation, and the kill switch.
7. Run shadow/paper trading through multiple market conditions before considering any subsequent project.
