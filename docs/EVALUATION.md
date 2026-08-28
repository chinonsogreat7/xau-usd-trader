# Evaluation and Promotion Protocol

## Purpose

This document defines how a trading strategy learned or extracted from educational videos is evaluated before it can trade real money. Its purpose is to prevent hindsight, data leakage, unrealistic fills, overfitting, and selective reporting from being mistaken for a durable edge.

The system treats every video as a source of **testable hypotheses**, not as proof that a strategy works. No backtest, paper-trading result, or live result guarantees future profit.

The requirements in this document are normative:

- **MUST** means the test is invalid if the requirement is not met.
- **SHOULD** means an exception requires a written justification in the run manifest.
- Numeric thresholds are initial defaults. They may be changed only before results are viewed and must then be versioned and frozen.

## Evaluation artifacts

Every candidate strategy MUST have a unique version and an immutable evaluation manifest containing:

- source video URLs, titles, publishers, publication timestamps, and relevant timestamps within each video;
- transcript files and hashes, ingestion timestamp, and any human corrections;
- the exact extracted entry, exit, invalidation, sizing, timeframe, instrument, and risk rules;
- unresolved ambiguities and extraction confidence;
- model, prompt, feature, and parameter versions;
- source-code commit, dependency lockfile, configuration, random seeds, and data hashes;
- broker, account type, symbol specification, quote currency, and trading-session assumptions;
- every parameter set and strategy variant tried, including failed experiments;
- predefined primary metric, baselines, risk limits, and pass/fail thresholds; and
- final signal, order, fill, position, and P&L ledgers.

A strategy with ambiguous discretionary language is not testable. It MUST be rejected or converted into explicit rules before evaluation. Any later edit produces a new version and resets the untouched holdout and paper-trading qualification.

## Backtest validity rules

### Pre-registration and trial accounting

Before a backtest is run, the hypothesis, allowed parameters, search space, primary metric, risk budget, baselines, and promotion thresholds MUST be frozen. All experiments MUST be recorded in a trial ledger. Failed variants may not be deleted, because the number of attempted variants is required to estimate selection bias and backtest overfitting.

Exploratory results may guide a future strategy version, but they MUST NOT be reported as out-of-sample evidence for the current version.

### Point-in-time data

The simulator MUST use data that could have been available at each decision time. Data must be checked for:

- duplicate, missing, out-of-order, or corrected records;
- timezone, daylight-saving, session, holiday, and rollover errors;
- bid/ask inversion, stale quotes, invalid spikes, and feed outages;
- differences between the research feed and intended broker feed; and
- symbol precision, tick size, minimum lot, stop-distance, and margin-rule changes.

Cleaning rules MUST be deterministic and recorded. Suspicious prices may not be removed merely because they hurt performance. Bar data MUST NOT be used to assume a favorable order of intrabar stop and target execution; use finer data or the conservative ordering.

### Decision and fill timing

Signals may use only observations whose timestamps are at or before the decision timestamp. Orders MUST fill only on a later tradable quote after configured decision, network, and broker latency. A close-derived signal cannot fill at the same close unless a real executable order could have been placed before that quote.

Features, labels, scalers, normalizers, imputers, thresholds, and model selection MUST be fit using training data only. Overlapping labels or positions require purging and an embargo around validation/test boundaries.

### Reproducibility and engine verification

The same manifest, data hashes, code commit, and seeds MUST reproduce the signal, order, fill, and P&L ledgers exactly, except for a documented numerical tolerance.

Before strategy evaluation, the engine MUST pass:

- unit tests for long and short P&L, position sizing, fees, swaps, margin, stops, targets, gaps, and forced liquidation;
- hand-calculated golden trade cases;
- tests for missing data, rejected orders, partial fills, and duplicate events; and
- reconciliation against an independent calculation or implementation.

Unexplained reconciliation differences invalidate the run.

## Walk-forward and publish-date leakage controls

### Source cutoff

For each strategy version, define:

`knowledge_cutoff = max(latest source publication time, transcript ingestion time, latest permitted training-data time, strategy freeze time)`

Only results after this cutoff can be treated as admissible out-of-sample evidence. Testing a rule from a later video on earlier prices is a retrospective diagnostic, because the presenter may have selected the rule with knowledge of those prices. Such results MUST be labeled **hindsight-contaminated** and cannot qualify a strategy for paper or live trading.

