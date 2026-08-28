# Risk Policy

- Status: Initial policy
- Scope: Private, personal research on XAU/USD; paper trading only
- Jurisdictional assumption: Project owner is in Nigeria

## 1. Purpose and current boundary

This project may collect permitted educational material, convert trading claims into testable hypotheses, backtest those hypotheses, and submit orders only to a paper-trading environment.

The project is not authorized to:

- place or route a live-money order;
- accept, pool, custody, or manage another person's money;
- trade or advise through another person's account;
- publish personalized signals, recommendations, copy-trading instructions, or account-management services;
- solicit investment, advertise expected returns, or claim guaranteed profitability; or
- promote a research result to production automatically.

Any component that could reach a live broker must remain absent or disabled by default. A paper account must be visibly identified as paper/simulated, use separate credentials, and have no withdrawal capability.

Trader videos, transcripts, posts, and courses are untrusted research inputs. Popularity, confidence, screenshots, and claimed win rates do not establish that a method works. Every claim must become a falsifiable hypothesis and pass the validation process below.

## 2. Non-negotiable engineering controls

### Separation of learning and execution

- Content ingestion and AI systems may propose hypotheses; they may not create, approve, deploy, or alter executable live strategy logic.
- Research, backtesting, paper execution, and any future live execution must use separate environments and credentials.
- Strategy promotion requires an explicit human decision, a versioned review record, and reproducible test evidence.
- Models must not rewrite their own production code, risk limits, prompts, evaluation criteria, or approval records.

### Independent risk layer

The strategy cannot override the risk layer. The risk layer must be deterministic, independently tested, and fail closed. It must enforce configured limits for:

- allowed account and environment;
- allowed instruments, initially XAU/USD only;
- maximum position, order size, gross notional, leverage, and order frequency;
- maximum loss per position, session/day, and evaluation period;
- maximum drawdown and consecutive-loss threshold;
- price age, spread, slippage, market-hours, and connectivity tolerances; and
- duplicate, conflicting, malformed, delayed, or out-of-order orders.

No martingale, loss-chasing, uncapped averaging down, or automatic increase in risk after a loss is permitted. All limits must be absolute rather than inferred by the model.

### Halt and recovery behavior

- A global kill switch must cancel pending orders, prevent new orders, and be available independently of the strategy process.
- The system must halt on stale or missing prices, clock drift, broker/API disagreement, rejected-order ambiguity, repeated network errors, breached limits, corrupted state, or an unknown account/environment.
- Restarting after a safety halt requires human review and acknowledgment of the cause.
- Recovery must reconcile local state against broker state before another order is considered.
- Kill-switch and restart procedures must be tested in the paper environment.

### Auditability

Record, with UTC timestamps and immutable identifiers:

- source provenance and license/permission status;
- dataset and market-data versions;
- strategy, model, prompt, configuration, and risk-policy versions;
- every signal, relevant input, decision, risk rejection, order, broker response, fill, cancellation, and error;
- human approvals and reasons for strategy promotion or rollback; and
- fees, spreads, swaps/financing, slippage, latency, and data-quality warnings used in evaluation.

Logs must be sufficient to reproduce a result without exposing secrets or unnecessary personal data.

## 3. Research and validation standard

No result may be described as validated unless it has:

1. A strategy specification written before final evaluation, including entry, exit, sizing, invalidation, and risk rules.
2. Data-quality checks for timestamps, gaps, duplicated bars/ticks, corporate/vendor revisions, time zones, and price-source differences.
3. Separation of training/development data from untouched out-of-sample evaluation data.
4. Walk-forward or equivalent time-ordered testing that prevents future information from entering past decisions.
5. Realistic spreads, commissions, swaps/financing, rollover effects, latency, rejected orders, and slippage.
6. Sensitivity tests showing that small parameter or cost changes do not destroy the result.
7. Comparison with simple baselines and documentation of all failed experiments, not only winners.
8. Paper/shadow evaluation over a predeclared period that includes materially different market conditions.

Backtest profitability is not evidence of future profit. Paper results must be labeled simulated and kept separate from any future real-money results.

## 4. Nigeria legal and regulatory boundary

This policy is a conservative project boundary, not a legal opinion.

- Nigeria's [Investments and Securities Act 2025](https://sec.gov.ng/documents/1319/Investments_and_Securities_Act_2025_x9rSXtI.pdf) broadly includes commodities, futures, contracts, options, and other derivatives within its definition of securities. Section 61 restricts operating in Nigeria's capital market or carrying on investment and securities business without registration. The precise classification of a particular XAU/USD product depends on its contract and provider.
- The SEC's [Robo-Advisory Rules](https://home.sec.gov.ng/documents/1292/Rules-on-Robo-Advisory-Services_Executed-30-August-2021.pdf) govern client-facing algorithmic investment advice and require registration, suitability controls, algorithm governance, testing, disclosures, technology-risk management, and additional authorization for portfolio management or execution functions.
- SEC's [May 2026 notice](https://www.sec.gov.ng/for-investors/keep-track-of-circulars/public-notice-unregistered-online-investment-schemes/) states that only registered entities may promote investment services, provide investment advisory services, or solicit public funds in the Nigerian capital market and warns against unrealistic or guaranteed returns.
- SEC's [January 2026 CFD-broker warning](https://sec.gov.ng/for-investors/keep-track-of-circulars/suspected-fraudulent-activities-of-modmount-services-limited/) shows that an offshore license or incorporation claim does not by itself authorize solicitation or operation in Nigeria.
- SEC's still-published [online retail forex notice](https://sec.gov.ng/for-investors/keep-track-of-circulars/public-notice-online-retail-foreign-exchange-forex-trading/) describes leveraged online retail forex as unregulated and undertaken at the participant's own risk. Because the notice predates the 2025 Act, written SEC or Nigerian legal advice is required before relying on a regulatory characterization.

