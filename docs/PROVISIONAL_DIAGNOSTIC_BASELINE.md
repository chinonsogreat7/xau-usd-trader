# XAU/USD provisional diagnostic baseline

- Status: unapproved operator hypothesis
- Purpose: deterministic event-kernel development and hand reconciliation
- Execution: none; this policy cannot place or simulate an order

## What this baseline is

This is the first fully selected interpretation of the admitted
`XAUUSD Trading Bot Strategy.docx` research input. It exists so that the event kernel can produce
repeatable paper **candidate decisions** while every material choice remains visible and
fingerprinted. It is not a claim that the rules came verbatim from the linked video, have positive
expectancy, or are suitable for live trading.

Changing any choice below creates a new research revision. Results from different revisions must
not be pooled as though they came from one strategy.

## Selected formation and lifecycle policy

| Area | Provisional selection |
|---|---|
| Instrument and prices | `XAU_USD`; arithmetic bid/ask midpoint for diagnostic features |
| H1 impulse window | Four contiguous completed H1 bars |
| Directional candles | At least 3 of 4 in the impulse direction |
| Directional movement | Net first-open to final-close movement |
| ATR | H1 Wilder ATR(14), initialized with an SMA seed |
| ATR snapshot | Last completed H1 bar strictly before the four-bar impulse window |
| Movement threshold | At least `1.5 × ATR(14)` |
| Largest-body threshold | At least one body of `0.8 × ATR(14)` |
| Zone origin | Last opposite-direction H1 candle strictly before the impulse window |
| Origin search | Finite 20-H1-bar lookback |
| Zone freshness | First retest episode only |
| Retest episode | Consecutive inclusive M15 overlaps form one episode |
| Equal-time order | Apply H1 invalidation before M15 touch |
| Overlapping zones | Select newest origin, then the stable zone key |
| Invalidation | Strict H1 close beyond the zone's outer boundary |

The before-window ATR snapshot and H1-first precedence are conservative anti-lookahead choices,
not source-verified rules. The 20-bar origin bound is an explicit engineering hypothesis added to
make the reverse search finite and reproducible.

## Selected regime and M15 confirmation policy

| Area | Provisional selection |
|---|---|
| H1 regime | Compare the latest two confirmed pivot highs and latest two confirmed pivot lows |
| Bullish / bearish | Both pairs strictly rise / both pairs strictly fall |
| Mixed or equal structure | Range; do not propose an entry |
| Incomplete structure | Unknown; do not propose an entry |
| EMA | M15 EMA(8), SMA-period seed |
| Crossover | Prior close vs prior EMA and current close vs same-bar EMA |
| Engulfing | Opposite-direction prior body; inclusive body boundaries |
| Displacement | Current body at least `1.2 ×` the median of the previous 20 M15 bodies |
| Even median | Arithmetic mean of the middle two values |
| Doji | Neither bullish nor bearish |
| Candle evidence | Engulfing **or** displacement |
| Touch-bar confirmation | Allowed |
| Confirmation lifetime | Touch bar through two later M15 closes |

The EMA seed, inclusive engulfing boundary, touch-bar eligibility, and two-bar lifetime are
predeclared experiment choices. They are deliberately not hidden defaults.

## Candidate-decision timeline

For every completed M15 observation, the diagnostic engine evaluates only information available at
that exact lifecycle transition:

1. admit and update zones using the sealed H1/M15 receipt group;
2. select the latest regime that is visible under the configured equal-time order;
3. find an eligible demand retest for bullish context or supply retest for bearish context;
4. require a direction-matched M15 EMA-and-candle confirmation inside its declared touch window;
5. emit `BUY`, `SELL`, or `NO_TRADE` with evidence keys and rejection reasons; and
6. if accepted, record only a **proposed** next-M15-open time.

An input that becomes available after that proposed time is rejected as late evidence. No candidate
is backdated, and no candidate contains a fill price, quantity, stop, target, account value, P&L,
or broker instruction.

## Replay boundary

`replay-diagnostic` is the first complete consumer of this bundle. It accepts only a gap-free,
whole-hour M15 bid/ask CSV whose strict v1 sidecar manifest binds the exact raw and normalized hashes.
The replay derives H1 bars, ATR, pivots, regime, impulses, zones, lifecycle transitions, M15 EMA and
candle evidence, confirmations, and one paper candidate decision per M15 bar. Early warm-up bars
remain explicit `NO_TRADE` records rather than being discarded.

