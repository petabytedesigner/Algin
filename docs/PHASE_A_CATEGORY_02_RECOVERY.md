# Faza A — Kategoria 02: Journal, State dhe Rikuperimi

**Fusha:** Integriteti i intent-journal, lifecycle state + transaction cursor, gjendjet UNKNOWN, rikuperimi nga crash/lock dhe backup/restore. Pa strategji te re, pa ndërrim parametrash risku dhe pa mutacione te OANDA. Testet jane offline.

## Kontratat e forcuara

1. **Journal:** Perpara se te perdoret nje tentative e ruajtur, JSON i kerkeses serializohet ne menyre deterministe dhe rillogaritet SHA-256; krahasohet me `request_hash`. Kontrollohen formati i ID-se se sinjalit, unikësia e sinjaleve, kohet UTC, marker-i boolean i mbrojtjes dhe payload-i JSON finitar. Hash-i zbulon ndryshime aksidentale ose paqartesi ne ruajtje; **nuk eshte autentikim i bazuar ne çeles sekret**.
2. **Symlink/lock:** Kontrolli `is_symlink()` paraprin `exists()`, qe lidhjet e prishura te mos interpretohen si gjendje bosh. Locks janë ekskluzive; nje lock i mbetur pas crash-it ose i zevendesuar gjate nje operacioni nuk hiqet automatikisht.
3. **Lifecycle/cursor:** `processed_transactions` dhe `counted_closures` kane rend rrites, pa dublikime, nuk kalojne `cursor`; mbylljet e numeruara duhet te kene transaksion te perpunuar. `LifecycleStore.mutate` ruan gjendjen e plote dhe cursor-in ne nje skedar te vetem me checksum, fsync dhe replace atomik.
4. **Reconciler:** Nese snapshot-i aktual i brokerit permban pending/unowned trade, intent UNKNOWN ose operacione menaxhimi te pambyllura, ndryshimet e propozuara te cursor-it nuk publikohen si pajtim i perfunduar. Cursor-i i ruajtur mbetet ai i fundit i verifikuar dhe batch-i provohet serish vetem me lexim. Asnje mbyllje e papersistuar nuk raportohet e re. Vetem pas zgjidhjes se bllokimit dhe pajtimit te plote commit-ohet gjendja.

## Politika e UNKNOWN

- `PREPARED`, `IN_FLIGHT` dhe `UNKNOWN` nuk lejojne ridërgim te verbër.
- `OrderEngine.reconcile(attempt_id)` perdor GET me identitetet dhe transaksionet e OANDA; mungesa e porosise nga nje GET ose timeout nuk verteton qe ajo nuk u krye.
- `FILLED` pa SL/TP te provuar nuk lejon hapje te reja. Nuk krijohet "success" nga vete CSV ose nga mungesa e pozicionit.
- Zgjidhja autoritative e gjendjeve UNKNOWN dhe procedura e plote BE/partial me ID te brokerit mbeten varësi te kategorive 05–07.

## Backup dhe restore: vetëm procedure e kontrolluar

Backup-i i lifecycle nuk është autorizim per te rinisur tregtimin. Per nje restore operacional:
1. Ndaloni plotësisht instancat e procesit. **Mos hiqni lock-un e mbetur pa inspektim te pronarit dhe procesit.**
2. Ruani si grup journal-in, lifecycle state, cursor-in, eventet dhe nje manifest me llogarine, environment dhe gjurmen e kopjes. Mos perfshini kredenciale ne manifest.
3. Verifikoni paprekshmerine dhe perputhjen e llogarise/mjedisit para vendosjes se kopjes. Mos mbishkruani nje state aktiv. Nje backup i vjeter nuk eshte snapshot i vertetuar i brokerit te tanishem.
4. Kryeni GET te plote te llogarise dhe `sinceTransactionID` nga cursor-i i kopjes; nese historia ose identitetet nuk verifikohen, sistemi mbetet BLOCKED. Ndaloni çdo ridërgim derisa UNKNOWN te zgjidhet me prova.

Testet e kesaj kategorie simulojne kopje dhe kthim te file-it me fixture sintetike; nuk testojne ruajtjen e nje llogarie operacionale ne Termux ose nje restore ne OANDA Practice.

## Provat

`python -m compileall -q .`, `python -m unittest discover -s tests -v` dhe `python main.py --check` verifikohen nga GitHub Actions per PR-ne. Testet `tests/test_phase_a_category02.py` ushtrojne tampering te hash-it, symlink te prishur, lock te vjeter/te zevendesuar, cursor te parregullt, restart me UNKNOWN, atomic replace failure dhe restore offline.

## Kufizimet qe mbeten

- Ledger-i financiar neto dhe transaksionet financiare: **Kategoria 03**.
- `ExposureBook`/`LossState` dhe `entry_allowed`: **Kategoria 04**.
- Ekzekutimi real i brokerit, BE/partial dhe operimi ne Practice: **Kategorite 05–07, 12**.
- Gjendja e OANDA dhe urdhrat e saj nuk modifikohen nga kjo kategori. `ORDER_WRITES_ENABLED=False` dhe `_PRACTICE_WRITES_RELEASED=False` mbeten te pandryshuara.

**Kriteri:** Testet e plota kalojne pa regresione; snapshot-i dhe cursor-i nuk avancohen nga prova kontradiktore; gjendjet e paqarta mbeten te bllokuara dhe nuk lindin mutacione brokeri.