Private paper research must stop and receive legal review before the project adds clients, public recommendations, copy trading, managed accounts, pooled funds, performance marketing, or live commercial activity. A client-facing experiment should first be discussed through SEC's [FinPort Regulatory Incubation program](https://www.sec.gov.ng/fintech-and-innovation-hub-finport/finport-programs-ri-and-arip/).

Before selecting any future broker, verify the exact contracting entity—not just its brand—against the SEC register and the entity's home-regulator register. Confirm in writing that it may serve Nigerian residents, offers the stated retail protections, and permits the intended API automation.

## 5. Content and data boundaries

- Use only content and transcripts for which the intended access, storage, extraction, and reuse are permitted by law, license, platform terms, or direct creator permission.
- Do not scrape, download, import, cache, or store YouTube audiovisual content contrary to YouTube's [API Developer Policies](https://developers.google.com/youtube/terms/developer-policies). Use official APIs only within their authorization and quota rules.
- Prefer creator-provided or licensed transcripts. Store derived research notes with source URL, creator, title, publication date, relevant timestamp, access date, and license/permission status.
- Do not treat a creator's identity, reputation, claimed account balance, or claimed performance as verified unless independently substantiated.
- Do not reproduce or redistribute substantial copyrighted content in datasets, reports, prompts, or product output.

The paper-only prototype must not collect client profiles or other people's financial data. If the scope later includes users, Nigeria's [Data Protection Act 2023](https://ndpc.gov.ng/wp-content/uploads/2024/03/Nigeria_Data_Protection_Act_2023.pdf) requires a documented lawful basis, transparent notices, data minimization, security, rights handling, and—in high-risk cases—a privacy impact assessment. Significant solely automated decisions also trigger safeguards including human intervention and contestability.

## 6. Secrets policy

- Never commit API keys, passwords, tokens, cookies, account numbers, recovery codes, private certificates, or signed broker requests to source control.
- Never place secrets in prompts, model context, training data, notebooks, screenshots, issue reports, telemetry, or logs.
- Load secrets at runtime from an approved secret store or ignored local environment file. Secret files must be outside version control with restrictive filesystem permissions.
- Use separate credentials for development and paper trading. Paper credentials must be least-privilege, limited to the paper account, and incapable of withdrawals or live trading.
- Where supported, require MFA, IP allowlisting, short-lived tokens, and narrowly scoped API permissions.
- Redact credentials and sensitive account data before any diagnostic information leaves the local environment.
- Rotate credentials on a schedule, on team/member changes, and immediately after suspected exposure.
- A suspected secret leak requires immediate disablement and rotation, review of broker and local access logs, documentation of impact, and a safety halt until containment is verified.

No future live credential may be created or added until every live-trading prerequisite is satisfied.

## 7. Live-trading prerequisites

Moving beyond paper trading requires a new, explicitly approved policy revision. At minimum, all of the following must be complete:

- written Nigerian legal/regulatory analysis of the precise product, broker entity, automation model, and intended use;
- confirmation that no required SEC registration, authorization, or FinPort process is being bypassed;
- verification that the exact broker entity may serve the owner in Nigeria, allows API automation, and clearly states leverage, margin closeout, negative-balance, custody, complaint, and insolvency protections;
- documented Nigerian tax treatment and an exportable, reconciled transaction-record process;
- independent security review and threat model covering the workstation, dependencies, data vendors, broker API, secrets, update path, and recovery process;
- signed-off strategy specification, reproducible out-of-sample and walk-forward results, realistic cost/stress tests, and a completed paper evaluation with no unresolved critical safety incident;
- predefined numerical live-risk limits based on money the owner can afford to lose, implemented outside the strategy and verified by automated tests;
- segregated initial capital small enough that total loss would not impair living expenses, debt obligations, emergency savings, or dependants;
- tested monitoring, alerts, kill switch, state reconciliation, rollback, incident response, and broker-outage procedures;
- manual approval for every strategy version and every increase in capital or risk; and
- an explicit prohibition on accepting outside money or providing services to others unless the required registrations, governance, client protections, and data-protection controls are in place.

No deadline, sunk cost, paper profit, model confidence score, or desire to recover a loss can waive these prerequisites.

## 8. Mandatory stop conditions

Research and paper execution must stop immediately when:

- the environment cannot be proven to be paper/simulated;
- data or broker state cannot be reconciled;
- a risk limit or control is bypassed, disabled, or changes without authorization;
- credentials may be exposed;
- test results cannot be reproduced;
- source permission or licensing is unclear;
- a proposed feature enters regulated advice, solicitation, client-money, or account-management territory; or
- the system produces misleading performance reporting or hides losses, costs, rejected orders, or failed experiments.

Resumption requires a documented cause, corrective action, verification, and human approval.

## 9. Change control

Changes to scope, permitted instruments, risk controls, data sources, broker connectivity, secret handling, validation criteria, or live-trading prerequisites require review of this policy before implementation. The most restrictive applicable rule wins when code, configuration, broker settings, and this policy disagree.