If the language model may have learned later market outcomes during pretraining, it SHOULD be used to extract and formalize source rules rather than invent rules from historical outcomes. The decisive evidence remains an untouched period after the strategy freeze time.

### Walk-forward design

Evaluation MUST be chronological. Use either rolling or anchored training windows followed by validation and test windows. The chosen schedule MUST be fixed before results are viewed and SHOULD include at least five non-overlapping test folds spanning materially different trend, range, volatility, and liquidity conditions.

For each fold:

1. Fit and tune on training data.
2. Select the candidate using validation data only.
3. Freeze the candidate.
4. Run the test window once.
5. Do not feed test results back into the same strategy version.

The final holdout MUST remain inaccessible until the candidate and thresholds are frozen. Viewing the holdout, changing the rules, and rerunning it converts it into training data; a new future holdout is then required.

## Realistic execution and cost assumptions

Performance MUST be reported net of all costs. Gross or mid-price results may be shown only as diagnostics beside, never instead of, net results.

The base simulation MUST include:

- observed or session-aware bid/ask spread rather than a fixed mid-price fill;
- published broker commissions and account fees;
- broker-specific overnight financing/swap, including day-count and multi-day rollover rules;
- decision, network, and broker latency;
- adverse slippage calibrated by order type, size, session, volatility, and event conditions;
- gaps through stops, stop-order conversion behavior, rejects, partial fills, and price improvement only when supported by evidence;
- tick/lot rounding, minimum stop distance, margin, leverage, liquidation, and negative-balance rules; and
- quote-currency conversion where applicable.

Until live execution data exists, assumptions MUST be conservative and documented. Paper and tiny-live fills will later be used to calibrate a versioned slippage model; that model may affect only future strategy versions.

Every candidate MUST also survive these cost stresses:

- **Base:** best current broker/feed estimates.
- **Adverse:** 2x normal spread and slippage, higher observed latency, and no favorable price improvement.
- **Event/gap:** empirically plausible spread expansion, delayed fill, and stop gaps during volatile periods.

A strategy that is viable only with zero latency, constant spread, guaranteed stop prices, or full fills is invalid.

## Baseline comparisons

Every strategy MUST be compared over identical test windows, capital, risk targets, and cost rules with:

1. cash/no-trade;
2. constant-risk XAU/USD exposure or buy-and-hold, where relevant;
3. a simple trend baseline, such as a moving-average or breakout rule;
4. a simple mean-reversion baseline;
5. random entries matched for trade count, direction balance, exposure, and holding period; and
6. an ablation using the same pipeline without video-derived information.

Baselines MUST receive the same favorable or unfavorable execution treatment as the candidate. Added complexity is justified only if the candidate improves net risk-adjusted performance or risk control beyond these baselines.

## Metrics and statistical controls

Reports MUST include at least:

- net return, CAGR where meaningful, expectancy, and profit factor;
- volatility, Sharpe, Sortino, and Calmar ratios;
- maximum drawdown, drawdown duration, time underwater, and tail loss/CVaR;
- trade count, win rate, payoff ratio, turnover, exposure, leverage, and capacity assumptions;
- results by fold, year, session, direction, volatility regime, and cost scenario; and
- concentration of profit by trade and time period.

Confidence intervals MUST account for serial dependence, for example with a block bootstrap. Model selection MUST account for the complete trial count using a Deflated Sharpe Ratio, a multiple-testing reality check, and, where the trial structure permits, an estimate of Probability of Backtest Overfitting.

## Promotion gates

Promotion is sequential. A later gate cannot compensate for failure at an earlier gate.

### Gate 1: Valid research backtest

All of the following are required:

- the manifest, rule specification, trial ledger, and reproducibility checks are complete;
- all leakage, point-in-time data, timing, and engine tests pass;
- overall walk-forward net expectancy is positive after base costs;
- net expectancy is positive in at least 70% of test folds;
- the candidate beats cash and at least the relevant risk-matched simple baseline on the predefined primary metric;
- the result survives adverse cost stress without breaching the risk budget;
- the default Deflated Sharpe confidence is at least 95%, or a predeclared multiplicity-adjusted test has `p <= 0.05`;
- estimated Probability of Backtest Overfitting is no more than 20% when it can be measured reliably;
- performance is stable across nearby parameter values rather than concentrated at one optimum;
- no single regime is solely responsible for profitability; and
- the five best trades contribute no more than 50% of total net profit.

