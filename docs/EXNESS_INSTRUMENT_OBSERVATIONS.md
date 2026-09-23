# XAUUSDm specification observations

Observed from three user-supplied MT5 screenshots dated 2026-09-23. These are
source observations, not an executable broker configuration or approval to trade.
They describe the settings displayed at capture time, not a verified historical
schedule or historical swap rate for every backtest date.

## Confirmed visible settings

| Setting | Displayed value | Interpretation for this project |
| --- | --- | --- |
| Symbol | XAUUSDm | Keep the broker suffix; normalize to XAU_USD only with explicit provenance |
| Description / category | XAU/USD, Gold vs US Dollar / Metals | Gold against USD |
| Digits | 3 | Price display precision, not independently verified minimum tick size |
| Contract size | 100 XAU | 100 underlying units per lot |
| Spread | Floating | The sample's constant 0.260 spread is not a permanent broker spread |
| Stops level | 0 | Observed minimum-distance setting in points; not a promise that every order is valid |
| Margin / profit currency | XAU / USD | Keep margin and profit calculations distinct |
| Calculation / chart mode | Forex / By bid price | Chart candles cannot replace the separate ask side |
| Trade / execution | Full access / Market | Displayed symbol permission, not a bot connection or authorization to place an order |
| GTC mode | Good till cancelled | Observed pending-order lifetime mode |
| Filling | Fill or Kill, Immediate or Cancel | Both modes displayed; partial-fill handling remains connector work |
| Expiration / orders | All / All | Displayed labels, not separately exported bit masks |
| Minimum lots | 0.01 | Broker volume floor, not an instruction to always round up to it |
| Maximum lots | 200 | Broker displayed ceiling, not the bot's risk budget or a recommended size |
| Lot step | 0.01 | Lot sizing must respect this increment |
| Swap type | In points | Values below are not dollar amounts |
| Swap long / short | -536.4 / 0 | Snapshot values, not verified historical or account-billed costs |
| Swap multipliers | Mon 1, Tue 1, Wed 3, Thu 1, Fri 1 | Requires direction and rollover-date-aware financing |

These meanings follow the official
[MT5 specification guide](https://www.metatrader5.com/en/terminal/help/trading/market_watch).
The [MQL5 symbol-property reference](https://www.mql5.com/en/docs/constants/environment_state/marketinfoconstants)
separately defines digits, point, tick size, profitable/losing tick values, and
freeze level. Do not silently equate three decimal places with a verified
`SYMBOL_TRADE_TICK_SIZE` or infer tick value from a display label.

## Sessions visible in the screenshots

The Quotes and Trade columns show the same visible text. The Mon–Thu second
session end is truncated. Preserve that uncertainty instead of filling it in.

| Day | Visible session text, server clock |
| --- | --- |
| Sunday | 22:01–24:00 |
| Monday–Thursday | 00:00–20:58, 22:00–24… (end clipped) |
| Friday | 00:00–20:58 |
| Saturday | Blank; no session shown |

Exness's [timezone documentation](https://get.exness.help/hc/en-us/articles/360014390760-What-is-the-default-timezone-set-for-MetaTrader)
documents GMT+0 synchronized to its servers. The screenshots themselves contain
no timezone label. The weekly schedule is not a finite, date-specific calendar:
holiday exceptions, historical changes, and quote-only/close-only periods need
separate evidence before replay.

The **20:58 close** and **Sunday 22:01 opening** are enough to establish a mismatch
with the original whole-hour-only session importer and replay. The separate
[partial-session importer](PARTIAL_CANDLE_IMPORT.md) now preserves those edges;
full strategy replay integration is still pending. For example, an
ordinary 20:45–21:00 M15 interval straddles the displayed close. Treating it as a
fully open interval or rounding the broker closure to 21:00 would be false.
Likewise, a nominal Sunday 22:00 candle includes time before the displayed open.

## Configuration mapping and remaining work

The confirmed contract and volume observations can inform `PaperInstrument`:
`contract_size=100`, `min_lots=0.01`, `max_lots=200`, and `lot_step=0.01`.
The observed zero stops level corresponds to zero minimum price distance at
capture, but runtime broker checks still matter. **No PaperInstrument was created
or simulator defaults changed**, because the broker tick size is not confirmed.

Not shown or not established:

- Explicit point, tick size and profitable/losing tick values.
- Freeze level and any aggregate volume limit.
- Applicable account commission and swap-free/exemption status; absence from a
  screenshot is not proof of zero cost.
- Fully visible Mon–Thu session endpoints and dated historical exceptions.
- Contracting legal entity, data-use/retention basis, and quote receipt timing.

The displayed approximately 217.18 USD/lot buy margin and 217.17 USD/lot sell
margin are indicative values at the time the specification opens, not constants
to hardcode or measures of stop-loss risk. The official MT5 guide documents this
snapshot behavior. The current offline engine does not model broker margin.

The existing `financing_per_lot_per_utc_day` scalar also cannot faithfully express
different long/short swaps and the Wednesday multiplier. Do not copy -536.4 into
that field, take its absolute value, or silently replace it with zero. Resolve
swap units, effective dates, rollover timing, and account applicability before
testing positions across rollover.

The separate importer now explicitly models partial-session coverage and
distinguishes partial from complete M15/H1 observations. The user selected
inclusion of observed partial candles in indicators and signals; the versioned
input plan records that choice. Next development must integrate it into the
full strategy and test warm-up/indicator timing across the real break.
Preserve raw ticks and actual closure timestamps. Do not
remove existing guards, invent closed-period candles, silently discard edge
observations, or relabel rounded analysis exclusions as broker closures. Any
choice about using partial bars must be documented as a strategy policy.

The remaining terminal properties can be obtained through a read-only symbol
metadata export if they are not exposed in this dialog. This needs no trade or
new account. It is separate from executing the bot.

## Evidence identities

Original attachments were inspected without alteration. SHA-256 values:

- `Screenshot 2026-09-23 at 02.35.07.png`:
  `e927162b02c9107eb29b2c904c6ff4bee1611e826677bcd29fc59c1d79dfd36f`
- `Screenshot 2026-09-23 at 02.35.20.png`:
  `e68e9430f532b034e5c336f41128cd08be5a3a979ed76abdef9ae50f45dee942`
- `Screenshot 2026-09-23 at 02.35.29.png`:
  `ab169741a4dfff999752a76fd5a27291ad384e9836bc6c54dfddba8da170ba83`

The original screenshot review changed only documentation. The subsequent
partial-session importer is offline; no account settings, broker orders, or
source CSV files were changed.