The current bundle requires 96 M15 pre-roll bars: 20 complete H1 bars for the finite origin search
before the four-H1 impulse, which also covers ATR, pivot-window, EMA, and displacement history.
Because replay input must end on a complete UTC hour, the minimum accepted file is 100 M15 bars and
contains four `post_pre_roll` decisions. All earlier rows are labeled `pre_roll` and excluded from
post-pre-roll action counts. The lifecycle also declares
`empty_state_at_dataset_start`; zones that formed before the dataset remain unknown.

Its JSON and CSV outputs are diagnostic evidence artifacts. A `BUY` or `SELL` row, if one occurs,
is a next-open research candidate—not a submitted or simulated order—and contains no quantity,
fill, position, stop, target, P&L, or claim that the strategy makes money.

The separate `replay-session-features` command uses this bundle's ATR(14) and EMA(8) policies with
a v2 dataset manifest and an explicit finite calendar. It computes only H1 bars, ATR, confirmed H1
pivots, and M15 EMA across exact declared whole-hour closures, preserving actual timestamps and
history. A two-session synthetic fixture (184 M15 bars, one one-hour closure) yields 46 H1 bars,
177 EMA events, and 33 ATR events. That feature-only command does not generate zones, lifecycle,
confirmations, or candidates and does not assess full strategy readiness. `check-replay-data`
remains a strict v1 strategy preflight. See [SESSION_FEATURE_REPLAY.md](SESSION_FEATURE_REPLAY.md).

## Separately versioned session diagnostic baseline

`provisional_session_diagnostic_baseline_v1(calendar)` retains the selections above but binds
every relevant policy to one finite calendar and the `xauusd-session-strategy-v1` semantics.
Its baseline ID is `xauusd-supply-demand-session-provisional`; it remains unapproved. The original
strict bundle and its canonical identity are unchanged. Results from the two bundles must not
be pooled as if their timing semantics were identical.

| Closure-sensitive area | Provisional session selection |
| --- | --- |
| Supported calendar | Explicit, dataset-bound closures with whole UTC-hour endpoints only |
| Indicator/formation history | Actual open bars; no invented candles, timestamp compression, or seed reset |
| H1 impulse and origin lookback | Four actual H1 bars and up to 20 prior actual H1 bars, respectively |
| Pivot confirmation | Three actual right-wing H1 bars, available only after all dependencies |
| Zones | Preserve across the closure; invalidate only on the applicable observed H1 close |
| Active retest episode | Preserve across the closure; end on the next observed price exit or invalidation |
| Confirmation lifetime | Elapsed M15 intervals, including closed time; the two-interval lifetime does not pause |
| Entry at a closure | Explicit `NO_TRADE`; do not queue an entry for reopening |
| Entry beyond finite calendar evidence | Explicit `NO_TRADE`; require a fully covered next M15 interval |
| Warm-up | First 96 actual M15 bars are pre-roll, even when a closure intervenes |

`replay-session-diagnostic` consumes manifest v2 plus the bound calendar and emits the full offline
event trace and one decision row per M15 bar. Both output paths are required and must not already
exist. The JSON embeds calendar/session metadata, the CSV adds calendar/semantics provenance,
and export verifies the stored result against exact replay recomputation.
Market evidence is gated point-in-time, but calendar retrieval is not gated by `as_of`.
Session semantics explicitly record that the supplied schedule is assumed known rather than
verified as historically available.

The current synthetic two-session fixture yields 12 zones, 4 confirmations, and 184 decisions
(96 pre-roll, 88 post-pre-roll). This is an implementation regression, not strategy performance
or a verified Exness sample. See [SESSION_DIAGNOSTIC_REPLAY.md](SESSION_DIAGNOSTIC_REPLAY.md).

## Gates before P&L simulation

The following remain unresolved and prevent honest performance claims:

- M15 swing definition and confirmation delay for the structural stop;
- ATR stop-buffer snapshot, tick rounding, and gap behavior;
- target selection, tie handling, and behavior when no valid target exists;
- bid/ask execution triggers, spread, slippage, financing, commission, and same-bar priority;
- exact feed/session/calendar contract and licensed historical XAU/USD data;
- fixed-fractional sizing and account-wide daily/weekly risk transitions; and
- a frozen walk-forward manifest with untouched holdouts and predeclared pass/fail thresholds.

The next validation step is to inspect an authorized M15 sample and verify its calendar and
provenance. `check-replay-data` reports strict strategy compatibility; for feeds with closures,
the implemented session diagnostic path allows full event-trace reconciliation with manifest v2
and the separately versioned calendar-bound policy. Manually reconcile its zone, lifecycle,
confirmation-expiry, and candidate-timing behavior before building the separate event-driven P&L
simulator. No real dataset or Exness schedule has been verified, and no demo trades have been
sent by this implementation. See [the data intake checkpoint](DATA_INTAKE.md).