If there are too few independent observations for the statistical criteria, the strategy remains unproven and does not advance merely because point estimates look attractive.

### Gate 2: Untouched holdout

The frozen strategy is run once on data after the knowledge cutoff. It passes only if:

- net expectancy is positive after base costs;
- drawdown remains within the predefined risk limit and the walk-forward predictive range;
- performance is not materially worse than the predefined lower tolerance versus walk-forward results;
- relevant baselines are not superior on the primary metric; and
- no code, data, or rule defect is discovered.

Any strategy change after this result requires a new version and a new future holdout.

### Gate 3: Paper trading

Paper trading MUST use the production signal, order, portfolio, and risk code against live broker quotes. It may not substitute a simplified notebook or accept manual trade selection. Emergency interventions must be logged; a discretionary intervention invalidates the affected sample for qualification.

The minimum observation period is:

- for frequent/intraday strategies: at least 3 months **and** 100 closed, reasonably independent trades;
- for slower/swing strategies: at least 6 months **and** 30 closed, reasonably independent trades; and
- longer when required to observe the sessions, spread conditions, or volatility regimes the strategy claims to trade.

Paper trading passes only if:

- net expectancy remains positive after modeled costs;
- drawdown stays within the hard risk limit and the precomputed predictive bound;
- realized median and tail spread, slippage, latency, rejection, and fill rates remain inside the simulation assumptions;
- at least 99.5% of eligible signals produce the expected order-state transition, excluding documented broker outages;
- no unresolved critical risk, accounting, duplicate-order, stale-data, or kill-switch incident remains;
- broker statements/quotes reconcile with the internal order, position, and P&L ledgers; and
- results are not dominated by a few trades or one short-lived regime.

Paper trading is an execution and forward-validity test, not proof of profitability. It MUST NOT be shortened after a winning streak.

### Gate 4: Tiny live trading

Only capital that can be fully lost may be used. The initial allocation MUST be the smallest practical size and operate with hard per-trade, daily, portfolio drawdown, leverage, and aggregate exposure limits. A tested kill switch and broker-side protections are required.

Promotion beyond tiny live requires another predeclared observation window in which:

- live signal, fill, cost, and P&L ledgers reconcile;
- realized execution remains within paper/simulation tolerances;
- drawdown and loss limits are never overridden;
- no unresolved critical operational incident occurs; and
- live outcomes remain statistically consistent with the paper and holdout predictive ranges.

Scaling occurs only in fixed, predeclared increments. Loss chasing, martingale sizing, and scaling because of a short winning streak are prohibited.

## Automatic rejection or quarantine conditions

A candidate MUST be rejected or quarantined when any of these conditions is present:

- video examples or historical test periods are hindsight-contaminated and no genuine forward holdout exists;
- look-ahead, future-data normalization, label leakage, or same-bar impossible fills are detected;
- the holdout has been repeatedly inspected or used for tuning;
- experiment failures or the true number of tried variants are missing;
- rules are ambiguous, discretionary, silently changed, or not reproducible;
- profitability disappears after realistic costs or modest parameter perturbations;
- results depend on one feed, one short regime, one parameter value, or a few extreme trades;
- risk depends on unlimited averaging down, martingale/grid sizing, hidden short-volatility exposure, or uncapped leverage;
- simulated orders exceed realistic liquidity, margin, broker, or stop constraints;
- the system requires perfect connectivity, constant spreads, guaranteed stop fills, or zero latency;
- paper/live reconciliation fails or duplicate orders, stale prices, position drift, or kill-switch failures occur; or
- the strategy breaches a hard drawdown, daily loss, leverage, or exposure limit.

Quarantine means no new risk may be opened while the issue is investigated. A correction creates a new strategy or infrastructure version and repeats every affected gate.

## Reporting and decision record

Every gate concludes with a signed, versioned decision record containing:

- the complete manifest and trial count;
- all net and gross metrics, baselines, confidence intervals, and stress results;
- failures, exceptions, manual interventions, and known limitations;
- a clear pass, fail, or quarantine outcome; and
- the exact strategy and infrastructure versions eligible for the next gate.

Results must be described as hypothetical until they are live, and live results must remain distinct from simulated results. If results are shown to other people or the system is used to manage or advise on other people's money, applicable legal, licensing, disclosure, and recordkeeping requirements must be reviewed before deployment.
