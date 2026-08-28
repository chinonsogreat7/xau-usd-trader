# StrategySpec v1

Status: initial normative contract

Applies to: research, backtesting, and paper trading

## 1. Purpose

`StrategySpec` is the safe boundary between natural-language education and deterministic trading logic. It records provenance, exact rules, frozen parameter values, time semantics, execution assumptions, and risk constraints without admitting arbitrary code.

A draft may contain unresolved questions. A promotable spec may not.

This document fixes the v1 field names and semantics. The current milestone uses an equivalent
strict standard-library JSON validator so it remains dependency-free on Python 3.9. A future
Pydantic representation must be pinned, tested against the same rejection suite, and produce the
same canonical document and hash before it can replace this boundary.

## 2. Normative language

The words **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are requirements for validators and consumers.

## 3. Identity, canonicalization, and lifecycle

- `spec_version` MUST be `"1.0"`.
- `strategy_id` is a stable lowercase slug matching `^[a-z][a-z0-9-]{2,63}$`.
- `revision` starts at `1` and increments for every material change.
- Once `frozen_at` is set, the document is immutable. A change creates a new revision.
- The registry computes `spec_hash` from canonical JSON. `spec_hash` is registry metadata and MUST NOT be embedded in the hashed document.
- Maps are serialized with lexicographically sorted keys; UTC times use RFC 3339 with `Z`; quantities, prices, ratios, and monetary values use decimal strings.
- IDs within `parameters`, `features`, and `provenance.sources` MUST be unique.
- A promotable spec MUST have `unresolved_items: []` and a concrete `value` for every parameter.

Lifecycle and evaluation status live in the registry, not in `StrategySpec`. The same immutable spec can therefore be evaluated under multiple explicitly versioned data or cost manifests without changing its rules.

## 4. Top-level schema

```text
StrategySpec {
  spec_version: "1.0"
  strategy_id: StrategyId
  revision: integer >= 1
  name: non-empty string <= 120 chars
  description: string <= 2000 chars
  created_at: UtcTimestamp
  frozen_at: UtcTimestamp | null
  provenance: Provenance
  market: MarketScope
  parameters: map<ParameterId, Parameter>
  features: Feature[]
  schedule: Schedule
  entry: EntryRules
  exit: ExitRules
  sizing: SizingPolicy
  execution: ExecutionPolicy
  risk: RiskPolicy
  assumptions: Assumption[]
  unresolved_items: UnresolvedItem[]
  tags: string[]
}
```

Unknown fields MUST be rejected. This prevents misspellings from silently changing behavior.

## 5. Supporting types

### 5.1 Provenance

```text
Provenance {
  extraction_run_id: string | null
  parent: { strategy_id: StrategyId, revision: integer } | null
  sources: Source[]                         // at least one
}

Source {
  source_id: string
  type: "video" | "article" | "book" | "operator" | "experiment"
  uri: URI | null
  title: string
  creator: string | null
  published_at: UtcTimestamp | null
  ingested_at: UtcTimestamp
  content_sha256: 64 lowercase hex chars
  rights_basis: "user_supplied" | "licensed" | "public_api" | "operator_authored"
  evidence: Evidence[]
}

Evidence {
  evidence_id: string
  start_ms: integer >= 0 | null
  end_ms: integer > start_ms | null
  excerpt_sha256: 64 lowercase hex chars
  supports: string[]                        // JSON pointers into this spec
  extractor_confidence: decimal string in [0, 1] | null
}
```

For a video-derived strategy, every material entry, exit, feature definition, referenced parameter
value, bar duration, sizing, schedule, and decision-timing claim MUST point to video/operator
evidence or an explicit operator interpretation. Human clarifications use an additional `operator`
source rather than altering the original evidence. In the closed single-source learning pipeline,
`excerpt_sha256` is the SHA-256 of canonical JSON containing source/language identity, the evidence
interval, and all overlapping transcript segments with normalized millisecond timestamps.

### 5.2 Market scope

