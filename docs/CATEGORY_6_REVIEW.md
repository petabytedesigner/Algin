# Kategoria 6 — Auditimi, diagnostikimi, regresioni dhe raportimi

> **Statusi pas rishikimit (2026-11-16):** Baza teknike e auditimit
> idempotent, sondimit heartbeat, regresionit fake-broker dhe raportimit
> offline është plotësuar. Lidhja operative me ledger-in e plotë të OANDA
> dhe verifikimi real Practice mbeten faza të mëvonshme. **OBSERVE_ONLY**.

## 1. Fusha dhe ndarja 6A–6D

Pikat: **P57 P58 P59 P60 P61 P64 P65 P66 P67 P70 P71 P74**.

- **6A:** CSV, flag-et, kode specifike refuzimi, korrelacion, replay pas
  restart-it dhe ndarja e UNKNOWN nga sukseset e konfirmuara.
- **6B:** heartbeat durable + read-only stale/checksum verification.
- **6C:** cikël end-to-end me fake broker: fill → SL/TP verified → BE →
  partial → close, rinisje dhe timeout pa ridërgim.
- **6D:** stop-first OHLC, provenienca eksplicite e kostove dhe
  metrika deterministe të rezultateve.

## 2. CSV EventLog dhe AuditProjector

Skema CSV mbetet e pandryshuar:

`timestamp,event_id,correlation_id,instrument,direction,stage,event,outcome,reason_code,attempt_id,order_id,transaction_id,trade_id,detail`

Regjistri i auditimit kontrollon skemën, çdo rresht ekzistues, ID-të
unike, newline-in përfundimtar, lock-un dhe symlink; përdor O_EXCL,
append + fsync. Një tail i dëmtuar **nuk riparohet automatikisht**.
`emit_once` dhe `emit_many_once` rindërtojnë të njëjtat evente me ID
deterministe dhe pa dublikime; konfliktet bllokohen. CSV nuk është
autoritet për hapje, mbyllje ose ridërgim porosie.

`AuditProjector(account_id, EventLog(...)).project(attempts, state)`
projekton prova nga `IntentJournal` dhe `LifecycleStore`. Dallohen:

- `INTENT_PREPARED`: intent durable, **jo POST i provuar**;
- `ORDER_IN_FLIGHT` / `ORDER_UNKNOWN`: rezultat i paqartë;
- `ORDER_FILLED` / `ORDER_REJECTED` / `ORDER_CANCELLED`: rezultati i
  klasifikuar dhe ruajtur në journal;
- `PROTECTION_VERIFIED`: SL/TP të verifikuara nga GET sipas journal-it;
- `BE_INTENT/UNKNOWN/CONFIRMED` dhe `PARTIAL_INTENT/UNKNOWN/CONFIRMED`:
  vetëm gjendja e verifikuar e lifecycle (jo pretendime mutacioni);
- `TRADE_CLOSED`: mbyllje e pajtuar me transaction ID, pa hamendësuar
  nëse ishte SL, TP, manuale apo automatike;
- `COOLDOWN_STARTED`: cooldown me provë të humbjes së konfirmuar,
  instrumentit dhe expiry.

Identiteti i një event-i PREPARED mbetet i pandryshuar kur porosia
merr më vonë order/trade ID. Replay pas restart nuk dyfishon eventet.

### Klasifikimi i mbylljeve nga transaksione reale

Parametri opsional `transaction_fetcher` kërkon provë shtesë me
`GET /transactions/{transactionID}`. Verifikohen transaction ID,
accountID, tipi `ORDER_FILL` dhe përkatësia e trade ID te
`tradesClosed`. Vetëm pasi provohet `reason` sipas OANDA:

- STOP_LOSS_ORDER, GUARANTEED_STOP_LOSS_ORDER, TRAILING_STOP_LOSS_ORDER
  → `SL_HIT`;
- TAKE_PROFIT_ORDER → `TP_HIT`;
- MARKET_ORDER_MARGIN_CLOSEOUT → `SYSTEM_CLOSE`.

Flamujt për këto evente nuk varen nga `CSV_LOG_TRADES`.
`MARKET_ORDER_TRADE_CLOSE` **nuk mjafton** për të dalluar manuale
kundrejt mbylljes së komanduar nga Algin, prandaj `MANUAL_CLOSE`
nuk falsifikohet. Ky klasifikim kërkon close-intent të plotë në F3.
Edhe ID e saktë e transaksionit BE/partial nuk ruhet ende në skemën
legacy të lifecycle; regjistrohet konfirmimi i pajtuar i state-it.
Këto janë varësi për mbylljen **operacionale** të P58/P60.

