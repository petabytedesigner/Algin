# OANDA v20 contracts used by Algin

Verified against OANDA official REST-v20 documentation, 2026-11-15.

- Practice base: `https://api-fxpractice.oanda.com/v3`; never a live endpoint.
- `GET /v3/accounts/{accountID}`: `account.NAV`, `account.marginAvailable`, `account.currency`, `account.trades`, `account.orders`, `lastTransactionID`.
- `GET /v3/accounts/{accountID}/instruments`: `pipLocation`, `displayPrecision`, `tradeUnitsPrecision`, `minimumTradeSize`, `maximumOrderUnits`, `marginRate`.
- `GET /v3/accounts/{accountID}/pricing?instruments=X&includeHomeConversions=true`: `prices[].tradeable`, timestamp, best bid/ask, `homeConversions[].accountLoss`, `homeConversions[].positionValue`.
- `GET /v3/accounts/{accountID}/instruments/{instrument}/candles`: use `complete=true` and strictly increasing timestamps; ignore incomplete candle.
- `POST /v3/accounts/{accountID}/orders`: `MARKET`, signed `units`, `FOK`, `priceBound`, `positionFill=OPEN_ONLY`, `stopLossOnFill`, `takeProfitOnFill`; never retry blindly.
- `orderFillTransaction.tradeOpened.tradeID` identifies an opened trade; `orderCreateTransaction.id` is **not** a trade ID. `tradeReduced` and `tradesClosed` are separate outcomes.
- `PUT /v3/accounts/{accountID}/trades/{tradeSpecifier}/orders`: omitting `takeProfit` preserves it; sending null cancels it.
- `PUT /v3/accounts/{accountID}/trades/{tradeSpecifier}/close`: positive units for partial close even for SHORT; `ALL` for full close.
- `GET /v3/accounts/{accountID}/changes?sinceTransactionID=...` for state refresh, validated against broker snapshot.

Refs:
https://developer.oanda.com/rest-live-v20/account-ep/
https://developer.oanda.com/rest-live-v20/pricing-ep/
https://developer.oanda.com/rest-live-v20/pricing-df/
https://developer.oanda.com/rest-live-v20/order-df/
https://developer.oanda.com/rest-live-v20/transaction-df/
https://developer.oanda.com/rest-live-v20/trade-ep/
https://developer.oanda.com/rest-live-v20/primitives-df/

Important: some official sample responses still illustrate deprecated fields. Prefer the schema over an old sample.
