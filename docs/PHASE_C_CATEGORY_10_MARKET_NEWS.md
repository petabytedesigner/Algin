# Faza C — Kategoria 10: Instrumente, kuota dhe kalendar ekonomik

**Lloji:** rregulla e adapterë verifikimi, me prova të automatizuara **offline**. Pa akses te kredencialet reale, pa thirrje te llogarise OANDA në këtë PR dhe **pa asnjë POST/PUT/PATCH/DELETE**. `main.py` vazhdon `OBSERVE_ONLY`; `ORDER_WRITES_ENABLED=False` dhe `_PRACTICE_WRITES_RELEASED=False` nuk ndryshojnë. Parametrat e riskut/strategjisë dhe emërtimet e API-së mbeten të pandryshuara.

## 1. Kontrata zyrtare e OANDA REST v20

- [Account instruments](https://developer.oanda.com/rest-live-v20/account-ep/): `GET /v3/accounts/{accountID}/instruments` kthen listën e instrumenteve të disponueshme për atë llogari. Disponueshmëria varet nga llogaria/divizioni rregullator, jo nga lista statike e botit.
- [Instrument definitions](https://developer.oanda.com/rest-live-v20/instrument-df/): `name`, `type`, `pipLocation`, `displayPrecision`, `tradeUnitsPrecision`, kufijtë e njësive dhe `marginRate` janë të dhëna të brokerit; nuk duhen hamendësuar.
- [Pricing](https://developer.oanda.com/rest-live-v20/pricing-ep/) dhe [Pricing definitions](https://developer.oanda.com/rest-live-v20/pricing-df/): `GET /v3/accounts/{accountID}/pricing` jep çmime specifike të llogarisë. `tradeable` është autoriteti aktual; `status` është i deprekuar. Ora, bid/ask, precision dhe spread duhet të verifikohen.
- [Candles](https://developer.oanda.com/rest-live-v20/instrument-ep/): vetëm M15 të mbyllur, sipas `data.candle_validation`. Nuk ndryshon protokolli ekzistues.

## 2. Instrumentet e autorizuara (P53)

`broker/account_instruments.py` validon përgjigjen e plotë të katalogut me `lastTransactionID` numerike, identitete unike të instrumenteve dhe metadatat e detyrueshme për FX. Ndërton `InstrumentRules` për `CURRENCY`, duke marrë pip, precision, kufijtë dhe margin nga përgjigjja e llogarisë. Një përgjigje me instrument FX të keqformuar ose dyfishuar **nuk** kthehet në katalog të vlefshëm.

`DataHandler.get_account_instruments()` e kërkon katalogun rishtas në secilin scan. `None` = verifikimi dështoi; `{}` = katalog i verifikuar pa çifte FX të përzgjedhura. `main._scan_once` ndalon ciklin në mungesë të katalogut dhe kapërcen simbolet që nuk janë në të. Metalet/CFD/crypto nuk aktivizohen nga kjo kategori, edhe nëse emrat ndodhen në `main.INSTRUMENTS`.

**Kufiri:** një GET katalogu provon disponueshmërinë në atë moment të kërkesës, por jo një snapshot atomik me `NAV`, `/pricing` dhe `/transactions`. Kjo rrugë është vetëm vëzhgim, jo autorizim ekzekutimi.

## 3. Kuotat dhe spread-i (P25)

`main` i kalon `InstrumentRules` të verifikuara te `DataHandler.get_price(instrument, rules=...)`. Kjo degë strikte përdor **të njëjtin** `broker.execution_validation.parse_market_quote` që përdor `OrderEngine`, duke kontrolluar:
- një kuotë unike për instrumentin, `tradeable`, data UTC jo më e vjetër se `config.MAX_QUOTE_AGE_SECONDS` dhe jo më shumë se 2 sekonda në të ardhmen;
- bid/ask pozitive, të renditura dhe të përputhshme me `displayPrecision` të brokerit;
- spread i matur në **pip-e të instrumentit**, jo me një pip-size të hamendësuar, brenda `config.MAX_SPREAD_PIPS`.

Nuk ndryshohen kufijtë ekzistues 20 sekonda dhe 2.5 pip. Kërkesa pa `rules` ruhet për kompatibilitet **vetëm si lexim legacy/display**; nuk vërteton instrumentin dhe spread-in dhe nuk përdoret nga cikli kryesor. Observimi i spread-it në test është sintetik; shpërndarja reale sipas instrumenteve/sesioneve duhet të matet në një fazë të mëvonshme.

## 4. Lajmet: nga `NEWS_UNKNOWN` te evidenca e plotë (P55/P56)

`NewsFilter` e konsideron `None` (kalendari i pastër) vetëm kur:
- përgjigjja ka `fetched_at` të freskët UTC, `events`, `coverage_start`, `coverage_end` dhe `currencies` të verifikueshme;
- mbulimi përfshin **të dy monedhat** e çiftit, si dhe të gjithë intervalin e riskut: nga `now - NEWS_LOCKOUT_MINUTES_AFTER` deri te `now + NEWS_LOCKOUT_MINUTES_BEFORE`;
- eventet kanë kohë UTC, monedhë brenda fushës së deklaruar, titull dhe impakt të saktë;
- burimi i largët është `HTTPS` me hostname të miratuar shprehimisht nga `NEWS_PROVIDER_HOST` (i njëjtë me hostname e `NEWS_PROVIDER_URL`). Ridrejtimet nuk ndiqen.

Kalendar bosh, por pa mbulim kohor/valutor, është `NEWS_UNKNOWN`, jo `NEWS_CLEAR`. Eventi `HIGH` mbetet bllokues gjatë dritares prej 30 minutash para/pas. Për testim, konfigurimi i `STATIC_NEWS_*` duhet të deklarojë po ashtu mbulimin; ndryshimi i listës statike edhe *in-place* e zhvlerëson cache-in.

**Burim i vërtetë ekonomik nuk është zgjedhur, kontraktuar ose testuar.** `NEWS_PROVIDER_URL`/`NEWS_PROVIDER_HOST` nuk janë të konfiguruar në këtë zhvillim, ndaj sinjalet reale mbeten `NEWS_UNKNOWN`. Verifikimi i autenticitetit, licencës, cilësisë, plotësisë dhe versionit të skemës së një ofruesi konkret mbetet detyrë operative e hapur. Deklarimi i mbulimit nga një burim nuk përbën, vetvetiu, provë të pavarur të plotësisë së tij.

## 5. Testet dhe kriteri i integrimit

`tests/test_phase_c_category10.py` përmban 28 teste offline: katalogu i llogarisë, metadata FX, instrumente të dyfishta, cursor i pavlefshëm, spreade mbi kufi, precision, çmime stale/future/locked, calendar bosh me dhe pa mbulim, dy monedhat, kufijtë e lockout-it, ndryshimi i cache-it, burim HTTPS i pamiratuar dhe izolimi i scan-it pa katalog.

`tests/test_category5.py` dhe `tests/test_category2.py` përditësojnë **vetëm fixtures sintetike** të skanimit për të ruajtur kontratën e re të katalogut dhe të mbulimit të lajmeve. Të gjithë regresionet ekzistuese duhet të kalojnë dhe dokumentimi zyrtar duhet të mbetet autoritar.

**Merge gate:** të gjithë workflow-t në commit-in final të PR, listë skedarësh brenda fushës, bazë SHA e pandryshuar dhe CI pas merge. Testet offline nuk nënkuptojnë verifikim real të llogarisë apo release për porosi.