```text
MarketScope {
  instrument: "XAU_USD"
  asset_type: "spot_metal_cfd"
  data_feed_id: string
  execution_venue_id: string
  timezone: "UTC"
  bar_duration: ISO-8601 duration           // MVP allowlist: PT5M, PT15M, PT30M, PT1H, PT4H
  feature_price: "mid"
  required_quote_fields: ["bid", "ask"]
  required_bar_fields: subset of ["open", "high", "low", "close"]
}
```

The instrument identifier is an internal canonical identifier. Adapters translate it to a vendor symbol.

### 5.3 Parameters

```text
Parameter {
  type: "integer" | "decimal" | "boolean" | "enum"
  value: integer | DecimalString | boolean | string
  research_bounds: {
    min: integer | DecimalString
    max: integer | DecimalString
    step: integer | DecimalString
  } | null
  choices: string[] | null                   // required only for enum
  description: string
}
```

`research_bounds` documents the predeclared search space. It does not make a promoted parameter dynamic. The frozen `value` is always used for replay and paper evaluation.

### 5.4 Operands and rule AST

Rules use a closed abstract syntax tree. Strings containing source code, SQL, Python, template expressions, or broker commands are forbidden.

```text
Operand =
  { ref: "bar.open" | "bar.high" | "bar.low" | "bar.close" |
         "quote.bid" | "quote.ask" | "position.side" |
         "position.unrealized_r" | "features.<feature_id>" }
  | { param: ParameterId }
  | { decimal: DecimalString }
  | { integer: integer }
  | { boolean: boolean }
  | { enum: string }

Condition =
  { op: "all" | "any", args: Condition[] }
  | { op: "not", args: [Condition] }
  | { op: "gt" | "gte" | "lt" | "lte" | "eq" | "neq",
      args: [Operand, Operand] }
  | { op: "crosses_above" | "crosses_below", args: [Operand, Operand] }
```

Operator semantics:

- `all` and `any` require at least one child and use short-circuit boolean semantics.
- Comparison operands MUST have compatible types.
- `crosses_above(a, b)` at bar `t` is `a[t-1] <= b[t-1] AND a[t] > b[t]`.
- `crosses_below(a, b)` at bar `t` is `a[t-1] >= b[t-1] AND a[t] < b[t]`.
- A missing current or prior operand produces `UNKNOWN`, not `true`. Entry rules require exactly `TRUE` to trigger.
- Conditions are evaluated only at the decision point declared by `execution.decision_point`.
- No operand may address a future bar, a holdout result, a future fill, or revised data that was unavailable at the decision time.

### 5.5 Features

```text
Feature {
  id: FeatureId
  kind: "ema" | "sma" | "rsi" | "atr" | "rolling_high" | "rolling_low"
  inputs: FeatureInput[]
  params: map<string, { param: ParameterId } | { integer: integer } | { decimal: DecimalString }>
  output_type: "decimal"
  warmup_bars: integer >= 1
  availability: "bar_close"
}

FeatureInput {
  ref: "bar.open" | "bar.high" | "bar.low" | "bar.close" | "features.<feature_id>"
}
```

Feature dependencies MUST form an acyclic graph. The validator MUST calculate the effective warm-up and reject a declared `warmup_bars` below the implementation's requirement. Indicator algorithms and initialization conventions are owned by a versioned feature-library manifest, not inferred by the extractor.

### 5.6 Schedule

```text
Schedule {
  days: subset of ["MON", "TUE", "WED", "THU", "FRI"]
  windows: TimeWindow[]                     // at least one
  exclude_dates: YYYY-MM-DD[]
  close_before_weekend: {
    enabled: boolean
    cutoff_utc: HH:MM | null
  }
}

TimeWindow {
  start_utc: HH:MM
  end_utc: HH:MM                            // strictly later; no overnight window in v1
}
```

Session windows use UTC deliberately. If a named local-market session is needed, a future schema must reference a versioned DST-aware calendar.

### 5.7 Entry rules

```text
EntryRules {
  long: Condition | null
  short: Condition | null
  confirmation_bars: integer >= 1
  cooldown_bars: integer >= 0
  max_entries_per_bar: 1
}
```

