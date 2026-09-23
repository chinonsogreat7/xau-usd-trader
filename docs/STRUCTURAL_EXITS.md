# Provisional structural exit planner

`structural_exits.py` connects the existing candidate evidence to **stop and
target plans**, not to simulated fills or broker orders. Its public entry point
revalidates the complete diagnostic replay and currently accepts synthetic
evidence only. Every candidate receives a plan or an explicit skip reason;
pre-roll decisions remain in the trace.

## Explicit research interpretation

Callers must supply `StructuralExitPolicy(m15_left_wing, m15_right_wing,
stop_atr_multiple)` and the instrument's `price_tick`. There are no implicit
policy defaults. The regression example uses three M15 candles on each side and
an ATR multiplier of `Decimal("0.15")`. These values are an **unapproved operator
hypothesis**. The source document did not completely specify the M15 swing or
the stop ATR's timeframe/snapshot.

- M15 swings are strict midpoint highs/lows; equal wing prices disqualify a
  swing. A swing is unavailable until its right-side candles close and every
  candle needed for the comparison is received. Scheduled closures do not
  create candles.
- At the candidate's decision time, choose the latest confirmed, visible M15
  directional swing and the latest completed, visible H1 ATR from the replay.
- A long stop is the lower of the demand-zone low and swing low, minus the ATR
  buffer. A short stop is the higher of the supply-zone high and swing high,
  plus the buffer. Stops round outward to the supplied price tick.
- Freeze all confirmed H1 highs for a long, or lows for a short, that were
  available at that decision. Round high targets down and low targets up to the
  grid. Preserve pivot provenance and deterministic equal-price tie ordering.
- Do not choose a target using future prices. `select_entry_target(plan,
  entry_price)` later selects the nearest frozen level strictly beyond the
  supplied entry price, or returns `None`. It never chooses a farther target
  merely to improve reward/risk.

No visible M15 swing, ATR, or target evidence means no plan. Multiple selected
zones also fail closed rather than introduce an undocumented selection rule.
Policy, evidence, replay, and result fingerprints make changed inputs visible;
they are not source approval or a performance claim.

## Verification fixture

`tests/structural_exit_fixture.py` deliberately changes one wick in generated
synthetic candles. The unchanged provisional strategy then derives one BUY from
224 candles. Its visible structural evidence produces a stop of 2002.49 and a
nearest H1 target of 2005.45 at a test tick size of 0.01.

That nearest target would not meet 2R at the fixture's nearby entry prices. This
is useful rejection evidence, not a reason to pick the farther 2009.60 target.
The fixture does not claim to represent actual XAU/USD market history.

## Execution is a separate stage

An actionable candidate normally becomes available exactly at its proposed next
M15 open. The [synthetic strategy adapter](STRATEGY_PAPER.md) now connects these
plans using an explicit contract: source quote time must equal the proposed
boundary and quote receipt must be strictly later than decision availability.
Missing boundary quotes and equal-time ambiguity reject the attempt. No timestamps
are backdated and no late source quote is called an exact open. This contract is
a provisional simulation assumption, not a guarantee of executable live prices.

This module does not select an actual fill, test executable reward/risk, size an
order, implement the document's daily/weekly guards, calculate returns, or send a
broker request. A plan's prices are structural research levels, not proof that
a broker could accept or execute them.

See [how to run the existing simulator](RUNNING_THE_BOT.md). The Mac launcher still
runs scripted execution scenarios; the new adapter has its own explicit-input
replay command and does not contact a broker.
