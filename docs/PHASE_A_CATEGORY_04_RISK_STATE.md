# Faza A — Kategoria 04: Ekspozimi dhe gjendja e riskut

**Statusi:** implementim *offline / read-only*. Mos e ngaterroni `RiskReadiness.inputs_verified` me `ReconcileReport.entry_allowed` ose autorizim per POST ne OANDA.

## Qellimi dhe kufijte

Kjo kategori bashkon inventarin e provuar te brokerit, journal-in e tentativave, lifecycle-in e ruajtur dhe ledger-in financiar (Kategoria 03) per te ndertuar `ExposureBook` dhe `LossState` te kontrolluara me prova. Ndryshimet jane te izoluara. Asnje parametër risku, strategji, API endpoint apo konfigurim i kredencialeve nuk modifikohet; `ORDER_WRITES_ENABLED=False` dhe `_PRACTICE_WRITES_RELEASED=False` mbeten te pandryshuara.

## 1. Ekspozimi i verifikuar

Moduli `risk/reconciled_account.py` ka funksionin `build_risk_readiness`. Ai kerkon:

- `AccountFunds` me te njejtin `account_id` dhe `lastTransactionID` si `LifecycleStore.load()`, me numra te sakte te inventarit;
- `ReconcileReport` nga pajtimi i perfunduar, me `owned_open_trades` qe perputhen me inventarin;
- journal me tentative unike dhe shkalle `risk_pct` te verifikuara kunder `TradeRecord`;
- çdo pozicion te hapur i lidhur me `attempt_id`, `trade_id`, `clientExtensions.tag=algin`, grade dhe SL/TP te konfirmuara nga reconciler-i;
- rezervim eksplicit `OUTCOME_UNKNOWN` per `IN_FLIGHT`/`UNKNOWN` dhe `PENDING` per `PREPARED`; asnje intent i tille nuk lejon `verified=True` dhe te gjitha porosite pending pa pajtim bllokohen.

Per nje pozicion te hapur ruhet konservativisht `risk_pct` fillestar, pavaresisht BE apo partial-close. Nuk lejohet netting optimist i ekspozimit; kufijte e korrelacionit, grade, instrumenteve dhe marzhit mbeten te `TradePlanner`.

## 2. Lirimi i rezervimit pas mbylljes

`IntentJournal.retire_confirmed(attempt_id, lifecycle_store)` lejon `retired=True` vetem kur:

1. Statusi i tentatives eshte `FILLED`, me `protection_verified=True`.
2. `LifecycleStore` i te njejtes llogari / Practice verifikon `closed=True` dhe `result_counted=True`.
3. `trade_id`, `attempt_id`, `transaction_id`, `processed_transactions` dhe `counted_closures` perputhen.

`AccountReconciler.run()` e kryen pensionimin e tentatives vetem **pasi** state/cursor jane ruajtur atomikisht. Nese deshton shkrimi i journal-it pas commit-it te lifecycle, intent-i mbetet `FILLED` e bllokues dhe nje pajtim i mevonshem mund ta pensionoje idempotentisht. ID e sinjalit origjinal nuk mund te riperdoret; historiku journal nuk fshihet. Mbyllja pa provenience nuk liron rezervimin.

**Kujdes:** `OrderEngine._account_preflight()` ende zbaton ndalesen e meparshme ndaj `FILLED` ne journal. Lidhja e plote e pipeline-it dhe hapja e tregtive te reja do te trajtohen ne Fazën B / Kategoria 05, me verifikim te gjendjes se re. Kategoria 04 nuk e heq kete barriere.

## 3. LossState: vetem me anchor te vertetuar

`RiskHistoryAnchor` eshte nje kontrate e shprehur per proveniencen: `account_id`, cursor aktual, `day` UTC, `day_start_cursor`, `day_start_nav`, `peak_nav`, numri i humbjeve radhazi para anchor-it dhe konfirmimi i plotesise se historikut.

- Nje GET i çastit nuk provon NAV ne fillim te dites, peak NAV historik apo histori te plote te humbjeve. Asnje anchor nuk sajohet automatikisht.
- `LossState.verified=True` vetem nese anchor-i i siguruar eshte per llogarine, cursor-in dhe diten e njejte, ledger-i mbulon periudhen e dites dhe *çdo* transaksion ka shume financiare te plote.
- Shuma konservative e humbjes ditore llogarit debitet negative te transaksioneve monetare pas `day_start_cursor`, pa supozuar se fitimet kompensojne humbjet per qellimin e kufirit.
- Seria e humbjeve merret nga mbylljet e llogaritura nga `trade_summary()` me kosto neto te certifikuar; `closed_results.pl` **nuk** perdoret si neto. Mbylljet para cursor-it anchor nuk numerohen serish.
- Cooldown-et konservative perfshijne si regjistrimet ekzistuese, ashtu edhe çdo mbyllje neto negative te verifikuar. Kur history, kostot, anchor-i ose atribuuimi nuk verifikohet, `LossState.verified=False`.

Anchor-i i riskut duhet te vijë nga nje mekanizem i ardhshëm rigoroz i historiës / monitorimit dhe te ruhet me provenience; ky modul nuk pretendon ta krijoje vetë nje reference historike nga nje snapshot i vetem.

## 4. Porta e lejes se hyrjeve

`RiskReadiness.inputs_verified` eshte rezultat informativ i provave, **jo** leje per broker. `RiskReadiness.entry_allowed=False` dhe `ReconcileReport.entry_allowed=False` mbeten fort te çaktivizuara ne kete kategori. Kategoria 05 duhet te lidhe verifikimin e plotë, risk engine, quote freshness dhe autorizimin e veprimit nen proces lease. Edhe aty transporti real mbetet i mbyllur deri ne verifikim te kategorive te mevonshme.

## Testet

`tests/test_phase_a_category04.py` teston pozicione aktive, rezervime UNKNOWN, porosi pending, journal retirement te konfirmuar dhe idempotent, deshtim atomik/backup, ID dhe cursor te gabuar, historik neto te plote e te paplote, NAV/dite/cursor te gabuar dhe kufizimin e `entry_allowed`.

Te gjithe testet ne GitHub Actions mbeten offline; asnje POST/PUT/PATCH/DELETE broker nuk kryhet.
