# Project Charter

- Status: accepted starting assumptions
- Date: 2026-08-25
- Owner scope: private, personal research in Nigeria

## Objective

Build a reproducible research machine that converts permitted trading education into explicit
XAU/USD hypotheses, rejects ambiguous or weak ideas, and evaluates frozen candidates on historical
and forward paper data after realistic costs.

Making money is a hoped-for outcome, not an engineering acceptance criterion. The acceptance
criterion is trustworthy evidence: point-in-time inputs, deterministic rules, realistic execution,
complete failure records, bounded risk, and reproducible results.

## Initial defaults

| Decision | Initial value |
| --- | --- |
| Instrument | `XAU_USD` internal canonical symbol |
| Timeframe | `PT1H` / one-hour completed bars |
| Price data | Executable bid and ask, UTC |
| Strategy source | Manually curated, permitted transcripts or operator notes |
| Candidate approval | Human review required |
| Execution | One paper account only |
| Active strategies | At most one approved version in the first runtime |
| Learning | Offline candidate proposal/ranking only |
| Live trading | Explicitly out of scope |

These defaults can be changed by a documented project decision. A change affecting evidence or
risk creates a new version; it must not silently alter an existing experiment.

## Phase-one deliverables

- A validated bid/ask data contract.
- A hand-verifiable event-driven baseline backtest.
- Next-bar execution and no-lookahead tests.
- Explicit spread, slippage, commission, financing, and liquidation assumptions.
- Versioned source citations and strategy candidates.
- An append-only experiment ledger.
- An independent replay risk gate and read-only practice-market-data interface; order submission is
  intentionally absent.
- Architecture, evaluation, content-rights, secrets, and risk documentation.

## Out of scope

- Live orders or live credentials.
- Automatic deployment or online self-modification.
- Arbitrary model-generated Python in the execution path.
- Scraping or storing content without permission.
- Client accounts, outside capital, managed accounts, copy trading, or personalized advice.
- Claims or advertising of guaranteed, passive, or expected returns.

## Decisions required for phase two

1. The exact paper broker and contracting entity available to the owner in Nigeria.
2. Whether the broker exposes XAU/USD as a CFD, spot metal, future, or another contract.
3. The historical and live bid/ask data source, licence, granularity, financing, and session rules.
4. The first three to five permitted educational sources.
5. Whether source media will be creator-provided transcripts, authorized local audio, or operator
   notes.

No broker credentials or paid data are required until those decisions are recorded.