At least one of `long` or `short` is required. The MVP holds at most one net XAU/USD position, so an opposite entry cannot implicitly reverse a position. Reversal requires an exit, a confirmed flat state, and a later independent entry.

### 5.8 Exit rules

```text
ExitRules {
  signal: {
    long: Condition | null
    short: Condition | null
  }
  initial_stop: StopPolicy
  take_profit: TakeProfitPolicy | null
  trailing_stop: TrailingStopPolicy | null
  max_holding_bars: integer >= 1 | null
}

StopPolicy = {
  type: "feature_multiple"
  feature_ref: "features.<feature_id>"
  multiple: { param: ParameterId } | { decimal: DecimalString }
  snapshot: "at_signal"
}

TakeProfitPolicy = {
  type: "initial_risk_multiple"
  multiple: { param: ParameterId } | { decimal: DecimalString }
}

TrailingStopPolicy = {
  type: "feature_multiple"
  feature_ref: "features.<feature_id>"
  multiple: { param: ParameterId } | { decimal: DecimalString }
  activation_r: DecimalString
  update_at: "bar_close"
} | {
  type: "break_even"
  activation_r: DecimalString
  offset_r: DecimalString
  update_at: "bar_close"
}
```

The initial stop distance is fixed from the feature snapshot associated with the signal. Stops MUST be protective: below entry for long positions and above entry for short positions.

### 5.9 Sizing

```text
SizingPolicy {
  type: "fixed_fractional"
  risk_fraction_of_equity: DecimalString     // > 0
  equity_source: "paper_account_nav"
  stop_distance_source: "initial_stop"
  quantity_rounding: "down"
  min_units: DecimalString
  max_units: DecimalString
  unit_step: DecimalString
}
```

The risk engine, not the signal engine, computes quantity. It rounds down and applies the most restrictive of the strategy, account, broker, margin, and global risk limits.

### 5.10 Execution policy

```text
ExecutionPolicy {
  environment: "PAPER"
  decision_point: "bar_close"
  earliest_entry: "next_observable_quote"
  entry_order_type: "market"
  time_in_force: "FOK" | "IOC"
  allow_partial_fill: boolean
  price_side: "natural_bid_ask"
  cost_model_id: string
  intrabar_ambiguity: "stop_first"
  signal_ttl_bars: integer >= 1
}
```

`natural_bid_ask` means buys execute against ask-side liquidity and sells against bid-side liquidity. `stop_first` is the conservative bar-backtest rule when both stop and target lie inside the same bar and no finer data determines which occurred first.

### 5.11 Risk policy

```text
RiskPolicy {
  max_open_positions: 1
  max_trades_per_utc_day: integer >= 1
  max_daily_loss_fraction: DecimalString
  max_spread_price: DecimalString
  max_quote_age_ms: integer >= 1
  min_stop_distance_price: DecimalString
  max_stop_distance_price: DecimalString
  close_on_data_stale: boolean
  entry_kill_switch_required: true
}
```

These are strategy ceilings. A separate account-wide policy MAY be stricter and always wins. Risk values in the example are illustrative research settings, not financial recommendations.

### 5.12 Assumptions and unresolved items

```text
Assumption {
  id: string
  text: non-empty string
  affects: string[]                         // JSON pointers
  source: "explicit" | "operator_interpretation" | "simulation_policy"
}

UnresolvedItem {
  id: string
  question: non-empty string
  affects: string[]                         // JSON pointers
  severity: "blocking" | "non_blocking"
}
```

All unresolved items are blocking for promotion in v1, regardless of stated severity. The severity field supports review prioritization only.

## 6. Complete example

This example is intentionally simple. It demonstrates the contract and is not a claim of profitability or a recommended way to trade gold.

