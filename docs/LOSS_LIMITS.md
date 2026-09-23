# Offline daily loss-count and weekly drawdown limits

These controls implement a **provisional research interpretation** of RISK-02 and
RISK-04 in the strategy intake. They do not approve the strategy, create a broker
connection, or provide persistent account-wide protection.

## Explicit configuration

`PaperLossLimits(max_losses_per_day=2, max_weekly_drawdown_fraction=Decimal("0.03"))`
enables the two controls in the quote engine and strategy adapter. There are no
implicit threshold defaults. Passing `None` preserves legacy behavior and reports
the additional controls as disabled.

Synthetic scenario **v2** requires this additional top-level object:

```json
"loss_limits": {
  "max_losses_per_day": 2,
  "max_weekly_drawdown_fraction": "0.03"
}
```

All existing scenario fields remain required. V1 cannot contain `loss_limits`;
v2 cannot omit it or set it to null. Historical/live inputs remain rejected.

To export an explicitly configured *scripted engineering fixture*:

```sh
PYTHONPATH=src python3 -m xau_trader export-paper-scenario reports/guarded-scenario.json \
  --max-losses-per-day 2 --max-weekly-drawdown-fraction 0.03
PYTHONPATH=src python3 -m xau_trader simulate-paper reports/guarded-scenario.json \
  --report-json reports/guarded-report.json
```

Both options must be supplied together and output filenames must be new. This
fixture is not the supply/demand strategy. `replay-strategy-paper` accepts a v2
quote scenario with the same explicit limits and an **empty** intents array,
then derives its own intents from validated strategy evidence.

## Daily rule: total losing closes

- A loss is a closed trade with net P&L strictly below zero after both entry/exit
  commissions and all accrued modeled financing. A gross gain can be a net loss.
- Count losses on the UTC **exit receipt date**, regardless of the entry date.
- The second loss blocks further entries for that UTC day. Losses need not be
  consecutive; a winner does not erase earlier losses. Breakeven does not count.
- Reset on the first received quote of the next UTC day. No synthetic midnight
  event or unobserved trade is created.
- This count is independent of the existing percentage daily loss guard. Either
  can block entries; a day reset never clears a latched global emergency halt.

## Weekly rule: drawdown from equity high water

- A week is the UTC ISO week, starting Monday, using ISO week-year as well as
  week number. A calendar year change inside the same ISO week does not reset it.
- Track the high-water liquidation equity, including unrealized P&L, modeled
  adverse exit slippage/commission, and accrued financing. A standing target
  caps favorable marks consistently with the existing engine.
- The first observed week starts from initial cash. Each later observed week
  starts from the **previous observed equity**, before applying the new quote's
  price gap and financing, then incorporates current marks. This is an explicit
  sparse-data convention, not an assertion of a known midnight account balance.
- At or beyond a 3% decline from this weekly peak, latch the weekly halt until
  the first received quote in a new UTC ISO week. Price recovery or a daily reset
  cannot clear it within the week.
- Block entries and close an existing position at the first usable fresh quote.
  Stale quotes can halt the run but never produce an executable fill; an independent
  global stale-data halt remains latched even after a week reset.
- Limit new position sizing by the remaining weekly allowance, alongside the
  existing trade-risk, daily-loss, and overall-drawdown allowances.

Guards are checked before exits/entries, after closes and entry costs, and after
final forced liquidation. Exit priority is global/emergency halt, daily percentage
loss, daily loss count, weekly drawdown, then normal stop/target/holding exits.
Stop gaps, future financing, and unmodeled broker behavior can still cause losses
beyond configured caps; these limits are not a maximum-loss guarantee.

## Audit and remaining limitations

Versioned definitions, thresholds, and fingerprints are included in reports. Events
and equity snapshots retain daily counts, week identity/peak, and halt states.
Trade records say whether a close counted as a loss. Legacy runs explicitly mark
the controls disabled rather than presenting missing state as zero losses.

State exists only within a single offline run. Restart persistence, real account
reconciliation, broker-specific sessions/costs/margin and emergency execution are
not implemented. UTC definitions and source interpretation remain unapproved.
`source_risk_policy_complete=false` and `promotion_eligible=false` therefore remain
correct even when `loss_limits_configured=true`.
