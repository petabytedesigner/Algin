# Faza A — Kategoria 03: Ledger financiar i bazuar ne transaksionet OANDA

## Fusha dhe statusi

Kategoria 03 regjistron fakte monetare te brokerit me `transaction_id`, `account_id` te kontrolluar nga `AccountReconciler`, dhe cursor te ruajtur atomikisht me gjendjen e llogarise. **Nuk aktivizon porosi OANDA, nuk nxjerr risk allowance dhe nuk certifikon histori para baseline-it.** Komponentet e financave jane vec nga perllogaritja legacy `TradeRecord.net_pl_home` / `closed_results.pl`; migrimi i vendimeve te riskut te ledger-i i verifikuar i perket Kategorise 04.

## Burimet OANDA v20 (review 2026-11-16)

- [Transaction definitions](https://developer.oanda.com/rest-live-v20/transaction-df/): `ORDER_FILL.pl`, `financing`, `commission`, `guaranteedExecutionFee`, `halfSpreadCost`, `tradeOpened`, `tradeReduced`, `tradesClosed`; `DAILY_FINANCING` dhe `DIVIDEND_ADJUSTMENT` me femijet e tyre.
- [Transaction endpoints](https://developer.oanda.com/rest-live-v20/transaction-ep/): `GET /v3/accounts/{accountID}/transactions/sinceid?id=<cursor>`.

## Kontrata financiare

Per nje `ORDER_FILL` te plote, **account-home-currency cash result** per periudhen llogaritet nga fushat e dallueshme:

`pl + financing - commission - guaranteedExecutionFee`

`halfSpreadCost` eshte vetem metrike diagnostikuese e ekzekutimit: spread-i eshte pasqyruar ne cmimet e mbushjes, keshtu qe **nuk zbritet perseri**. Komisioni/garantimi kerkojne vlere explicite dhe jo negative. Financing eshte me shenje (plus ose minus).

- `DAILY_FINANCING`: `financing` me shenjen e brokerit; `positionFinancings[].openTradeFinancings[]` perdoret vetem kur eshte i pranishëm dhe perputhet me shumen.
- `DIVIDEND_ADJUSTMENT`: `dividendAdjustment`, me `openTradeDividendAdjustments[]` nese jepen dhe pajtohen.
- `TRANSFER_FUNDS`: dallohet dhe regjistrohet, **perjashtohet nga trading P/L**. Nuk trajtohet si fitim ose humbje.
- Transaksione te panjohura dhe vlera monetare te munguara prodhojne `account_net=None`, jo zero te shpikur. Mospërputhjet ne shumat e deklaruara te brokerit e bllokojne batch-in para commit-it.
- Kur nje `ORDER_FILL` prek disa tregti dhe komisionet nuk mund te alokohen nga fushat per-trade, `trade_summary(...).complete=False`; account-level net mbetet i llogaritshëm nese te gjitha fushat e agreguara jane te plota.

## Persistenca / kompatibiliteti

- `LifecycleStore` ruan `financial_ledger` ne te **njejtin state JSON** (checksum, fsync, atomic replace) me `processed_transactions` dhe `cursor`. Skema legacy e lifecycle version 1 pranohet pa reset; ledger-i i ri fillon vetem pas cursor-it te ruajtur.
- `financial_ledger.baseline_cursor` nuk perfaqeson domosdoshmërisht fillimin e llogarise. Nje state legacy pa ledger migrohet vetem kur snapshot-i i brokerit pajtohet, me baseline te barabarte me cursor-in ekzistues.
- `events` indeksohen me `transaction_id`. Duplicate, out-of-range, te paperpunuar, numerike jo-finite dhe evente me rezultate te paharmonizuara nuk pranohen.
- `summary(ledger)` jep `net_pl_home_since_cursor` vetem nese ka evidence te plote per *çdo* transaksion monetar relevant pas baseline-it. E kaluara e llogarise mbetet e paverifikuar (`historical_account_pl_complete=False`).
- `trade_summary(ledger, trade_id)` jep neto vetem kur OPEN, CLOSE, çmimet/komponentet e kerkuara dhe shpenzimet e atribuuara jane te provuara; ndryshimet nga kalimi i dividendave/financimeve te paatribuara e bllokojne certifikimin.

## Testimi dhe kufizimet

`tests/test_phase_a_category03.py`: 19 teste offline per net P/L me shpenzime, spread jo te dyfishuar, nje ose shume trade, financim ditor, dividend, transferim fondesh, fusha te munguara, korruptim, duplicate, perputhje shumash, crash atomik dhe migrim legacy. Te gjitha testet ekzistuese duhet te kalojne ne PR.

**Kufijte:** Nuk ka burim real-time te transaksioneve ne keto teste. Legacy `TradeRecord.net_pl_home` / `closed_results.pl` aktualisht reflektojne `TradeReduce.realizedPL`, jo neto te attestuar te ketij ledger-i; nuk duhet te perdoren per perfundime perfomance apo risk te bazuar ne net. Kategoria 04 do te ndertoje `LossState` dhe vendimet e riskut mbi te dhëna te verifikuara. Pajtimi i transaksioneve te vjetra dhe komisionet per trade ne multi-fill kane nevoje per evidence shtese; nuk plotesohen me hamendesim.

`ORDER_WRITES_ENABLED=False` dhe `_PRACTICE_WRITES_RELEASED=False` mbeten te pandryshuara. Asnje mutacion OANDA.
