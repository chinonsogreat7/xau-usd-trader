# First Exness gold tick sample

Checked on 2026-09-23. Result: the raw export passes the format probe and the
existing MT5 parser. This is not a completed production import, strategy backtest,
or bot-to-broker connection.

## Source and checks

The user exported `XAUUSDm_202609221200_202609221259.csv` from their Exness MT5
demo terminal. The original was read without alteration. No account number,
password, or screenshot containing account information is included here.

| Check | Result |
| --- | --- |
| File size | 755,153 bytes |
| Format | UTF-8-compatible ASCII, tab-delimited, CRLF lines |
| Columns | DATE, TIME, BID, ASK, LAST, VOLUME, FLAGS in MT5 angle-bracket notation |
| Data rows | 16,066; all accepted by the existing parser |
| First file-clock timestamp | 2026-09-22 12:00:00.111 |
| Last file-clock timestamp | 2026-09-22 12:59:59.413 |
| Missing or invalid bid/ask | 0 |
| Crossed quotes (bid above ask) | 0 |
| Decreasing timestamps | 0 |
| Adjacent equal timestamps | 81; source order preserved |
| Exact duplicate rows | 0 |
| Minutes containing observations | 60 of 60 |
| Largest gap between adjacent observations | 2.797 seconds |
| Observed ask-minus-bid spread | 0.260 price units in every supplied row |
| M15 interval observation counts | 3,319; 4,645; 4,337; 3,765 |
| Blank LAST and VOLUME | All rows; permitted unused fields, not missing bid/ask |
| FLAGS | 6 in every row |

Raw SHA-256:

`9fc30a08d480f556ba4bc277095f43e6d3280cf0ba8d7bcf9252da1ccf325092`

An independent JavaScript audit and the project's Python parser agreed on the
row count, hash, timestamps, flags, equal-time observations, largest gap, spread,
and all four exploratory bid/ask OHLC groups. The original hash was checked again
after analysis. All **58 MT5 parser/import-plan/import tests** passed. No project
parser or execution code was changed for this check.

The detailed local audit and reproducibility scripts are in the ignored
`reports/exness-tick-probe-20260923-pU7kmP/` directory. No normalized replay dataset,
calendar, import plan, or dataset manifest was manufactured for this probe.

## Time and symbol evidence

[Exness's timezone guidance](https://get.exness.help/hc/en-us/articles/360014390760-What-is-the-default-timezone-set-for-MetaTrader),
updated 2026-08-22 and checked 2026-09-23, documents GMT+0 synchronized to its
trading servers. Its [trading-hours guidance](https://get.exness.help/hc/en-us/articles/4405235684498-Instrument-trading-hours),
updated 2026-09-21, independently states UTC+0. The parser check used offset 0 on
this documented basis, not the Mac's timezone. The CSV itself has no timezone
field; these policy sources do not establish an entire historical session calendar.

[Exness's suffix documentation](https://get.exness.help/hc/en-us/articles/360014560220-Account-type-suffixes),
updated 2026-09-09, lists XAUUSDm under Standard accounts. Retain this exact symbol
in source provenance. Do not substitute the synthetic fixture's instrument costs
or lot specifications for the actual XAUUSDm contract.

## What this establishes

The supplied export is structurally compatible with the current complete-bid/ask
parser. All four quarter-hour intervals contain observations. It does not prove
the broker exported every tick, that an observed price was executable, or that a
strategy would make money. Equal-time rows have not been sorted or deduplicated.
The first observations are 111, 155, 344, and 141 milliseconds after the respective
quarter-hour boundaries; they are not exact boundary-price evidence. Historical
tick timestamps also do not establish when a live bot would have received them.

## Next work

1. The user supplied **XAUUSDm Specification** screenshots on 2026-09-23. The
   confirmed contract/lot settings, current swaps and partial-hour session edges
   are recorded in [the specification observations](EXNESS_INSTRUMENT_OBSERVATIONS.md).
   Explicit tick size/value, freeze level, account cost applicability and clipped
   session endpoints remain unverified. Do not substitute fixture values.
2. Complete truthful source, legal-entity, retention/use and dated calendar
   evidence for the import plan. A terminal's software-company label alone is
   not proof of the client's contracting legal entity.
3. The supplied specification shows a 20:58 close and Sunday 22:01 opening;
   the original session import/replay accepts only whole-hour closures. The new
   [partial-session importer and inclusion plan](PARTIAL_CANDLE_IMPORT.md) preserves
   these exact boundaries; full strategy integration is still pending. Resolve the
   remaining schedule evidence before selecting a longer sample. Do not
   round an actual partial-hour closure, invent candles, or drop inconvenient
   ticks to make that restriction pass. The user selected inclusion of observed
   partial candles; do not replace that choice with a full-candles-only filter.
4. Acquire sufficient history within the current 64 MiB / 500,000-tick per-file
   limits, or extend bounded multi-file handling first. Existing whole-hour session diagnostics need
   at least **100 actual M15 bars**, including **96 warm-up bars**: at least 25
   observed open hours. That minimum starts diagnostics; it is not enough by
   itself to validate profitability. The partial-session adapter must recompute
   readiness from actual dependencies, not blindly apply that M15 count.
5. Extend real-data execution admission and validate strategy/cost/timing rules
   before P&L replay. The present strategy execution adapter is synthetic-only;
   the calendar-bound real-data path produces diagnostic candidates, not fills
   or profit. A demo-only broker connector and its safety checks remain separate.

No orders were submitted, no account settings changed, and no trading process
was started. The user-confirmed MT5 terminal login is distinct from a connection
between this Python project and the account.
