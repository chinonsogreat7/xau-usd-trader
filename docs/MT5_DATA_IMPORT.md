# Offline MT5 quote-tick import

This path converts an explicitly supported local MT5 quote-tick export into the project's M15
bid/ask CSV, a dataset manifest v2, and a diagnostic import report. It does not connect to MT5,
download history, run a strategy, calculate returns, or submit orders. A successful import checks
structure and the consistency of supplied provenance; it does not verify a broker's data rights,
the completeness of its history, or executable prices.

The first user-supplied Exness XAUUSDm format probe passed on 2026-09-23: 16,066 complete
bid/ask rows were accepted by the existing parser. See [the sample findings](EXNESS_FIRST_SAMPLE.md).
This was not a production import or bot activation. A bound historical trading calendar,
complete source metadata, and strategy/execution validation remain outstanding.

This page describes the original **whole-hour-only** `import-mt5-ticks` route.
For minute-level closes/reopens, the separate
[partial-session importer](PARTIAL_CANDLE_IMPORT.md) preserves all observations and
records the user's include-partials policy. That new bundle is not yet admitted
to the full strategy replay and does not produce a manifest v2 or normalized CSV.

## Get a small sample from MT5 desktop

1. Install MT5 using the [official macOS installation guide](https://www.metatrader5.com/en/terminal/help/start_advanced/install_mac).
   The MetaQuotes installer sets up the Wine-based desktop environment. Installing the terminal
   does not install a Python broker connector or connect this project to an account.
2. Sign in yourself to the existing **demo** account using its correct broker/server details.
   Keep Algo Trading off, do not attach an Expert Advisor, and do not place a test order. Keep
   passwords and account numbers out of chat, exported metadata, and this repository.
3. In Market Watch, open **Symbols** from the context menu, select the actual gold instrument,
   and use its **Ticks** history tab. Request a small completed period, then export it. Use tick
   history, not the current Market Watch quote snapshot or an ordinary chart/bar export.
   This route is described in [MetaQuotes' price-history instructions](https://www.metatrader5.com/en/terminal/help/trading/market_watch).
4. Begin with one completed UTC hour during a verified open period. Retain the unedited export,
   the exact broker symbol including any suffix, and redacted symbol-specification evidence.
   Obtain the historical server-time-to-UTC rule and schedule for the selected dates. Do not
   infer the server offset from the computer's timezone or assume today's offset applied then.
5. Check whether every exported tick includes both a positive bid and a positive ask. If the
   file contains empty/zero quote sides or a different format, keep the original and stop:
   this importer must not invent missing prices to make it pass.

A one-hour probe is enough to check supported formatting and aggregation. It is not enough for
the session strategy replay, which requires at least 100 actual M15 bars, including 96 pre-roll
bars. A feed's daily closure can therefore require more than one session of history. Select the
larger period only after the small sample, historical offset, data permissions, and calendar
have been checked. There is no promise that every MT5 or Exness export matches this first parser.
The immediate user handoff is the raw export and redacted symbol/timezone evidence; the import
plan can then be prepared together from those facts. There is no need to guess or manually fill
every metadata field before the format probe has been inspected.

## Supported input and explicit assumptions

The parser accepts comma- or tab-delimited quote-tick files with these exact ordered headers:

```text
<DATE>,<TIME>,<BID>,<ASK>,<LAST>,<VOLUME>
```

An optional seventh header is `<FLAGS>`. Files may be UTF-8, including a UTF-8 BOM, or UTF-16
with a BOM. Dates use `YYYY.MM.DD`; times use `HH:MM:SS` with optional millisecond precision.
The six-column shape is documented in [MetaQuotes' tick-data format](https://www.metatrader5.com/en/terminal/help/trading_advanced/custom_instruments).
This project's requirement for complete positive bid/ask pairs is deliberately narrower than
the possible contents of MT5 tick history.

This first version is bounded to 64 MiB of raw input and 500,000 ticks. The plan and calendar
are each limited to 1 MiB. Larger files are rejected; this is not a streaming full-history
importer. Fractional seconds contain one to three digits.

Each row must provide a positive finite bid and ask, with bid no greater than ask. Blank or zero
quote sides, decreasing timestamps, malformed rows, ordinary OHLC exports, and out-of-coverage
ticks are errors. Equal-millisecond rows retain their original file order; they are not sorted,
deduplicated, or merged. `<LAST>`, `<VOLUME>`, and optional `<FLAGS>` are not a substitute for
either quote side. Normalized bar volume is left unspecified, not relabeled as traded gold
volume or liquidity.
`<LAST>` and `<VOLUME>` may be empty or finite non-negative values. If the `<FLAGS>` column is
present, each row must contain a non-negative integer using only the supported MT5 flag bits
2, 4, 8, 16, 32, and 64; flags do not trigger quote reconstruction.

The import plan must declare a single verified fixed UTC offset for the entire selected period.
Conversion subtracts that offset from each naive server timestamp. No daylight-saving change,
server migration, timezone, or closure is guessed. Split an export that spans an offset change
into separately verified periods before using this version of the importer.

Coverage is explicitly half-open and begins and ends on complete UTC hours. Every raw tick must
fall inside it; the importer does not silently trim a larger export. A tick exactly at a M15
boundary belongs to the following interval, and a tick exactly at the overall coverage end is
outside the selected period.

Within each open M15 interval, bid and ask OHLC are independently derived from the supplied
complete quote pairs. Open and close are the first and last observed values in source order.
Every open M15 interval must contain at least one tick. Empty open intervals, ticks during
closures, undeclared gaps, and partial-hour closure boundaries are rejected. Missing intervals
must match the supplied whole-hour calendar closures exactly; no candles or quote sides are
forward-filled.

For a real user export, normalized bars use `historical_close_assumption` with availability at
the bar end. This is not a measured receipt time or forward-paper evidence. Synthetic fixtures
remain labeled `synthetic`; they must not be presented as acquired broker history.

## Plan, calendar, and command

Keep the exact tick export and dated schedule in `data/raw/`, and normalization outputs in
`data/processed/`. Those data directories are ignored by Git, but that does not make them a
credential store. Check the provider's permission to retain raw and derived data before export.

The required import plan records provider name and legal entity, product form, the exact source
symbol, redacted source/account environment, acquisition and retrieval details, rights and
retention basis, fixed-offset evidence, explicit UTC coverage, and the exact raw-file and
calendar identities. The calendar is a local finite artifact, using the schema in
[DATA_FORMAT.md](DATA_FORMAT.md#dataset-sidecar-manifest-v2-and-explicit-calendars). Its exact
bytes and canonical content are bound separately. Do not supply a fabricated provider identity,
timezone explanation, permission, or schedule simply to satisfy validation.
Input hashes are calculated from the exact byte snapshots parsed, including both metadata files;
the importer also rechecks the files before publication to catch changes during the run.

Its closed JSON schema is `xau_trader.mt5_tick_import_plan`, version `1`. All fields below are
required; unknown or duplicate fields, placeholder claims, and non-standard JSON values fail.

```text
schema, version
provider { name, legal_entity }
instrument { symbol, product_form }
source { route, account_environment, acquisition_basis, source_symbol, retrieved_at }
rights { basis, api_data_agreement_version, retention_basis }
timestamp { utc_offset_minutes, evidence_reference }
coverage { start, end }
raw_sha256
session_calendar { id, version, artifact_uri, artifact_sha256, content_fingerprint }
```

`instrument.symbol` is `XAU_USD`; `source.source_symbol` separately records the actual exported
broker symbol and any suffix. The mapping is an operator claim, not independently verified by
the parser. `source.acquisition_basis` is only `user_export` or `synthetic_generation`, which
determine `historical_close_assumption` and `synthetic` availability respectively. Retrieval must
be at or after coverage end, and the calendar must have been retrieved by dataset retrieval.

`timestamp.utc_offset_minutes` is an integer from −840 to +840 representing **server time minus
UTC**. For example, a verified offset of +120 converts a server timestamp of 02:00 to 00:00 UTC;
this is a sign-convention example, not an Exness timezone recommendation. `evidence_reference`
records the dated source for that choice. Metadata timestamps use explicit UTC. References are
redacted and may not contain URL credentials, query strings, or fragments. Neither the importer
nor a successful hash match independently verifies the recorded rights or timezone evidence.

`raw_sha256` binds the unchanged export bytes. The calendar's `artifact_sha256` binds its exact
file bytes; `content_fingerprint` binds canonical calendar JSON. Digests are lowercase SHA256.
Calendar identity, provider, legal entity, instrument, and product form must match the plan and
resulting manifest; calendar coverage must contain the planned dataset coverage. The generated
manifest records the import-plan fingerprint and
the first-observed-tick open semantics in its source metadata.

Run the importer only after those inputs are complete:

```bash
PYTHONPATH=src python3 -m xau_trader import-mt5-ticks \
  data/raw/gold-ticks.csv \
  --plan data/raw/gold-import-plan.json \
  --calendar data/raw/gold-calendar.json \
  --output-csv data/processed/gold-m15.csv \
  --manifest data/processed/gold-m15.manifest-v2.json \
  --report-json reports/gold-import.json
```

All five options shown are required. Output paths must be new and distinct from all inputs and
from each other; there is no overwrite option. The command reads local files only and does not
fetch source references or calendar URIs. It preserves the raw file and emits the normalized CSV,
manifest v2, and report as separate artifacts. Retain all three together with the raw export,
plan, and calendar for review.
Publication stages all outputs and rejects collisions; it is not a crash-atomic multi-file
transaction. After an interrupted process, compare the retained files with the manifest/report
hashes before using them. Do not treat the mere presence of one output as a completed import.

## What the report does and does not establish

The report records input/normalization identities and per-bar observations, including tick
counts, first/last observed ticks, and silence diagnostics. Review those observations before
comparing imported bars with the terminal chart and independently calculated bid/ask candles.

One tick per open M15 interval is sufficient for structural aggregation; it does **not** prove
that all broker ticks were exported. OHLC values summarize only the supplied rows. In
particular, the first observed tick may be later than the interval's nominal start: a derived
`bid_open` or `ask_open` is not evidence that the price was available or executable at the
strategy's proposed next-open time. An import success must not be treated as P&L-valid data or
permission to connect these bars to an execution simulator.

After independent data and provenance checks, a separate
[session diagnostic replay](SESSION_DIAGNOSTIC_REPLAY.md) can use the normalized CSV, manifest,
calendar, and original tick export (`--raw-data`). That command produces provisional candidate
evidence only. Verified quote-event timing, execution/cost semantics, strategy validation,
risk controls, and a demo-only connector remain separate work before automated demo trading.
