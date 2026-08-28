# ADR 0001 — Blocked OANDA practice-data candidate

- Date: 2026-08-25
- Status: **Blocked pending written confirmation; no credentialed transport exists**

## Decision

Use OANDA v20 only as the first offline schema/request candidate while retaining the provider-neutral
boundary. Ship no credential loader or network transport. Do not configure or contact an account
until the provider gate below is supported by current written evidence.

This is not a broker selection or a recommendation to open, fund, or trade through OANDA. No
current official source establishes a Nigerian-resident route to an OANDA entity that serves v20,
and no particular XAU/USD contract has been established as appropriate or lawful.

## Why this is blocked

The current official sources provide no established Nigerian route to a v20-serving entity:

- OANDA's [region selector](https://www.oanda.com/region-selector/) includes Nigeria, but navigation
  or application access is not eligibility confirmation. An older OANDA Europe help page says
  residents of the UK, Middle East, and Africa
  [may apply](https://help.oanda.com/uk/en/faqs/open-live-account-uk.htm). Application access is not
  account approval.
- OANDA's newer [division guidance](https://help.oanda.com/eu/en/faqs/oanda-division.htm) gives a
  narrower description for OANDA Europe and directs applicants to test their country during
  registration.
- Nigeria is absent from OANDA Global Markets'
  [published eligible-country list](https://help.oanda.com/bvi/en/faqs/eligible-ogm-countries-bvi.htm).
- OANDA says v20 is available to all divisions except OANDA Global Markets and OANDA TMS
  [in its v20 introduction](https://developer.oanda.com/rest-live-v20/introduction/). Therefore an
  account routed to either excluded division would invalidate this adapter.

Do not bypass residency controls with a VPN, false address, nominee, or different country selection.

## Options considered

| Option | Finding | Decision |
|---|---|---|
| OANDA v20 practice | Simple token-authenticated REST API, separate documented practice host, account-scoped instrument entitlement, and bid/ask candles with a 5,000-candle request maximum. No current official source establishes a Nigerian-resident route to a v20-serving entity. | Blocked schema candidate |
| Interactive Brokers | Nigeria is absent from IBKR's [available-country list](https://www.interactivebrokers.com/en/accounts/open-account-country-list.php). Its paper API normally depends on an approved, funded regular account and has materially more gateway/authentication complexity. | Do not implement now |
| Saxo OpenAPI SIM | Potential bid/ask chart API and simulation environment, but no authoritative Nigerian eligibility conclusion was found and some SIM market data may be unavailable. | Fallback only after written confirmation |
| Unlicensed public midpoint feed | Does not preserve executable bid/ask prices and may not grant storage, redistribution, or model-training rights. | Reject |

IBKR's paper-account and API constraints are documented in its
[paper-trading limitations](https://www.interactivebrokers.com/docs/tws-api/doc/notes-limitations/limitations/paper-trading)
and [Web API documentation](https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/).
Saxo documents its [SIM/LIVE environments](https://www.developer.saxo/openapi/learn/environments)
and [chart endpoint](https://www.developer.saxo/openapi/referencedocs/chart/v3/charts/get__chart).

## Provider gate

Before the first authenticated request, retain private evidence for all of the following:

1. Written confirmation that the provider may open and maintain this type of account for a resident
   of Nigeria.
2. The exact contracting affiliate, company/licence number, and regulator-register match.
3. An account agreement or provider response confirming v20 practice API access.
4. The account's instrument response containing `XAU_USD`, including the provider's API `type`
   metadata. This proves only runtime entitlement, not legal product classification.
5. The exact entity's written confirmation and product documents identifying the legal form: CFD,
   OTC spot metal, future, or something else.
6. The exact entity-specific API/data agreement and written clarification of private storage,
   retention, derived features, backtesting, and any AI/model use.
7. A practice-only account identity that is not capable of reaching live funds with the supplied
   credential. If OANDA cannot provide that separation, do not supply the token to this project.

The Nigerian SEC has separately warned about retail online forex and foreign-provider claims; a
foreign licence or successful signup does not itself establish Nigerian authorization. See the
SEC's [online retail forex notice](https://sec.gov.ng/for-investors/keep-track-of-circulars/public-notice-online-retail-foreign-exchange-forex-trading/)
and [foreign CFD-provider warning](https://sec.gov.ng/for-investors/keep-track-of-circulars/suspected-fraudulent-activities-of-modmount-services-limited/).
Live funding remains out of scope regardless of this gate.

## Technical contract

The offline request/normalization boundary represents only:

- an immutable `GET` request plan pinned to `https://api-fxpractice.oanda.com`;
- account summary, `XAU_USD` entitlement, and `XAU_USD` candle operations only;
- a five-minute account/instrument attestation required by every fake-data fetch;
- canonical `XAU_USD` only;
- canonical `PT1H`, mapped to OANDA `H1` only;
- `price=BA`, `smooth=false`, and frozen `units=1`;
- completed, hour-aligned, non-future candles only, with both bid and ask OHLC present;
- a maximum two-hour age for an observed latest completed candle; and
- a maximum of 5,000 candles per request.

The [practice endpoint](https://developer.oanda.com/rest-live-v20/development-guide/),
[account/instrument endpoints](https://developer.oanda.com/rest-live-v20/account-ep/),
[account-scoped candle endpoint](https://developer.oanda.com/rest-live-v20/pricing-ep/), and
[candlestick schema](https://developer.oanda.com/rest-live-v20/instrument-df/) support this mapping.
OANDA defines candle `time` as the interval start, so an H1 candle beginning at 10:00 retains
`start_time=10:00` and receives `timestamp=11:00` as its end. Historical normalization explicitly
assumes close-time availability; an observed latest bar instead receives the adapter receipt time
and cannot be passed to the current historical backtester. Returned data stays inside an attested
envelope containing the retrieval time, explicit acquisition basis, and a SHA-256 fingerprint of
the immutable request plan. OANDA `volume` is the number of prices created in the interval, not
traded gold volume or liquidity.

There is a documentation discrepancy: the current developer portal shows the account-scoped
`/v3/accounts/{accountID}/instruments/{instrument}/candles` route, while OANDA's published OpenAPI
repository still describes `/v3/instruments/{instrument}/candles`. The adapter pins the current
account-scoped route and must not silently fall back. Confirm it with OANDA and a practice-only
smoke test before accepting a dataset.

## Credential and data boundary

OANDA states that a personal access token grants access to all subaccounts and must be guarded like
a password in its [authentication documentation](https://developer.oanda.com/rest-live-v20/authentication/).
No granular read-only token scope was found. Consequently:

- never paste a token into chat, source code, command-line arguments, logs, screenshots, notebooks,
  datasets, or a committed environment file;
- if the gate is later satisfied, inject it only into an isolated worker from an operating-system
  credential store or secret manager;
- revoke and rotate it after suspected exposure;
- never add OANDA order, trade, position mutation, or live-host code to this adapter; and
- keep raw OANDA data private and local unless the exact agreement grants broader rights.

Do not upload raw vendor data to a hosted AI service, publish it, or redistribute a dataset without
written permission. The applicable agreement is entity-specific; example OANDA
[API terms](https://legal.oanda.com/oc/api_license_agreement_oc/en) contain restrictions that must
not be assumed identical to this account's eventual agreement.

## Implementation status

`xau_trader.oanda` implements immutable read-request plans, mandatory five-minute preflight state,
attested result envelopes, explicit historical-versus-observed acquisition semantics, clock-
rollback rejection, latest-bar freshness validation, and fail-closed normalization. Automated
tests use fake account identifiers and local fake responses. The project contains no token field,
environment credential factory, HTTP client, or broker order method, and no broker request has been
made. A validated approval record, isolated credentialed transport, redirect/egress controls, CLI,
pagination, raw-response archive, provider request-ID audit, revision checks, and an authoritative
provider session calendar are deferred until the gate is satisfied. A provider-neutral, hash-bound
offline dataset-manifest validator now exists, but no OANDA-specific manifest can be truthfully
completed until those provider/entity/rights/calendar facts and the raw acquisition path are
resolved.
