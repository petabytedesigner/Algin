# Faza B — Kategoria 06: Pajtueshmeria e OANDA REST v20

**Gjendja e dorëzimit:** kod + teste *offline*, pa mutacione te llogarise. `config.ORDER_WRITES_ENABLED=False` dhe `broker.practice_transport._PRACTICE_WRITES_RELEASED=False` mbeten barriera te pavarura. Kjo kategori nuk autorizon hapje, mbyllje ose modifikime tregtish.

## Referenca zyrtare (verifikuar me 2026-11-17)

- [Account Endpoints](https://developer.oanda.com/rest-live-v20/account-ep/): `GET /v3/accounts/{accountID}` kthen inventarin e plotë te tregtive/porosive dhe `lastTransactionID`; `/summary` nuk zevendeson inventarin.
- [Transaction Endpoints](https://developer.oanda.com/rest-live-v20/transaction-ep/): `GET /transactions/sinceid?id=...` kthen transaksione **pas** ID-se se dhene dhe `lastTransactionID`.
- [Pricing Definitions](https://developer.oanda.com/rest-live-v20/pricing-df/): `ClientPrice.tradeable` eshte boolean aktual; `Price.status` eshte deprecated.
- [Transaction Definitions](https://developer.oanda.com/rest-live-v20/transaction-df/): `OrderFillTransaction.price` eshte deprecated. Per nje `TradeOpen`, `tradeOpened.price` eshte çmimi i tregtise; `fullVWAP` nuk eshte gjithmone i barabarte me çmimin individual te trade-it.
- [Order Endpoints](https://developer.oanda.com/rest-live-v20/order-ep/): per `POST /orders`, HTTP 200/201/400/404 nuk mjafton vetem; kërkohet transaksion i lidhur dhe identitet i sakte per klasifikim.

## Korrigjimet e kesaj kategorie

### 1. Lexuesit e çmimeve

`broker.oanda_v20_contract.price_tradeable` perdoret ne `broker/execution_validation.py`, `broker/risk_data.py`, `data/data_handler.py` dhe `utils/common.py`:
- `tradeable=True` eshte autoritet kur fusha ekziston; `False` ose vlera jo-boolean bllokon.
- `status` lexohet si kompatibilitet legacy **vetem nese mungon `tradeable`**. Nje status deprecated kundershtues nuk ia ndryshon kuptimin vleres aktuale boolean.
- Filtrat e freskise, bid/ask, perputhjes se instrumentit dhe hom-conversion mbeten aktive dhe te pandryshuara.

### 2. Identiteti dhe çmimi i `ORDER_FILL`

`broker/execution_result.py` perdor `tradeOpened.price` (ose `tradeReduced.price`/`tradesClosed[0].price` per reduktime/mbyllje) kur jepet. `OrderFillTransaction.price` eshte vetem fallback legacy. `fullVWAP` **nuk perdoret** per te krijuar çmim individual tregtie kur mungon prova e sakte; rezultati eshte `UNKNOWN`. Mbeten detyrime klasifikimi per `orderID`, `tradeID`, units dhe lidhjen me `orderCreateTransaction`. `OrderEngine` ben GET per SL/TP/ownership pas fill-it dhe, kur jepet `Trade.price`, e krahason me çmimin per-trade te transaksionit para certifikimit te mbrojtjes. Mospërputhja e çmimit e lë `protection_verified=False`, pa reparim automatik.

### 3. `sinceid` dhe cursor

`broker.oanda_v20_contract.validate_sinceid_batch` vendos para commit-it te cursor-it:
- accountID i çdo transaksioni duhet te perputhet; ID-ja eshte numerike-string, e renditur strikt dhe brenda kufirit te cursor-it;
- nuk pranohen dublikime, transaksione jo-dict, mungesë type, batch bosh kur cursor ka avancuar, ose `lastTransactionID` kontradiktor;
- **nuk supozohet** qe ID-te numerike jane njera pas tjetres; nje hendek ne numerim nuk eshte vetvetiu mungese historiku.
- Nese transaksionet dhe snapshot-i i brokerit nuk pajtohen, `AccountReconciler` ngre `ReconcileError` dhe nuk ndryshon gjendjen e qendrueshme.

### 4. HTTP dhe siguria

`PracticeTransport` ruan `GET` read-only me timeout dhe `allow_redirects=False`, validon formën e account ID dhe paths para HTTP. `utils.common.safe_request` nuk ndjek automatikisht redirects as per lexime; nje 3xx nuk interpretohet si pergjigje 200 e verifikuar. Gabimet e transportit nuk ekspozojne token ose URL private ne exception message.

Shkrimet e brokerit mbeten te bllokuara edhe nese nje thirrje e nivelit te ulet provon `PUT/POST`. Asnje retry per mutacione dhe asnje inferim i sigurt nga 404/timeout: `UNKNOWN` mbetet status i paqartë dhe rezervimi journal bllokon nje urdher tjeter.

## Provat dhe kufizimet

`tests/test_phase_b_category06.py` perfshin 20 teste te reja me fake broker, stub HTTP, kontrata te deprekuara/aktuale, mospërputhje te transaksioneve, transport te mbyllur, timeout, dhe test pozitiv te `OrderEngine` me `tradeOpened.price`. Testet ekzistuese te fazave A dhe B duhet te mbeten jeshile. Numri final do te merret nga GitHub CI, jo nga supozimi.

**Kufizimet:** teste offline nuk vertetojne autentikimin, specifikat e llogarise, aksesin apo rezultatin e urdhrave te vertete ne OANDA Practice. Pajtimi i plotë BE/partial/close, monitorimi i ndryshimeve te shpejta ne broker dhe provat e NAV historik trajtohen nga kategorite pasuese. Nuk u lexuan dhe nuk u kopjuan kredenciale. Kjo pakete nuk shton/aktivizon asnje mekanizem tregtimi.
