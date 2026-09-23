# Strategy-driven synthetic paper replay

The provisional supply/demand strategy can now generate its own execution intents
instead of relying on the handwritten entries in the original demo. This is a
synthetic engineering integration, **not a historical profitability test, connected
demo account, or live trading bot**. The original `Run Paper Demo.command` remains
the scripted demonstration.

## Inputs and command

Run from the project directory with Python 3.9 or newer:

```sh
PYTHONPATH=src python3 -m xau_trader replay-strategy-paper synthetic-m15.csv \
  --manifest synthetic-m15.manifest.json \
  --quote-scenario synthetic-quotes.json \
  --m15-left-wing 3 --m15-right-wing 3 --stop-atr-multiple 0.15 \
  --report-json reports/strategy-paper-new.json
```

The filenames above are placeholders for a coherent fixture, not downloads or
broker exports. The explicit 3/3 swing and 0.15 ATR choices are provisional
research assumptions, not approved source rules. No policy is silently selected
for these structural parameters.

- The CSV and strict **v1 synthetic manifest** must match by hash, coverage, and
  acquisition basis. This first CLI does not accept calendar-bound manifest v2.
- The quote scenario uses the strict synthetic v1 or v2 scenario format but
  its `intents` array **must be empty**. All intents are derived from recomputed
  strategy evidence. Handwritten intents are rejected, not ignored.
- Use v2 with explicit `loss_limits` to enable the total daily loss-count and
  weekly drawdown controls; v1 leaves these additional guards disabled. See
  [loss-limit semantics and configuration](LOSS_LIMITS.md). Reports show the choice.
- Instrument, risk, costs, spread, slippage, quote-age limit, and all other engine
  settings are explicit scenario assumptions. They are not verified Exness specs.
- Every supplied quote must belong to a half-open M15 bar. Its receipt cannot be
  later than that completed bar's availability. The ordered quotes must reconstruct
  every bar's bid and ask open, high, low, and close exactly on the declared price
  grid. Missing groups, unrelated quotes, and out-of-range suffixes fail validation.
- The adapter never invents an intrabar path from OHLC. The fixture author supplies
  that path; its ordering is an assumption, not observed market history.
- Outputs must be new paths. Existing files and input files are never overwritten.

The Python API also accepts a validated synthetic calendar-bound diagnostic replay;
the current CLI intentionally exposes only the simpler continuous v1 path.

## Entry contract

1. Recompute strategy and structural evidence from the supplied replay. Preserve
   every candidate, including warm-up and no-trade rows.
2. Freeze the original signal time, structural stop, and all then-visible target
   evidence. Never backdate a signal or choose a target using future quotes.
3. The first supplied source quote at or after the proposed next M15 open must
   have **that exact source timestamp**, and its receipt must be **strictly after**
   signal creation. Equal-time ambiguity or a missing exact-boundary quote rejects
   the attempt once; later quotes do not retry it.
4. Receipt expires at the proposed boundary plus 15 minutes; the separate, usually
   much tighter quote-age limit still applies. This is a provisional receipt-window
   contract, not a claim of instantaneous real execution.
5. Calculate an adverse-rounded executable entry, choose only the nearest strictly
   directional frozen target, then apply spread, bracket, reward/risk, lot sizing,
   and existing engine guards. Never skip a nearer target to manufacture a good ratio.

A boundary beyond the supplied source tape is recorded as not observed. Rejections,
cancelled attempts, open positions, and closed positions are distinguished in the
full trace. An accepted target retains its original pivot provenance.

## What the regression proves

The 224-bar on-grid synthetic fixture genuinely produces one BUY under the
unchanged provisional signal rules. With the test's 2R minimum, that candidate
is rejected: its nearest target is too close relative to its stop. This is correct
strategy-to-risk wiring, not a profit result. Separate deliberately altered-policy
tests exercise fills and exits; those do not claim source-strategy performance.

Reports bind the replay, structural policy, complete quote order, generated intents,
execution assumptions, and outcomes with fingerprints. The CLI additionally binds
exact manifest/scenario snapshots and CSV/source hashes. Hashes detect changed inputs;
they cannot prove a fixture is real market data.

## Still pending

Daily total-loss-count and weekly drawdown controls are implemented with explicit,
provisional UTC semantics when configured. Approval of unresolved strategy/risk rules,
real data and broker contract/cost validation, real-time timing and execution behavior,
and a demo-only broker connector with persistence/reconciliation remain unfinished.
Every report therefore retains `promotion_eligible=false`,
`source_risk_policy_complete=false`, `broker_connected=false`, and zero real orders.
Enabling these offline controls does not make the source policy fully approved or
turn an MT5 historical timestamp into observed live receipt evidence.
