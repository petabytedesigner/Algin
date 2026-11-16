# Faza B — Kategoria 07: Cikli i menaxhimit te tregtise (vetem offline)

**Statusi:** Kontratat BE, partial-close, full close dhe pajtimi pas crash jane implementuar dhe testohen me nje broker sintetik. **Asnje mutacion real ne OANDA Practice ose Live**. `main.py` mbetet `OBSERVE_ONLY` dhe portat e brokerit vazhdojne te çaktivizuara.

## Referenca zyrtare OANDA REST v20

- [Trade Endpoints](https://developer.oanda.com/rest-live-v20/trade-ep/): `PUT /v3/accounts/{accountID}/trades/{tradeSpecifier}/orders` ndryshon SL/TP permes `{"stopLoss":{"price":"..."}}`; `PUT /v3/accounts/{accountID}/trades/{tradeSpecifier}/close` merr `{"units":"ALL"}` per mbyllje te plote, ose një varg numerik **pozitiv** per mbyllje te pjesshme.
- [Transaction Definitions](https://developer.oanda.com/rest-live-v20/transaction-df/): `STOP_LOSS_ORDER`, `MARKET_ORDER` dhe `ORDER_FILL` permbajne proveniencen e ndryshimit. `ORDER_FILL.reason` dallon `MARKET_ORDER_TRADE_CLOSE`, `STOP_LOSS_ORDER`, `TAKE_PROFIT_ORDER`, etj.
- HTTP 200 ose nje ndryshim ne snapshot-in e pozicioneve **nuk provon** përfundimin e një operacioni. Reconciler-i GET-only duhet të procesojë transaksionet e lidhura me cursor-in e brokerit.

## 1. BE dhe partial-close

`TradeManagement.stage_break_even()` dhe `stage_partial()` ruajne nismen ne `LifecycleStore` para ndonje komande. `execution/offline_management_engine.py` prodhon nga state komandat e sakta v20:

- **BE:** `PUT /trades/{tradeID}/orders`, trup `{"stopLoss":{"price":entry_price}}`.
- **Partial:** `PUT /trades/{tradeID}/close`, trup `{"units":"<pozitive>"}`; amount i nje trade SHORT mbetet pozitiv dhe nuk lejohet të kalojë units e mbetura.

Reconciler-i pranon konfirmimin e BE vetem pas `STOP_LOSS_ORDER` me `tradeID`, price dhe reason te lejuar, e kontrollon me SL ne snapshot dhe ruan `be_transaction_id`. Partial kerkon `tradeReduced`, units ekzakte te rezervimit, `reason=MARKET_ORDER_TRADE_CLOSE` kur jepet dhe korrespondencen e `currentUnits` ne broker. `partial_phases[phase].transaction_id` dhe `close_transaction_ids` regjistrohen atomikisht me cursor-in. Skemat legacy pa keto fusha vazhdojne te lexohen.

## 2. Full close: intent dhe konfirmim

`TradeManagement.stage_close(trade_id, source="SYSTEM"|"MANUAL")` regjistron **vetem synimin** ne `close_intents` te checksummed lifecycle JSON, per llogarine dhe cursor-in aktual, me `units` te sakta dhe kohe UTC. Nuk dërgon `PUT`. Dy thirrje me te njejtin trade bllokohen nga rezervimi i pandryshueshëm.

`AccountReconciler`:
- kerkon `ORDER_FILL.tradesClosed` per mbyllje te plote dhe perputhje të units/cursor-it te rezervuar; `MARKET_ORDER_TRADE_CLOSE` lejon `close_intents.status=CONFIRMED` me `confirmed_transaction_id`;
- nese brokeri e ka mbyllur nderkohe tregtine me SL/TP/closeout, vendos `SUPERSEDED` dhe ruan ID-ne e mbylljes — **nuk pretendon qe e ka shkaktuar veprimi yne**;
- nese be/partial jane ne `INTENT` ose `UNKNOWN` dhe tregtia mbyllet sipas provave te plota, i kthen ne `SUPERSEDED`, jo `CONFIRMED`;
- regjistron `closed_results`, `counted_closures`, ledger-in financiar dhe cursor-in ne nje update atomik; pastaj `IntentJournal.retire_confirmed` heq rezervimin e mbetur pa fshire sinjalin origjinal.

`source=SYSTEM`/ `MANUAL` eshte etiketë e synimit. Me endpoint-in v20 te mbylljes pa correlation ID te dedikuar, nje `MARKET_ORDER_TRADE_CLOSE` i njejtë mund te vije edhe nga nje aktor tjeter; prandaj konfirmimi i `close_intent` provon **mbylljen e tregtise**, jo autorin e sakte te kerkeses. Atributimi i autorit/RequestID eshte nje kufizim i hapur para release.

## 3. Timeout, UNKNOWN, restart, idempotence

`OfflineManagementEngine` **nuk pranon** `PracticeTransport`, subclass-e te transportit sintetik ose transport arbitrar. Tipi i lejuar eshte vetëm `SyntheticLifecycleBroker`, qe nuk ka HTTP. Konfigurimi real i write-eve duhet te jete ende i çaktivizuar edhe per testin. Kontrollet e transportit ndodhin **para** regjistrimit te intent-it.

- `INTENT` ruhet para operacionit sintetik. Pas nje timeout edhe pas efektit, `mark_unknown` ruan `UNKNOWN` dhe thirret vetem GET-only reconciliation. Nje konfirmim nga transaksionet mund ta zgjidhe pa POST/PUT te dyte.
- Nese brokeri nuk jep prova te plota, rezultati mbetet `UNKNOWN`, pa blind retry. Nje restart mund te replays `sinceid` nga cursor-i i ruajtur; nje close/partial/BE i konfirmuar nuk dërgohet serish.
- Në mungesë të transaksionit, zhdukja nga inventari nuk quhet close. Një ID e pavlefshme, reason i gabuar, units te papërputhura ose transaksion i dyfishuar bllokon cursor-in.

## 4. Provenienca dhe kufizimet

`ManagementResult.confirmed_transaction_id` jep ID e `STOP_LOSS_ORDER`, `ORDER_FILL.tradeReduced` ose `ORDER_FILL.tradesClosed` vetem kur jane perpunuar atomikisht nga reconciler-i. `broker_write_attempted=False` ne cdo rezultat te ketij moduli (shkrimet jane vetëm virtuale); `simulated_write_count` është numri i veprimeve te fake brokerit.

Kjo kategori **nuk** e lidh ende `main.py` me ekzekutimin real, nuk i aktivizon BE/partial/close në OANDA dhe nuk krijon token-e, URL ose metrika performance. Me tej kerkohen testet e stabilitetit/fuzz (Kategoria 08), lidhja e sinjaleve me burimet reale te tregut dhe provat e kontrolluara OANDA Practice (Kategoria 12).

## Testet dhe kriteret e pranimit

`tests/test_phase_b_category07.py` teston te gjithe ciklin e plote, BE dhe partial per SHORT, transaksione individuale, rikthim pas timeout/lost-ack dhe restart, SL/TP qe tejkalon nje nisme te planifikuar, mungesë mbylljeje, rreshtim cursor, kryerje te dyfishta dhe bllokim te transportit real.

Pranohet për merge vetëm kur suita e plotë offline, workflow-t 1–6 dhe rishikimi i diff-it perfundojne me sukses. **Suksesi offline nuk eshte garanci e aksesit, sjelljes ose përfitimit real me OANDA.**
