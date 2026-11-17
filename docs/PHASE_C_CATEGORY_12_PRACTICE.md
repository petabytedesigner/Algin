# Faza C — Kategoria 12: Verifikimi OANDA Practice

**Gjendja e saktë:** mekanizmi dhe CI e provës **reale read-only** janë implementuar; rezultatet e autentikuara nuk ekzistojnë deri në një përgjigje të vërtetë të OANDA-s. Testet e GitHub PR janë sintetike. **Testet me shkrim real në Practice nuk janë kryer** dhe janë të bllokuara nga dy porta të pavarura. Kategoria 12 mund të shënohet `LIVE_VERIFIED` vetëm pasi të dokumentohet prova reale, jo kur kalojnë testet offline.

## Llogaria dhe konteksti i sigurisë

- Mjedisi i lejuar: vetëm `https://api-fxpractice.oanda.com/v3`.
- Account ID i pritshëm nga përdoruesi: `101-001-37674492-003` (Llogaria2, **DEMO**, USD).
- Referencë fotografike e datës 15 nëntor 2026: Balance/NAV `$10,000`, margin available `$10,000`, margin used `$0`, leverage i shfaqur `50:1`. **Të dhëna historike të figurës**, jo prova API në kohë reale.
- `config.ORDER_WRITES_ENABLED = False` dhe `broker.practice_transport._PRACTICE_WRITES_RELEASED = False` mbeten të çaktivizuara; kjo kategori nuk i ndryshon.
- Kontrolli i klasës `PracticeReadOnlyProbe` nuk përmban `POST/PUT/PATCH/DELETE`, as auto-retry mutacionesh.
- Llogaria e testit mund të ndryshojë nga fotografia; nuk duhet të hamendësohet aktualiteti i NAV/balance ose të lejohet tregtim nga një screenshot.

## Problem sigurie që kërkon veprim nga zotëruesi

Repository `petabytedesigner/Algin` është **public**. Përdoruesi ka deklaruar se token-i OANDA është vendosur në skedarin e versionuar `.env.example`. Ky PR **nuk e lexon, nuk e kopjon dhe nuk e ndryshon atë skedar**. Nëse token-i ekziston në repo ose historikun e tij publik, duhet trajtuar si i ekspozuar. Rekomandohet **revokim/rotacion i token-it në OANDA dhe vendosje e token-it të ri në GitHub Actions Secrets**, pa kopjuar vlerën në chat, logs ose commits. Ndryshimi i kredencialeve do të bëhet vetëm nga pronari i tyre.

Për autentikim në GitHub Actions kërkohen:

- `OANDA_PRACTICE_API_TOKEN` (Secret, jo skedar);
- `OANDA_PRACTICE_ACCOUNT_ID` (Secret, me vlerën e llogarisë Practice të synuar).

Workflow-i i leximit i mapon këto vetëm në `OANDA_API_TOKEN` dhe `OANDA_ACCOUNT_ID` për procesin e izoluar. ID-ja e pritshme e llogarisë kontrollohet kundër `101-001-37674492-003` **përpara HTTP**. Token-i nuk printohet dhe nuk lexohet nga `.env.example`.

Dokumentim për vendosjen: [GitHub Actions Secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets).

## Prova reale e fazës GET-only

`broker/practice_readonly_probe.py` lexon në këtë rend:

1. `GET /v3/accounts/{id}`: ID e saktë, `currency=USD`, `NAV`, `balance`, `marginAvailable`, `openTradeCount`, `pendingOrderCount`, lista e tregtive dhe urdhrave, `lastTransactionID`. Validon numrat dhe përputhjen e numërimeve.
2. `GET /v3/accounts/{id}/summary`: konfirmim i pavarur i ID-së së llogarisë dhe monedhës.
3. `GET /v3/accounts/{id}/instruments`: validimi i katalogut të instrumenteve të autorizuara dhe rregullave FX ekzistuese.
4. `GET /v3/accounts/{id}/pricing?instruments=EUR_USD`: nëse çifti është i disponueshëm, kontrolli real i `tradeable`, freskisë, precision dhe spread-it me parserin e prodhimit. Në treg të mbyllur kuota mund të jetë `QUOTE_UNAVAILABLE_OR_UNVERIFIED`; **nuk bëhet leje tregtimi**.
5. `GET /v3/accounts/{id}/transactions/sinceid?id=<cursor>`: provë GET-only e cursor-it; kur ka transaksione të reja, verifikon ID/account/type/renditjen dhe raporton `CONCURRENT_CHANGES_NO_WRITE`. Nuk zhvendos asnjë cursor lokal në mënyrë të paverifikuar.

Log-et e CI publikojnë **vetëm** një raport të sanitizuar: llojin e mjedisit, burimin, tri shifrat e fundit të ID-së, monedhën, statusin e verifikimit të inventarit, numrin e instrumenteve, statusin e kuotës/cursor-it dhe nëse balanca përputhet me referencën `10,000`. Nuk printojnë token, ID të plotë, vlera të sakta NAV/balance, trade/order IDs, përgjigje JSON të papërpunuara apo RequestID.

