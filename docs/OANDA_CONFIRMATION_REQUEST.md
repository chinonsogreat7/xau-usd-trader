# OANDA eligibility and data-use confirmation request

Do not include an API token, password, identity document, or account number in the first message.
Send this through the contact option on OANDA's
[official Help Centre](https://help.oanda.com/uk/en/home.htm) and retain the dated response privately.

## Message template

> I am a resident of Nigeria considering a private, paper-only research project. Before opening or
> configuring any account, please confirm the following in writing:
>
> 1. May OANDA currently open and maintain a practice/demo account for a resident of Nigeria?
> 2. Which exact OANDA legal entity would be the contracting provider? Please provide its registered
>    company number, regulatory licence number, and regulator.
> 3. Would that account be a v20 account with access to the REST practice host
>    `api-fxpractice.oanda.com` and personal API tokens?
> 4. Would the account's instrument list include `XAU_USD`? Is that product a CFD, OTC spot metal,
>    future, or another legal form for this entity?
> 5. May its API be used to download private historical bid/ask H1 candles for local research and
>    backtesting? Please identify the exact API/data agreement and version that would govern.
> 6. May I privately retain raw candles, calculate and retain derived indicators/features, and use
>    those features for local machine-learning research? Are raw-data submission to a third-party AI
>    service, redistribution, publication, or commercial use prohibited without separate permission?
> 7. Can OANDA issue a credential that is restricted to a practice account and read-only market data,
>    and that cannot access any live subaccount or submit an order? If not, please confirm the token's
>    actual scope.
> 8. Which account-scoped historical-candle route is current for this entity:
>    `/v3/accounts/{accountID}/instruments/{instrument}/candles` or
>    `/v3/instruments/{instrument}/candles`?
>
> This request is for private paper research only. I am not seeking permission to manage client
> funds, publish signals, solicit investments, or trade live money.

A support response can document OANDA's contractual willingness and data permissions. It does not
by itself establish Nigerian regulatory authorization or make live funding part of this project.

## Evidence to retain

- the official support case/reference number and full dated response;
- the contracting entity and regulator-register result;
- the applicable customer, API, and data agreement versions;
- a redacted screenshot showing the account is practice/demo and v20; and
- a redacted instrument/API result confirming `XAU_USD` without revealing an account ID or token.