`main.project_account_audit` përdor GET vetëm-lexim dhe cache në
memorie për transaksionet immutable gjatë jetës së procesit.

## 3. Kode refuzimi dhe konfigurimi

`Strategy.last_rejection_reason` ofron arsyen e vlerësimit të fundit
sinkron, p.sh. `INVALID_CANDLES`, `TREND_OR_RESET`,
`BREAKOUT_RETEST_MISSING`, `STRUCTURE_NOT_CONFIRMED`,
`RETEST_INVALID`, `PATTERN_REJECTED`, `SCORE_BELOW_THRESHOLD`.
Kontrata e vjetër `evaluate_signal(...)` ruhet. Main regjistron
`SIGNAL_<REASON>` për secilin drejtim të refuzuar.

Flamujt specifikë NEWS, SESSION dhe COOLDOWN kanë prioritet
ndaj `CSV_LOG_SIGNALS`. Eventet e konfirmuara projektohen nga state-i,
jo nga një thirrje e supozuar brokeri.

## 4. Heartbeat dhe trajtimi i gabimeve

`Heartbeat.write` ruan ACTIVE/CYCLE_COMPLETE/BLOCKED me checksum,
fsync dhe atomic replacement. `Heartbeat.read` validon skemën,
timestamp dhe checksum. `inspect(max_age_seconds=180)` kthen
gjendjen read-only, përfshirë STALE.

Komandë për një monitor të jashtëm:

`python main.py --heartbeat-check`

Kthen 0 për ACTIVE/CYCLE_COMPLETE të freskët dhe 3 për STALE,
BLOCKED, skedar të munguar ose të dëmtuar. Nuk bën GET/POST.
Nuk është instaluar ende daemon watchdog i host-it (P62/T08).

Dështimi i CSV dhe përjashtimet e skanimit/menaxhimit e bllokojnë
ciklin; një CSV i korruptuar nuk duhet ta lërë heartbeat në ACTIVE.
Nuk logohen raw broker headers apo kredencialet.

## 5. Backtest, kostot dhe raportet

`stop_first_bar` ruan rregullin konservator SL-first kur SL dhe TP
preken brenda të njëjtit qiri për LONG/SHORT, pa hamendësuar rend
intrabar. `net_result` kërkon `gross_includes` eksplicit: kur nuk
dihet nëse spread, slippage, commission apo financing janë përfshirë
te gross P/L, llogaritja refuzohet. Ky është ndryshim i qëllimshëm
në kontratën e funksionit për të parandaluar double-counting.

`analytics.performance.report` ruan input-in ekzistues
trade_id/instrument/session/net_pl dhe shton max win/loss streak,
largest/mean win/loss, median dhe përqendrimin e fitimit te
tregtia më e madhe. Drawdown presupozon rend kronologjik.
Raporti mbetet funksion analitik mbi **input-e të dhëna**;
nuk garanton që P/L është neto i verifikuar nga OANDA (F1/F7).

## 6. Evidenca, regjistrimet dhe kufizimet

Testet: `tests/test_category6.py` dhe
`tests/test_category6_completion.py`, bashkë me regresionin e
kategorive 1–5. Skenarët: CSV i dëmtuar/duplikim/replay, stale
heartbeat, sinjale të refuzuara, fake-broker E2E, UNKNOWN,
BE/partial/close, SL/TP me GET të simuluar dhe kosto të
përfshira shprehimisht. Workflow `category6.yml` ekzekuton
compileall, unittest dhe `--check` në PR, degën e punës dhe main.

Kufizimet e mbetura pas këtij zhvillimi:

1. F1: ledger financiar autoritativ, ExposureBook dhe LossState.
2. F2/F3: pipeline operacional i hyrjes dhe close-intent, ID të
   transaksioneve BE/partial, provë e MANUAL_CLOSE.
3. F5: replay/backtest i plotë, kosto dhe parametra të testuar.
4. F6: teste real-time OANDA Practice me brokerin e vërtetë.
5. F7: forward test me kampion dhe periudhë të kontrolluar.

**Kategoria 6: baza teknike e auditimit dhe regresionit offline
e plotësuar. Aktivizimi i porosive mbetet i bllokuar.**