```yaml
spec_version: "1.0"
strategy_id: xau-m15-ema-cross-atr
revision: 1
name: XAU/USD M15 EMA crossover with ATR exits
description: >-
  Illustrative bidirectional crossover strategy extracted into deterministic
  closed-bar rules for historical evaluation and paper trading.
created_at: "2026-08-25T10:00:00Z"
frozen_at: "2026-08-25T12:00:00Z"

provenance:
  extraction_run_id: extract-20260825-0001
  parent: null
  sources:
    - source_id: video-001
      type: video
      uri: "https://example.invalid/trading-video-001"
      title: Illustrative EMA and ATR lesson
      creator: Example educator
      published_at: "2026-07-01T09:00:00Z"
      ingested_at: "2026-08-25T09:30:00Z"
      content_sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      rights_basis: user_supplied
      evidence:
        - evidence_id: ev-entry
          start_ms: 125000
          end_ms: 181000
          excerpt_sha256: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
          supports:
            - /entry
            - /entry/long
            - /entry/short
            - /features
            - /market/bar_duration
            - /parameters
            - /schedule
            - /execution/decision_point
          extractor_confidence: "0.92"
        - evidence_id: ev-exit
          start_ms: 245000
          end_ms: 296000
          excerpt_sha256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
          supports:
            - /exit/initial_stop
            - /exit/take_profit
            - /exit/max_holding_bars
            - /sizing
          extractor_confidence: "0.88"
    - source_id: operator-001
      type: operator
      uri: null
      title: Rule clarification record
      creator: research-operator
      published_at: null
      ingested_at: "2026-08-25T11:30:00Z"
      content_sha256: "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
      rights_basis: operator_authored
      evidence: []

market:
  instrument: XAU_USD
  asset_type: spot_metal_cfd
  data_feed_id: practice-feed-v1
  execution_venue_id: practice-broker-v1
  timezone: UTC
  bar_duration: PT15M
  feature_price: mid
  required_quote_fields: [bid, ask]
  required_bar_fields: [open, high, low, close]

parameters:
  fast_period:
    type: integer
    value: 20
    research_bounds: {min: 10, max: 30, step: 5}
    choices: null
    description: Fast EMA period in closed M15 bars.
  slow_period:
    type: integer
    value: 50
    research_bounds: {min: 40, max: 80, step: 10}
    choices: null
    description: Slow EMA period in closed M15 bars.
  atr_period:
    type: integer
    value: 14
    research_bounds: null
    choices: null
    description: ATR period in closed M15 bars.
  stop_atr_multiple:
    type: decimal
    value: "1.5"
    research_bounds: {min: "1.0", max: "2.5", step: "0.25"}
    choices: null
    description: Initial stop distance as a multiple of signal-time ATR.
  reward_r_multiple:
    type: decimal
    value: "2.0"
    research_bounds: {min: "1.0", max: "3.0", step: "0.5"}
    choices: null
    description: Take-profit distance in initial risk units.

features:
  - id: ema_fast
    kind: ema
    inputs: [{ref: bar.close}]
    params: {period: {param: fast_period}}
    output_type: decimal
    warmup_bars: 20
    availability: bar_close
  - id: ema_slow
    kind: ema
    inputs: [{ref: bar.close}]
    params: {period: {param: slow_period}}
    output_type: decimal
    warmup_bars: 50
    availability: bar_close
  - id: atr
    kind: atr
    inputs:
      - {ref: bar.high}
      - {ref: bar.low}
      - {ref: bar.close}
    params: {period: {param: atr_period}}
    output_type: decimal
    warmup_bars: 15
    availability: bar_close

schedule:
  days: [MON, TUE, WED, THU, FRI]
  windows:
    - {start_utc: "07:00", end_utc: "17:00"}
  exclude_dates: []
  close_before_weekend:
    enabled: true
    cutoff_utc: "16:30"

entry:
  long:
    op: crosses_above
    args:
      - {ref: features.ema_fast}
      - {ref: features.ema_slow}
  short:
    op: crosses_below
    args:
      - {ref: features.ema_fast}
      - {ref: features.ema_slow}
  confirmation_bars: 1
  cooldown_bars: 4
  max_entries_per_bar: 1

exit:
  signal:
    long:
      op: crosses_below
      args:
        - {ref: features.ema_fast}
        - {ref: features.ema_slow}
    short:
      op: crosses_above
      args:
        - {ref: features.ema_fast}
        - {ref: features.ema_slow}
  initial_stop:
    type: feature_multiple
    feature_ref: features.atr
    multiple: {param: stop_atr_multiple}
    snapshot: at_signal
  take_profit:
    type: initial_risk_multiple
    multiple: {param: reward_r_multiple}
  trailing_stop: null
  max_holding_bars: 96

sizing:
  type: fixed_fractional
  risk_fraction_of_equity: "0.0025"
  equity_source: paper_account_nav
  stop_distance_source: initial_stop
  quantity_rounding: down
  min_units: "0.01"
  max_units: "1.00"
  unit_step: "0.01"

execution:
  environment: PAPER
  decision_point: bar_close
  earliest_entry: next_observable_quote
  entry_order_type: market
  time_in_force: FOK
  allow_partial_fill: false
  price_side: natural_bid_ask
  cost_model_id: xau-paper-cost-v1
  intrabar_ambiguity: stop_first
  signal_ttl_bars: 1

risk:
  max_open_positions: 1
  max_trades_per_utc_day: 3
  max_daily_loss_fraction: "0.01"
  max_spread_price: "1.00"
  max_quote_age_ms: 3000
  min_stop_distance_price: "0.50"
  max_stop_distance_price: "50.00"
  close_on_data_stale: false
  entry_kill_switch_required: true

assumptions:
  - id: assume-close-confirmation
    text: The crossover is confirmed only after the M15 bar is closed.
    affects: [/entry, /execution/decision_point]
    source: explicit
  - id: assume-opposite-cross-exit
    text: An opposite crossover exits an existing position but does not reverse it.
    affects: [/exit/signal]
    source: operator_interpretation
  - id: assume-intrabar-order
    text: A stop is assumed to fill before a target when both are touched in one bar.
    affects: [/execution/intrabar_ambiguity]
    source: simulation_policy

unresolved_items: []
tags: [xau-usd, m15, crossover, atr, illustrative]
```

