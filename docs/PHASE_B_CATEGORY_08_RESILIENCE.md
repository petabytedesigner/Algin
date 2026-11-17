# Faza B — Kategoria 08: Auditimi operacional dhe qendrueshmeria

**Objekti:** auditim i rivendosshëm nga journal/lifecycle, mbrojtje e lock-ut CSV, kontroll i crash/concurrency dhe coverage ne CI. **Vetëm offline.** Asnje token, llogari Practice/Live, HTTP mutation ose strategji tregtimi nuk perdoret. `main.py` mbetet `OBSERVE_ONLY`; `config.ORDER_WRITES_ENABLED=False` dhe `_PRACTICE_WRITES_RELEASED=False` janë porte të pavarura të pandryshuara.

## 1. Gjetjet e auditimit te fillimit

- `EventLog._append` krijonte lock me `O_EXCL`, por ne fund e hiqte pa kontrolluar nese lock-u ishte zevendesuar. Kjo mund te shkelte izolimin ndermjet shkruesve.
- `AuditProjector._state_outcome` nuk e mbështeste `SUPERSEDED`, edhe pse lifecycle-i i Kategorise 07 mund te shenonte nje BE/partial si te tejkaluar nga mbyllja me SL/TP. Projekti i auditit duhej te ruante dallimin nga `CONFIRMED`.
- `close_intents`, BE/partial `transaction_id` dhe gjendjet e plota `CONFIRMED/UNKNOWN/SUPERSEDED` ishin te ruajtura ne lifecycle, por nuk gjeneronin ende gjurme korrelimi te qarta ne CSV.
- CI i meparshem kalonte gjithë `unittest` por nuk zbatonte minimum matës te coverage per modul.

## 2. Korrigjimet e izoluara

### Siguria e lock-ut CSV

`EventLog` ruan token unik te pronarit dhe identitetin `device/inode` te lock-ut. Pas shkrimit/fsync te token-it, lexon e verifikon lock-un para append dhe para lirimit; nje lock i huaj ose symlink **nuk hiqet**. Deshtimi i inicializimit `fsync` e le orphan-in per inspektim manual. Kodi nuk ben auto-repair per CSV te demtuar.

### Audit replay i ciklit te plote

`AuditProjector` projekton:
- `BE_SUPERSEDED` dhe `PARTIAL_SUPERSEDED` si `INFO`, **jo sukses**;
- `BE_TX_CONFIRMED` dhe `PARTIAL_TX_CONFIRMED` me ID te verifikuara nga `processed_transactions` dhe `close_transaction_ids`;
- `CLOSE_INTENT` nga synimi fillestar, dhe `CLOSE_UNKNOWN`, `CLOSE_CONFIRMED`, `CLOSE_SUPERSEDED` nga ledger-i i provuar. Terminalet kerkojne `closed_results`, `counted_closures` dhe `processed_transactions`.

ID-te e eventeve jane deterministe dhe **nuk riklasifikojne** nje event ekzistues. Fusha `timestamp` per INTENT dhe terminalet me transaksion vjen nga prova e ruajtur; perseritja e projektimit nuk krijon rreshta te rinj. CSV mbetet *projection*, jo evidence qe lejon porosi.

## 3. Provat e automatizuara

`tests/test_phase_b_category08.py` perfshin 19 teste te reja:
- dy shkrues konkurues te CSV-se dhe lifecycle-it, si dhe bllokim mbi lock te huaj;
- `fsync` i pasigurt, append i shkeputur dhe `os.replace` i deshtuar pa avancuar cursor;
- replay determinist i eventeve me renditje te rastësishme; 75 raste konfliktuale te ID-ve; 96 state-e te manipuluara me hash te rillogaritur; 150 batch-e OANDA valid/malformuar;
- cikli sintetik BE → partial → full-close dhe replay pas restart-it, pa event te dyfishte;
- `UNKNOWN`, `SUPERSEDED` nga SL/TP dhe falsifikim i `transaction_id`;
- ruajtja e kontrates `GET` per audit dhe `simulated_writes=0` gjate projektimit.

Randomizimi eshte me seed konstant per riprodhim; nuk eshte Hypothesis i pafund, nuk provon te gjitha permutimet. Crash testet injektojne defekte ne boundaries e ruajtjes; nuk zevendesojne nderprerje reale te energjise apo disk fault-injection OS-level.

## 4. CI e re

`.github/workflows/category8.yml` ekzekuton Python 3.11, `compileall`, bllokimet e shkrimit, **te gjithe** `unittest` me `coverage --branch`, raport te modulit kryesor me prag minimal **70%** dhe `main.py --check`. Paketa `coverage` instalohet vetem ne GitHub CI; `requirements.txt` dhe kontratat runtime nuk ndryshohen.

Ky prag eshte minimum i fazes B, jo certifikim i plote i çdo dege kritike. Raporti shfaq perqindjen reale ne CI; branch/decision coverage me nivele me te larta dhe mutation-testing mund te permiresohen ne kategori pasuese. `coverage` eshte matje strukturore, jo prove e korrektesise ekonomike.

## 5. Kufij operacionalë

- Asnje rrugë e ketij ndryshimi nuk ben `POST/PUT/PATCH/DELETE` ne OANDA.
- `UNKNOWN` dhe mospërputhjet e cursor-it/lock-ut nuk riprovohen verbërisht.
- Mungesa e historikut te NAV/risk, lidhja reale e news/session/market-data, testimi me llogari OANDA Practice dhe release i kontrolluar jane ende jashte kesaj kategorie.
- Ndryshimet e fushes kufizohen ne `storage/event_log.py`, testet, workflow-n e kategorise dhe kete dokument.

**Kriteri i merge:** suita e plote dhe CI 1–6 + 8 PASS ne commit-in final te PR; verifikim i bazes, SHA-ve dhe listës së skedareve; pa hapje te porteve te brokerit. Pas merge kontrollohet edhe CI e `main`.