`PracticeProbeReport.broker_writes_enabled` është gjithmonë `False`, **pavarësisht nëse GET ka sukses**. Kjo nuk është certifikim se llogaria është gati për porosi: para një urdhri duhen verifikime të tjera mbi risk/state/protection.

### Ekzekutimi në GitHub Actions

Workflow: `.github/workflows/category12.yml`.

- Në `pull_request` ekzekutohen vetëm testet offline, **pa secrets**.
- Në `push` të `main` ose `workflow_dispatch` ekzekutohet një GET publik/pa token drejt hostname-it Practice për të provuar rrugën TLS/HTTP. Një përgjigje `401/403` provon vetëm kontaktin, jo autentikimin.
- Po të ekzistojnë të dy GitHub Secrets, ekzekutohet script-i i autentikuar vetëm për GET. Po të mungojnë, shfaqet `AUTHENTICATED_PRACTICE_PROBE_NOT_RUN`; suksesi i job-it **nuk duhet interpretuar si konfirmim brokeri**.
- Kodi i autentikuar nuk merret nga PR i jashtëm dhe nuk aktivizohet në pull-request jobs.

Në Termux ose një runner të besuar, e njëjta provë mund të thirret me `python -m broker.practice_readonly_probe`, duke vendosur `OANDA_API_TOKEN`, `OANDA_ACCOUNT_ID` dhe `OANDA_PRACTICE_EXPECTED_ACCOUNT_ID` në **mjedis të sigurt**, jo në commit ose terminal të regjistruar.

## Porta për urdhrin real Practice: NUK ËSHTË HAPUR

Hapat e mëposhtëm mbeten **detyrë e hapur** dhe nuk duhet të ekzekutohen vetëm sepse testet sintetike kalojnë:

1. Konfirmim i autentikuar Practice me targetin e saktë dhe pa lidhje drejt Live; përgjigje HTTP/JSON, ID, valutë, inventar dhe mjetet e tregtimit të verifikuara.
2. Rivendosje dhe pajtim i sigurt i journal/lifecycle **në të njëjtin account**, pa porosi ose tregti të huaja në inventar, me risqe, margin dhe rezervime të verifikuara. `NEWS_UNKNOWN` mbetet bllokues për sinjalet e strategjisë.
3. Audit i kodit që do të bëjë një urdhër praktik të vetëm (me `clientExtensions`, `priceBound`, FOK dhe SL/TP në urdhër), kontroll për njësitë minimale, çmimet aktuale dhe riskun absolut më të ulët të mundshëm. Release-i kërkon protokoll të veçantë, jo aktivizim të heshtur të `main.py`.
4. Një urdhër i vetëm Practice, i regjistruar **para kërkesës** në journal; përgjigje `UNKNOWN`/timeout/HTTP 5xx nuk e aktivizojnë **kurrë** një retry të verbër.
5. `GET` mbi order, trade, SL/TP, transaction IDs dhe reconciliation që provon plotësimin dhe mbrojtjen. Nëse mbrojtja nuk verifikohet, **ndalo testin** dhe kërko ndërhyrje të kontrolluar; mos e quaj ekzekutim të suksesshëm.
6. Testimi i BE, partial-close dhe full-close kërkon shkrime `PUT` në Practice. Adapteri aktual i prodhimit i bllokon me qëllim. Për një release të kontrolluar nevojitet rrugë e veçantë e audituar; gjendja `UNKNOWN` duhet të ruhet në state përpara çdo rikuperimi read-only.
7. Krahasimi i çmimeve reale, slippage, komisionit, financing, realizimit neto dhe journal-it; raporti final i prova-ve reale.

**Nuk ka asnjë trade të hapur/mbyllur në OANDA nga ky PR.** Nuk mund të certifikohet faza e ekzekutimit të urdhrave pa një kërkesë dhe transaksion real të verifikuar në Practice.

## Referenca

- [OANDA Account Endpoints](https://developer.oanda.com/rest-live-v20/account-ep/)
- [OANDA Pricing Endpoints](https://developer.oanda.com/rest-live-v20/pricing-ep/)
- [OANDA Transaction Endpoints](https://developer.oanda.com/rest-live-v20/transaction-ep/)
- [OANDA Trade Endpoints](https://developer.oanda.com/rest-live-v20/trade-ep/)

## Testet dhe kriteri i integrimit

`tests/test_phase_c_category12.py` provon me **19 teste offline**: ID e saktë e llogarisë, USD, endpoint vetëm Practice, portat e shkrimit të çaktivizuara, NAV/balance/inventar i pavlefshëm, katalog i korruptuar, kuotë e munguar, cursor i paqartë/konkurrent, maskim të gabimeve dhe zero dërgesa mutacionesh.

Kriteret e PR: të gjithë workflow-t testues, ndryshime vetëm në script, workflow, testet dhe dokumentim; verifikim i tree SHA dhe prova pas merge. Rezultati i autentikuar OANDA raportohet **veçmas** nga numri i testeve offline.