## 7. Validation rules

A v1 validator MUST reject a spec when any of these conditions holds:

1. Schema, enum, pattern, range, arity, or unknown-field validation fails.
2. `frozen_at < created_at`, a source has `published_at > ingested_at`, or a frozen spec cites a
   source with `ingested_at > frozen_at`.
3. A feature, parameter, evidence pointer, or rule reference does not resolve.
4. Feature dependencies are cyclic or warm-up is understated.
5. A promoted candidate contains an unresolved item or an unfrozen parameter.
6. Entry rules are missing for both directions.
7. A rule uses a forbidden data source, unsupported operator, future reference, or arbitrary expression.
8. Execution environment is not `PAPER`, quotes do not include bid and ask, or price-side behavior is not natural bid/ask.
9. The earliest entry could occur before the decision data was available.
10. The initial stop, sizing source, quantity limits, or risk limits are incomplete or internally inconsistent.
11. Schedule windows overlap, cross midnight, or fall outside the declared days/calendar behavior.
12. Evidence timestamps are malformed or a video-derived material rule, feature, referenced
    parameter value, bar duration, sizing choice, schedule, or decision time has neither source
    evidence nor an explicit operator clarification.

Warnings that do not change validity SHOULD include unusually broad parameter ranges, weak extraction confidence, too little planned warm-up/history, implausible risk values, and assumptions that materially depart from the source.

## 8. Deterministic evaluation semantics

For each closed bar at decision time `t`:

1. Confirm the bar and all dependent records have `available_at <= t`.
2. Update features using only data available through that bar.
3. Return no signal during warm-up or outside the schedule.
4. Evaluate exits for the current position before evaluating new entries.
5. Evaluate entry ASTs; `UNKNOWN` behaves as false.
6. Emit at most one signal intent with the complete feature/input hash.
7. Permit fill only on the next observable quote after `t` and before signal expiry.
8. Let the risk engine calculate and cap quantity using current paper account state.
9. Never reverse in the same event; wait for confirmed flat state.

Any future compiled-plan backtest and paper workers MUST call the same evaluation implementation.

### 8.1 Current dependency-free implementation profile

The executable validation/compiler milestone makes the following formerly implicit details
explicit:

- Input is JSON only. Duplicate keys, non-finite numbers, custom Python objects, and JSON numeric
  decimals are rejected. Decimal fields use plain strings and are normalized to non-exponent form;
  negative zero becomes `"0"` and insignificant trailing zeros are removed.
- Timestamps must already use RFC 3339 UTC `Z` notation with no more than six fractional digits.
  Text is normalized to Unicode NFC. Maps are hash-sorted; rule/feature/evidence arrays retain
  order, while schedule days, windows, and excluded dates are normalized to semantic order.
- Parameter, feature, source, evidence, assumption, and unresolved-item IDs match
  `^[a-z][a-z0-9_-]{0,63}$`. Dots are therefore reserved for closed references such as
  `features.ema_fast`.
- Draft parameter `value` may be `null` only as non-compilable draft state. Every unresolved item,
  including one marked `non_blocking`, prevents compilation and promotion. Compilation is frozen
  by default and revalidates the canonical document rather than trusting readiness flags.
- Frozen specs cannot cite a source ingested after `frozen_at`. Feature-period values and their
  declared research bounds stay within `[1,10000]`; research bounds for protective multiples stay
  strictly positive.
- Condition trees are limited to depth 32 and 512 nodes. Integer and decimal operands share one
  numeric comparison domain; booleans do not coerce to integers. Crosses require a time-varying
  operand and immediately adjacent bars. Multi-bar entry confirmation rejects cross events and
  position-dependent history that the current profile cannot repeat coherently.
- UTC schedule windows use `[start,end)` at the completed bar time and gate new entries only.
  Excluded dates override windows. Simultaneous long and short truth produces `CONFLICT`; exits are
  evaluated before entries, and no entry is emitted while a position remains open. An enabled
  Friday cutoff must align to a completed boundary of the declared bar duration.
- The built-in feature manifest fixes local 28-digit, half-even `Decimal` arithmetic; SMA/rolling
  use inclusive windows, EMA uses an SMA seed and `2/(period+1)`, and ATR/RSI use Wilder seeding and
  recurrence. Gaps or close-time-unavailable bars reset feature continuity, and every required
  feature must be ready before entry. The manifest includes the compiler source-artifact hash;
  persisted plans whose compiler/profile/manifest do not match the current runtime are rejected.
- Position state is complete and bound to the exact evaluated decision time. Positioned contexts
  require holding duration, and an active exit using `position.unrealized_r` requires that value.
- Video material must cover entries, exits, features, referenced parameter values, bar duration,
  sizing, schedule, and decision timing. The closed learning pipeline additionally binds source
  identity/rights/times, ensures ingestion precedes freezing, and recomputes each canonical
  transcript-excerpt hash.

The resulting `CompiledStrategyPlan` is `diagnostic_only`. Its pure evaluator covers feature and
condition behavior, schedule gating, cooldown input, max-holding input, exit precedence, and input
hashing. It does not execute the initial stop, target, trailing stop, quantity sizing, quote TTL,
`close_on_data_stale`, or order state. Those require a separate observed-clock event-driven runner
with `(available_at, sequence)` event identity, so the current P&L backtester must not accept this
plan yet.

## 9. Related immutable manifests

`StrategySpec` intentionally does not contain results. Reproducible evaluation also requires:

- `DatasetManifest`: sources, snapshots, cutoffs, time ranges, hashes, gaps, and quality rules.
- `FeatureManifest`: indicator algorithms, initialization, numeric precision, and code revision.
- `CostModel`: spread source, slippage, latency, financing, gaps, and rejection assumptions.
- `EvaluationPlan`: train/walk-forward/holdout boundaries, purging, embargo, metrics, baselines, and trial family.
- `PromotionPolicy`: thresholds and required reviews declared before holdout access.
- `PromotionManifest`: hashes the exact approved objects and limits its use to the paper environment.

Changing any of these creates a new evaluation lineage. It must never overwrite an earlier result.
