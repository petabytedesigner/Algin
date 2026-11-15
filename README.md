# Algin — Trading Bot | Dokumentimi teknik dhe plani i zhvillimit

> **STATUSI AKTUAL: OBSERVE_ONLY / OANDA PRACTICE / PA URDHRA REALE.**
>
> Gjashtë kategoritë e zhvillimit janë integruar në `main` dhe testet automatike offline kanë kaluar. **Kjo nuk do të thotë se boti është gati të hapë, menaxhojë apo mbyllë tregti në OANDA.** Rruga e plotë operative, kontabiliteti i riskut dhe verifikimi real në Practice kanë ende punë të pambyllura. Asnjë parametër nuk garanton fitim.
>
> **Referenca e këtij dokumentimi:** auditimi i datës 2026-11-15, me kodin bazë `a0ecdf172793caccf3b065d4adc8749bd6aa9a26` (para përditësimit të këtij README). Këtu dokumentohen aftësitë e verifikuara në kod, kufizimet aktuale dhe **projektimi F0–F7**, jo zbatimi i atij projektimi.

## Përmbajtja

1. [Qëllimi, gjendja dhe kufijtë](#1-qëllimi-gjendja-dhe-kufijtë)
2. [Instalimi dhe përdorimi](#2-instalimi-dhe-përdorimi)
3. [Arkitektura dhe rrjedha e të dhënave](#3-arkitektura-dhe-rrjedha-e-të-dhënave)
4. [Strategjia dhe filtrat e sinjaleve](#4-strategjia-dhe-filtrat-e-sinjaleve)
5. [Risku dhe planifikimi i pozicioneve](#5-risku-dhe-planifikimi-i-pozicioneve)
6. [Integrimi OANDA dhe trajtimi i porosive](#6-integrimi-oanda-dhe-trajtimi-i-porosive)
7. [Gjendja, rikuperimi dhe menaxhimi](#7-gjendja-rikuperimi-dhe-menaxhimi)
8. [Lajmet, sesionet dhe instrumentet](#8-lajmet-sesionet-dhe-instrumentet)
9. [Logimi, heartbeat dhe raportet](#9-logimi-heartbeat-dhe-raportet)
10. [Konfigurimi i projektit](#10-konfigurimi-i-projektit)
11. [Testet dhe evidenca e verifikimit](#11-testet-dhe-evidenca-e-verifikimit)
12. [Statusi i 75 pikave origjinale](#12-statusi-i-75-pikave-origjinale)
13. [Gjetjet e auditimit dhe detyrat e pambyllura](#13-gjetjet-e-auditimit-dhe-detyrat-e-pambyllura)
14. [Projektimi i vazhdimësisë: F0–F7](#14-projektimi-i-vazhdimësisë-f0f7)
15. [Problemet e zakonshme dhe rikuperimi i sigurt](#15-problemet-e-zakonshme-dhe-rikuperimi-i-sigurt)
16. [Rregullat e zhvillimit, miratimit dhe përfundimit](#16-rregullat-e-zhvillimit-miratimit-dhe-përfundimit)
17. [Referenca dhe dokumentet mbështetëse](#17-referenca-dhe-dokumentet-mbështetëse)

---

## 1. Qëllimi, gjendja dhe kufijtë

**Algin** është një projekt bot-i tregtimi algoritmik në Python me strategji të bazuar në **EMA 20/50, ATR 14, strukturë tregu BOS/CHoCH dhe breakout → retest → konfirmim** në timeframe **M15**. Ai përdor kontratat e **OANDA REST v20** për leximin e të dhënave të llogarisë, instrumenteve dhe çmimeve, si dhe ka module të veçanta për planifikimin e riskut, përgatitjen e porosive, pajtimin e transaksioneve dhe auditimin.

Projekti është i pavarur nga robotë ose repository të tjerë. Dokumentimi dhe ndryshimet e këtij projekti duhet të kufizohen te `petabytedesigner/Algin`.

### 1.1 Çfarë është implementuar

| Fusha | Ekziston në kod | Çfarë është verifikuar |
| --- | --- | --- |
| Hyrja kryesore dhe komandat CLI | Po | Teste offline, `--check` |
| Leximi i qirinjve M15 dhe i kuotimeve OANDA | Po | Validim me përgjigje sintetike; jo prova operative të llogarisë aktuale |
| Sinjalet EMA/ATR/strukturë/retest | Po | Teste pozitive dhe negative sintetike |
| Politika e riskut dhe `TradePlan` | Po | Llogaritje `Decimal` me kontekst të simuluar dhe të verifikuar nga testi |
| Përgatitja e market order dhe `priceBound` | Po | Transport i simuluar; jo POST real |
| Journal i tentativave dhe klasifikim `UNKNOWN` | Po | Teste të dështimeve dhe të komunikimit të paqartë |
| Pajtimi i llogarisë dhe lifecycle state | Po, vetëm për lexim/ruajtje lokale | Teste offline të transaksioneve dhe rinisjes |
| Llogaritja e R, preview i BE dhe partial | Po | Pa ndryshime reale SL dhe pa mbyllje reale |
| CSV audit, heartbeat, raporte të kufizuara | Po | Teste të moduleve; jo zinxhir i plotë ngjarjesh operative |
| Ekzekutimi i porosive në OANDA Practice | **I çaktivizuar** | **Asnjë provë e konfirmuar nga kjo bazë** |
| P/L neto i plotë, backtest/replay, forward test | **Të papërfunduara** | Nuk ka evidencë performance të verifikuar |

### 1.2 Kufijtë e sigurisë që nuk duhen anashkaluar

- `config.ORDER_WRITES_ENABLED = False`.
- `broker/practice_transport.py` përmban portën e pavarur `_PRACTICE_WRITES_RELEASED = False`.
- `main.py` refuzon konfigurimin me shkrime të aktivizuara dhe mbetet `OBSERVE_ONLY`.
- `TradeExecutor.open_market_order()`, `modify_sl_tp()` dhe `close_trade()` nuk dërgojnë kërkesa; dështojnë në mënyrë eksplicite.
- `TradeManagement` mund të prodhojë preview ose të regjistrojë intent lokal, por **nuk** kryen BE/partial te brokeri.
- Një `TradePlan(preview_only=True)` nuk është autorizim tregtimi.
- Një `HTTP 404` pas një tentative të paqartë **nuk provon** se porosia nuk u ekzekutua.
- Mungesa e një SL-je të verifikuar, tregtitë manuale/të huaja, state-i i dëmtuar dhe rezultati `UNKNOWN` nuk duhen interpretuar si llogari e sigurt.
- Risku i llogaritur deri te stop-loss nuk garanton humbje maksimale në kushte gap, slippage, financimi apo kostoje shtesë.

**Këto mbrojtje mbeten të pandryshuara gjatë fazave F0–F5.** Aktivizimi eventual i Practice kërkon rishikim dhe miratim të veçantë në F6. Llogaria live nuk është pjesë e autorizimit aktual.

---

## 2. Instalimi dhe përdorimi

### 2.1 Kërkesat

- Python **3.10 ose më i ri**, me mjedis testimi GitHub Actions në **Python 3.11**.
- `requests>=2.31,<3` dhe `tzdata>=2025.2,<2028` sipas `requirements.txt`.
- Akses në internet vetëm për komandat që lexojnë OANDA; `--check` dhe testet offline nuk kanë nevojë për kredenciale.
- Për komandat që lidhen me OANDA: **llogari Practice** dhe çelës API që i përket asaj llogarie.
- Orë sistemi dhe timezone të sakta. Timestamp-et e analizës dhe të brokerit normalizohen në UTC.

**Linux / macOS:**

```bash
git clone https://github.com/petabytedesigner/Algin.git
cd Algin
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python main.py --check
python -m unittest discover -s tests -v
```

**Windows / PowerShell:**

```powershell
git clone https://github.com/petabytedesigner/Algin.git
cd Algin
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py --check
python -m unittest discover -s tests -v
```

**Termux / Android — instalim orientues, jo mjedis i certifikuar:**

```bash
pkg update
pkg install python git
git clone https://github.com/petabytedesigner/Algin.git
cd Algin
python -m pip install -r requirements.txt
python main.py --check
python -m unittest discover -s tests -v
```

Termux **nuk është testuar ende** për funksionim afatgjatë me ekran të fikur, ndryshim rrjeti, ndërprerje interneti, battery optimization ose rinisje të telefonit (P62). Një `--check` i suksesshëm nuk provon se shërbimi qëndron funksional gjatë natës.

### 2.2 Kredencialet dhe siguria

`.env.example` përmban vetëm emrat e variablave. **Projekti nuk e ngarkon automatikisht skedarin `.env`**; konfigurimi lexon variablat e mjedisit në import.

Linux / Termux, në një shell lokal privat:

```bash
export OANDA_API_TOKEN='TOKEN_I_PRACTICE'
export OANDA_ACCOUNT_ID='ID_E_LLOGARISE_PRACTICE'
python main.py --once
```

PowerShell, në një sesion lokal privat:

```powershell
$env:OANDA_API_TOKEN = 'TOKEN_I_PRACTICE'
$env:OANDA_ACCOUNT_ID = 'ID_E_LLOGARISE_PRACTICE'
python main.py --once
```

**Mos vendosni token, ID private, `.env`, skedarë të gjendjes, loge me të dhëna personale ose përgjigje të plota brokeri në Git.** Repoja është publike. Mos publikoni output-in e terminalit që përmban kredenciale. Mos përdorni Practice token në sistem tjetër pa verifikuar përkatësinë e llogarisë.

### 2.3 Komandat ekzistuese

| Komanda | Sjellja aktuale | Kërkon OANDA? | Ndryshon llogarinë? |
| --- | --- | --- | --- |
| `python main.py --check` | Verifikon importet dhe kontrollin e sigurisë së konfigurimit | Jo | Jo |
| `python -m unittest discover -s tests -v` | Ekzekuton testet me fixtures offline | Jo, sipas testeve aktuale | Jo |
| `python -m compileall -q .` | Kontrollon kompilimin dhe mund të krijojë bytecode lokal | Jo | Jo |
| `python main.py --once` | Një cikël vëzhgimi sinjalesh M15; lexon brokerin, mund të shkruajë heartbeat/CSV/checkpoint lokal | Po | **Jo** |
| `python main.py --manage-once` | Një cikël pajtimi të llogarisë/transaction history; mund të inicializojë/përditësojë lifecycle state lokal | Po | **Jo** |
| `python main.py` | Loop që mban lease-in e llogarisë, pajton gjendjen, pastaj skanon; gjumi midis cikleve është 60 s | Po | **Jo** |

**Dallim i rëndësishëm:** "Read-only" këtu do të thotë **pa mutacione OANDA**. Komandat `--once`, `--manage-once` dhe loop-i normal **mund të krijojnë ose të ndryshojnë skedarë lokalë**. Mos i përdorni mbi një kopje të gjendjes që duhet ruajtur e pandryshuar pa bërë kopje rezervë dhe pa vlerësuar paraprakisht rikuperimin.

Kodi i daljes i `main.py` përdor aktualisht `0` për përfundim normal ose bllokim jo-fatal të skanimit, `2` për kredenciale të munguara dhe `3` për një sërë dështimesh të sigurisë/rikuperimit. **`0` nuk nënkupton se ka sinjal, tregti ose fitim.**

---

## 3. Arkitektura dhe rrjedha e të dhënave

### 3.1 Harta e moduleve

```text
Algin/
├── main.py                      # CLI, scan_once, manage_once, loop
├── config.py                    # Politika aktuale; broker writes OFF
├── requirements.txt
├── .env.example                 # Shembull, jo ngarkues automatik
├── broker/
│   ├── risk_data.py             # Account / instrument / quote reads
│   ├── execution_validation.py  # Market order preflight & priceBound
│   ├── execution_result.py      # FILLED / REJECTED / CANCELLED / UNKNOWN
│   ├── order_engine.py          # Intent → attempted submit → reconciliation
│   ├── practice_transport.py    # OANDA Practice HTTP + hard write gate
│   └── account_reconciler.py    # Full account + transactions/sinceid
├── data/
│   ├── data_handler.py          # M15 candles dhe pricing
│   ├── candle_validation.py     # complete, UTC, OHLC, freshness, gaps
│   ├── candle_gate.py           # Një analizë për instrument / qiri M15
│   ├── market_sessions.py       # London/New York/Tokyo, DST
│   └── news_filter.py           # Cache lajmesh; UNKNOWN fail-closed
├── strategy/
│   ├── strategy.py              # EMA/ATR + drejtimi + sinjali
│   ├── breakout_retest.py       # Pivot → breakout → retest
│   ├── structure_engine.py      # BOS / CHoCH
│   ├── trend_engine.py
│   ├── retest_engine.py
│   ├── candle_rules.py
│   ├── pattern_filters.py
│   ├── ema_engine.py
│   └── atr_engine.py
├── risk/
│   ├── risk_engine.py           # Vendim risku / kufij
│   ├── trade_plan.py            # Sizing, SL/TP, rezervime, risk policy
│   ├── trade_lifecycle.py       # Gjendja e tregtisë, R, BE, partial
│   ├── exposure_engine.py       # Helper këshillues (legacy)
│   ├── scoring_engine.py        # Score dhe A/B/C/D
│   └── cooldown_engine.py       # Helper UTC (legacy)
├── execution/
│   ├── trade_executor.py        # Lexime legacy, mutacione të ndaluara
│   ├── trade_management.py      # Preview/stage BE/partial
│   ├── safety_check.py
│   ├── order_validation.py
│   ├── partial_close_engine.py  # Kompatibiliteti legacy
│   └── oanda_patch.py           # Helpers legacy, kryesisht lexime
├── storage/
│   ├── intent_journal.py        # Historiku durable i tentativave
│   ├── lifecycle_state.py       # State, checksum, lease dhe cursor
│   ├── event_log.py             # EventLog CSV
│   └── heartbeat.py             # ACTIVE / CYCLE_COMPLETE / BLOCKED
├── analytics/
│   ├── conservative_backtest.py # Stop-first OHLC dhe komponentët e kostos
│   └── performance.py           # Raport sintetik me net P/L
├── utils/                       # UTC, HTTP GET, indikatorë, pip helpers
├── tests/test_category1.py ... test_category6.py
├── docs/CATEGORY_1_REVIEW.md ... CATEGORY_6_REVIEW.md
└── .github/workflows/category1.yml ... category6.yml
```

Disa module `legacy` mbahen për kompatibilitet. Prania e tyre nuk do të thotë që përdoren në pipeline-in aktiv. Ndryshimi ose heqja e tyre kërkon inventar të importeve dhe teste kontratash.

### 3.2 Rrjedha aktive sot

```text
--once / scan_once
  ├─ Kontroll i konfigurimit, heartbeat ACTIVE
  ├─ GET openTrades (vetëm kontroll i kufizuar i ekspozimit)
  ├─ Kontroll i sesionit të hyrjes
  └─ Për çdo instrument:
       ├─ GET candles M15 → validate complete/UTC/OHLC/freshness
       ├─ GET pricing → tradeable + bid/ask i freskët
       ├─ CandleGate.claim (checkpoint për qiriun)
       ├─ EMA20 / EMA50 / ATR14
       ├─ LONG & SHORT → BOS / breakout / retest / candle / scoring
       ├─ SIGNAL_OBSERVED (nëse gjendet)
       └─ NewsFilter → NEWS_UNKNOWN/lockout ose RISK_NOT_RECONCILED
  └─ heartbeat CYCLE_COMPLETE (nëse cikli arrin fundin)

--manage-once / management-loop
  ├─ GET full Account details / transaction cursor
  ├─ GET transactions/sinceid sipas nevojës
  ├─ Verifikim i journal-it, pronësisë, SL/TP, inventory
  └─ Ruajtje e kontrolluar e lifecycle state ose BLOCKED
```

**Mungon ende lidhja operative:**

```text
StrategySignal
  → VERIFIED ExposureBook + VERIFIED LossState
  → TradePlanner.plan
  → final broker preflight / priceBound
  → IntentJournal
  → Broker submit + reconciliation
  → SL/TP verified
  → BE / partial / close verified
  → true net P/L + risk state + audit
```

Kodet e këtyre komponentëve ekzistojnë pjesërisht, por **zinxhiri i plotë nuk është lidhur në `main.py`**. Nuk duhet përshkruar si funksional vetëm sepse importet dhe testet e moduleve kalojnë.

---

## 4. Strategjia dhe filtrat e sinjaleve

### 4.1 Parimet e implementuara

- **Timeframe:** M15. Për sinjale përdoren qirinj me `complete=true`; qiriu i fundit i papërfunduar hidhet poshtë. Një qiri i papërfunduar brenda serisë refuzohet.
- **Të dhënat:** çmime `mid` për OHLC strategjik; renditje pa dublikime, çmime pozitive e finite, kontrolle high/low, timestamp UTC në kufijtë 15-minutësh dhe kontroll i vjetërsisë.
- **Treguesit:** EMA 20 (fast), EMA 50 (slow), ATR 14. Çmimi i ekzekutueshëm bid/ask i brokerit nuk ngatërrohet me midpoint-in e qirinjve.
- **Trend/drejtim:** LONG kërkon trend të përshtatshëm UP dhe qiri konfirmues bullish; SHORT kërkon DOWN dhe qiri bearish. Reset zone, mbizgjatja dhe pattern-et e pavlefshme bllokojnë.
- **Struktura:** swing i konfirmuar me dy qirinj në secilën anë; BOS me mbyllje përtej nivelit, jo vetëm me wick; CHoCH sipas kontekstit të strukturës.
- **Sekuenca:** niveli strukturor ekziston *para* breakout-it; breakout-i konfirmohet me mbyllje dhe retesti ndodh më pas. Nuk pranohet breakout/retest në të njëjtin qiri. Ekzistojnë rregulla skadimi/invalidation.
- **Checkpoint:** `CandleGate` bën rezervimin e qiriut para vlerësimit të strategjisë. Pas crash-it mund të humbet një sinjal, por e njëjta analizë nuk duhet ridërguar verbërisht.
- **Score:** metrikat e trendit, strukturës, retestit, qiriut, EMA dhe ATR; `pattern_quality` ul rezultatin. `liquidity_sweep` nuk ndikon sa kohë nuk është matur. Pragjet bazë të gradave: A ≥ 80, B ≥ 60, C ≥ 40, D < 40.

**Kufizime të rëndësishme:** `Strategy.evaluate_signal` mund të prodhojë C, ndërsa `TradePlanner` dhe përgatitja e porosisë lejojnë vetëm A/B. `MAX_RETEST_BARS` ekziston në `config.py`, por konstruktori i strategjisë përdor aktualisht vlerën fikse `4`. Strategjia e rindërton setup-in historikisht; ende nuk ka identitet/state të qëndrueshëm të breakout-it të përdorur.

### 4.2 Çfarë nuk është provuar

Asnjë win rate, profit factor, expectancy ose fitim historik i kësaj strategjie nuk është i konfirmuar nga testet e njësive. Mungon replay/backtest që përdor **të njëjtën** logjikë hyrjeje, përjashtimet, sizing-un, ekzekutimin dhe kostot si boti operacional. Ndryshimet në strategji duhen bërë **pas** krijimit të një baseline performance të riprodhueshme.

---

## 5. Risku dhe planifikimi i pozicioneve

### 5.1 Politika aktuale — provizore, jo garanci

| Parametri | Vlera në `config.py` | Interpretimi |
| --- | --- | --- |
| `RISK_GRADE_A_PCT` | 1.0% | Buxheti maksimal nominal për kandidat A |
| `RISK_GRADE_B_PCT` | 0.5% | Buxheti maksimal nominal për kandidat B |
| `MAX_TOTAL_RISK_PCT` | 1.0% | Kufiri i totalit të riskut të rezervuar |
| `MAX_INSTRUMENT_RISK_PCT` | 1.0% | Përqendrimi për instrument |
| `MAX_CURRENCY_RISK_PCT` | 1.0% | Përqendrimi sipas valutës së përbashkët |
| `MAX_ACTIVE_TRADES` | 3 | Numri maksimal i rezervimeve/tregtive |
| `MAX_TRADES_GRADE_A` / `B` | 2 / 3 | Kufijtë sipas gradës |
| `MAX_CONSECUTIVE_LOSSES` | 3 | Bllokim i hyrjeve pas humbjeve të njëpasnjëshme |
| `MAX_DAILY_LOSS_PCT` | 2.0% | Kufiri i humbjes ditore të verifikuar |
| `MAX_DRAWDOWN_PCT` | 5.0% | Bllokim bazuar në peak NAV të verifikuar |
| `RISK_REWARD_RATIO` | 3.0 | Objektivi minimal nominal R:R |
| `ATR_STOP_MULTIPLE` | 1.5 | Bazë për distancën e SL |
| `MIN_DISTANCE_PIPS` | 3 | Minimumi i distancës sipas pip-it të instrumentit |
| `MAX_MARGIN_FRACTION` | 0.5 | Deri në 50% të margin-it të disponueshëm sipas modelit |
| `MAX_QUOTE_AGE_SECONDS` | 20 | Kufiri i moshës së kuotimit në plan/preflight |
| `MAX_SPREAD_PIPS` | 2.5 | Kufiri aktual i përgjithshëm; nuk është kalibruar për çdo instrument |
| `SLIPPAGE_ALLOWED` | 2 | Tolerancë nominale në pip për `priceBound` |

Logjika e planifikimit përdor bazën konservatore `min(balance, NAV)`, `Decimal` për llogaritjet, madhësi të njësive të rrumbullakosur **poshtë**, kufij të brokerit dhe konvertimet `homeConversions`. Risku për pozicion nuk është thjesht "1% i llogarisë": kërkon stop-distance, konvertim të monedhës, sasi reale dhe kontroll të kufijve edhe pas rrumbullakimit. Instrumentet me kontratë ose multiplier të paprovuar nuk duhet të autorizohen duke hamendësuar.

### 5.2 Parakushtet që mungojnë në runtime

`TradePlanner.plan()` refuzon pa:

1. `AccountFunds` të vlefshëm, instrument metadata dhe kuotim të freskët.
2. `ExposureBook(verified=True, uncertain_outcomes_accounted=True)` të ndërtuar nga të gjitha tregtitë/porositë/intentet, përfshirë të paqartat.
3. `LossState(verified=True)` me daily loss, day-start NAV, peak NAV, cooldown dhe loss streak të verifikuara.
4. Kontekst që përputhet me të njëjtën llogari, instrument dhe kohë.
5. Vlerësim përfundimtar që nuk lejon tejkalim risku, margin-i ose ekspozimi.

**Sot nuk ekziston lidhja e plotë automatike që ndërton dhe verifikon `ExposureBook` dhe `LossState` nga `AccountReconciler`/`LifecycleStore`.** Kjo është pengesë e mirëfilltë për F1–F2, jo thjesht një test që mungon. `ReconcileReport.entry_allowed` mbetet `False`.

---

## 6. Integrimi OANDA dhe trajtimi i porosive

### 6.1 Mjedisi dhe API

Endpoint-i i konfiguruar është vetëm **OANDA Practice**:

```text
https://api-fxpractice.oanda.com/v3
```

Modelet e leximit përfshijnë `GET /accounts/{accountID}`, `/instruments`, `/pricing`, `/openTrades`, `/transactions/sinceid` dhe `/trades/{tradeID}`. Të dhënat aktuale janë në fushën e REST v20; statusi i çmimeve preferon `tradeable` (boolean). Fusha `status` e çmimit është **deprecated** në dokumentacionin aktual OANDA dhe disa adapterë kanë ende fallback për kompatibilitet.

### 6.2 Forma e përgatitur e porosisë, jo e dërguar

`broker/execution_validation.py` ndërton, pas validimeve, një `MARKET` order me:

- `units` pozitive LONG ose negative SHORT;
- `timeInForce = FOK` dhe `positionFill = OPEN_ONLY`;
- `priceBound` sipas drejtimit dhe tolerancës së slippage;
- `stopLossOnFill` dhe `takeProfitOnFill`;
- `clientExtensions` / `tradeClientExtensions` për identifikim dhe pronësi.

Mbështetja për `clientExtensions` është e kufizuar në llogaritë e përshtatshme; llogaritë e lidhura me MT4 nuk duhen përdorur pa rishikim të veçantë të kontratës OANDA. Asnjë përgjigje nga `orderCreateTransaction` nuk është provë e `tradeID` së hapur.

### 6.3 Rezultatet dhe idempotenca

| Gjendja | Kuptimi | Veprimi i sigurt |
| --- | --- | --- |
| `PREPARED` | Intent ruajtur para dërgimit | Mos supozo që nuk u transmetua pas ndërprerjes |
| `IN_FLIGHT` | Transmetimi mund të jetë tentuar | Mos ridërgo verbërisht |
| `UNKNOWN` | Nuk ka provë vendimtare për rezultat | Vetëm GET/reconciliation; bllokim i hyrjeve |
| `FILLED` | Fill i verifikuar | Verifikim i tregtisë reale, units, SL/TP dhe pronësisë |
| `REJECTED` | Refuzim i vërtetuar | Regjistrohet si rezultat negativ, pa ridërgim automatik |
| `CANCELLED` | Anulim i vërtetuar | Regjistrohet si rezultat terminal |

`IntentJournal` ruan `attempt_id`, `signal_id`, payload-in e kërkesës dhe fazat. Ai është i nevojshëm, por **nuk e provon vetë** ekzekutimin e brokerit. Një përgjigje bosh, timeout, 429/5xx ose 404 gjatë kërkimit nuk autorizon POST të dytë. API-ja e brokerit mund të ndryshojë gjendjen edhe kur klienti nuk merr përgjigje.

**Defekt i identifikuar për F0:** `request_hash` ruhet, por nuk rillogaritet/konfirmohet gjatë leximit të journal-it. Ekziston edhe rast skajor me symlink të prishur, sepse `exists()` kontrollohet para `is_symlink()`. Këto kërkojnë patch dhe teste, jo reset manual të state-it.

### 6.4 Bllokimi aktiv i shkrimeve

`PracticeTransport` nuk dërgon realisht `POST /orders` në build-in aktual. Kontrolli është në transportin më të ulët HTTP dhe nuk anashkalohet duke vendosur një variabël mjedisi. Nuk ka mbështetje të liruar për `PUT/PATCH` të mbrojtjes ose mbyllje tregtie.

**Mos e ndryshoni vetëm `ORDER_WRITES_ENABLED` ose portën e transportit për të "nisur botin".** Pa gjendjen e pajtuar, journal recovery, kontroll të humbjeve dhe audit të ekzekutimit, ky do të ishte ndryshim me rrezik të lartë.

---

## 7. Gjendja, rikuperimi dhe menaxhimi

### 7.1 Reconciliation i llogarisë

`AccountReconciler`:

- kërkon snapshot të plotë të llogarisë dhe `lastTransactionID`;
- verifikon `accountID`, trade/order counts, identifikuesit dhe pronësinë e tregtisë;
- lexon `transactions/sinceid` kur cursor-i ndryshon;
- refuzon pozicione të huaja/manuale, SL/TP të munguar ose të ndryshuar pa provë;
- e trajton mungesën e një tregtie të hapur si **të pamjaftueshme** për të provuar mbylljen;
- përdor një `ProcessLease` për të shmangur dy procese që menaxhojnë të njëjtën llogari;
- ruan në `LifecycleStore` të dhëna të kontrolluara me checksum dhe zëvendësim atomik.

### 7.2 Lifecycle i tregtisë

`TradeRecord` përmban identifikuesin real, instrumentin, drejtimin, `entry_price`, `initial_stop`, `current_stop`, `take_profit`, njësitë fillestare/aktuale, riskun origjinal, statuset BE/partial, kohën, transaksionet e mbylljes dhe `net_pl_home` të akumuluar.

R llogaritet nga **distanca fillestare e riskut** dhe drejtimi i pozicionit; për LONG matet dalja me bid, për SHORT me ask. BE propozohet vetëm pas një lëvizjeje të favorshme. Partial close kontrollon madhësinë, precision-in dhe minimumin e brokerit; njësitë që mbyllen janë madhësi pozitive edhe për SHORT.

**Kufizim aktual:** `stage_break_even()` / `stage_partial()` regjistrojnë `INTENT` lokal **pa kërkesë mutacioni te OANDA**. Intenti i pazgjidhur mund ta bllokojë më pas reconciler-in. Mos i thirrni si "provë operative" pa projektuar paraprakisht ciklin e dërgimit, verifikimit dhe rikuperimit.

### 7.3 Skedarët lokalë kritikë

Shembujt më poshtë përdorin emra konceptualë. `ACCOUNT_ID` është placeholder, **jo** vlera juaj reale:

| Vendndodhja | Roli |
| --- | --- |
| `.local/execution_intents/ACCOUNT_ID.json` | Tentativat dhe rezultatet e kërkesave |
| `.local/execution_intents/ACCOUNT_ID.lock` | Lock i shkrimit të journal-it |
| `.local/lifecycle/practice.ACCOUNT_ID.json` | Gjendja durable e tregtive, cursor-it dhe rezultateve |
| `.local/lifecycle/practice.ACCOUNT_ID.write.lock` | Lock i shkrimit të lifecycle |
| `.local/locks/practice.ACCOUNT_ID.lease` | Lease i procesit të menaxhimit |
| `.local/candle_gates/ACCOUNT_ID.json` | Checkpoint-et e analizës M15 |
| `.local/candle_gates/ACCOUNT_ID.lock` | Lock i checkpoint-it |
| `.local/heartbeat.json` | Gjendja e fundit e procesit |
| `csv/events.csv` | Logimi i strukturuar i vëzhgimeve |

Skedarët e `.local/` janë të përjashtuar nga Git. **`csv/events.csv` nuk përputhet aktualisht me rregullin `csv/events_*.csv` të `.gitignore`**; ky është detyrim i pambyllur sigurie (L04) dhe duhet korrigjuar në një fazë të autorizuar për kod. Ky README nuk e ndryshon `.gitignore`.

### 7.4 Procedurë e sigurt në rast dështimi

1. **Mos** e nisni një instancë të dytë mbi të njëjtën llogari.
2. **Mos** fshini journal, lifecycle JSON, lease ose lock për të "zhbllokuar" sinjalet.
3. Identifikoni procesin që zotëron lease-in, orën e heartbeat-it, llogarinë dhe cursor-in e fundit të transaksionit.
4. Kontrolloni gjendjen reale të OANDA me **GET** dhe krahasoni tregtitë, porositë, transaksionet dhe mbrojtjet.
5. Ruani një kopje të pandryshuar të gjendjes origjinale përpara çdo riparimi ose migrimi.
6. Vetëm pas pajtimit të dokumentuar dhe miratimit operacional mund të trajtohet një lock i mbetur ose një intent i pazgjidhur.
7. Nëse nuk mund të provohet gjendja, sistemi duhet të mbetet **BLOCKED**.

Mos përdorni `git reset`, fshirje masive ose kopjim state-i nga një llogari tjetër si zëvendësim të reconciliation.

---

## 8. Lajmet, sesionet dhe instrumentet

### 8.1 Sesionet

`MarketSessions` përdor zonat IANA `Europe/London`, `America/New_York` dhe `Asia/Tokyo`. Për Forex përdor kufirin javor të New York (hapje e së dielës dhe mbyllje e së premtes, rreth 17:00 orë lokale) dhe dritare të përcaktuara të sesioneve. Përtej kalendarit, kërkohet edhe kuotim **i tregtueshëm** dhe i freskët nga brokeri.

Kjo **nuk është** një tabelë e verifikuar e orareve për çdo instrument, CFD, metal apo kriptomonedhë. Kontrolli aktual `instrument_tradeable()` pranon vetëm çifte të njohura Forex. Edhe me `ENABLE_SESSIONS=False`, kontrolli i brendshëm i tregtueshmërisë vazhdon të kërkojë sesion në `main.py`; kjo mospërputhje duhet sqaruar në projektim.

### 8.2 Universi i skanimit

Lista aktuale e shkruar në `main.py` është:

```text
EUR_USD, GBP_USD, USD_JPY, USD_CAD, AUD_USD,
NZD_USD, GBP_JPY, EUR_JPY, XAU_USD, BTC_USD
```

**XAU_USD dhe BTC_USD nuk kalojnë politikën e tanishme Forex-only**, pavarësisht se janë në listën e skanimit. Lista e instrumenteve duhet të konfigurohet dhe verifikohet për llogarinë Practice (P53). Asnjë instrument nuk duhet supozuar i disponueshëm pa të dhëna reale të OANDA.

### 8.3 Filtri i lajmeve

`NewsFilter` përdor timestamp UTC, `fetched_at`, TTL dhe kontroll të vjetërsisë së burimit. Bllokimi për ndikim të lartë synon valutat e instrumentit: dritare **30 minuta para** dhe **30 minuta pas** eventit.

**Nuk ka aktualisht një burim funksional lajmesh të konfiguruar në `config.py`**. Prandaj `NEWS_UNKNOWN` duhet trajtuar si **bllokim i hyrjeve të reja**, jo si provë se nuk ka lajme. Cache-i dhe validimi i formatit janë implementuar; lidhja reale me provider-in mbetet P55.

---

## 9. Logimi, heartbeat dhe raportet

### 9.1 CSV / Audit events

`storage/event_log.py` ofron një skemë fikse me fushat:

```text
timestamp, event_id, correlation_id, instrument, direction,
stage, event, outcome, reason_code, attempt_id, order_id,
transaction_id, trade_id, detail
```

Rezultatet e skemës përfshijnë `ATTEMPT`, `SUCCESS`, `FAILURE`, `UNKNOWN`, `BLOCKED` dhe `INFO`. Këto janë **lloje të lejuara të eventit**, jo dëshmi se runtime-i i prodhon të gjitha. Vlerat e fushave kontrollohen për të ulur rrezikun e CSV injection; regjistri nuk duhet të përmbajë token apo payload sekret.

Në `scan_once` ekzistojnë ngjarje `SIGNAL_OBSERVED` dhe disa `ENTRY_DENIED`; lidhja e plotë me ndryshimet reale të brokerit mungon. `CSV_LOG_TRADES`, `CSV_LOG_PARTIAL`, `CSV_LOG_BE`, `CSV_LOG_SL_HIT`, `CSV_LOG_TP_HIT`, `CSV_LOG_MANUAL_CLOSE` dhe `CSV_LOG_SYSTEM_CLOSE` janë të deklaruara por nuk prodhojnë ende evente reale të këtyre veprimeve.

**P58/P60 nuk janë të përfunduara plotësisht.** Duhet korrelacion i verifikuar nga `signal_id` te planifikimi, `attempt_id`, `order_id`, `transaction_id`, `trade_id` dhe mbyllja; pa loguar të dhëna sekrete.

### 9.2 Heartbeat

`storage/heartbeat.py` ruan `ACTIVE`, `CYCLE_COMPLETE`, `BLOCKED`, `cycle_id` dhe timestamp në skedar atomik me checksum. Kjo është **gjendja e raportuar nga procesi**, jo monitorim i jashtëm. Nëse procesi ka vdekur, një `CYCLE_COMPLETE` i vjetër nuk do të thotë se ai është ende aktiv. Një watchdog i pavarur mbetet punë e ardhshme.

### 9.3 Analitika

- `analytics/conservative_backtest.py` ka rregullin **stop-first**: nëse SL dhe TP preken brenda të njëjtit qiri OHLC, vendoset supozimi konservator SL. Nuk rikrijon sekuencën intrabar dhe nuk modelon të gjitha gap fills.
- `FillCosts` ndan spread, slippage, commission dhe financing. Këto nuk mund të zbriten automatikisht dy herë kur disa janë përfshirë tashmë në çmimin/`realizedPL` e brokerit.
- `analytics/performance.py` llogarit numrin e tregtive, net P/L të dhënë nga thirrësi, equity përfundimtar, win rate, expectancy, profit factor, maximum drawdown dhe ndarje sipas instrumentit/sesionit.
- **Raporti nuk ka ende pipeline të verifikuar të rezultateve neto reale**, as backtest/replay të plotë. Mungojnë disa matje të planit fillestar, si seritë e humbjeve dhe përqendrimi i fitimit në pak tregti të mëdha.

---

## 10. Konfigurimi i projektit

**Burimi aktual i konfigurimit:** `config.py`. Shumica e vlerave nuk lexohen nga environment; përveç `OANDA_API_TOKEN` dhe `OANDA_ACCOUNT_ID`, mos supozoni se mund të ndryshohen me `export` pa ndryshuar kodin.

| Grupi | Parametrat kryesorë |
| --- | --- |
| Brokeri | `API_KEY`, `ACCOUNT_ID`, `OANDA_URL`, `ORDER_WRITES_ENABLED=False` |
| Indikatorët | `EMA_FAST=20`, `EMA_SLOW=50`, `ATR_PERIOD=14` |
| Risku | `RISK_GRADE_A_PCT=1.0`, `RISK_GRADE_B_PCT=0.5`, `MAX_TOTAL_RISK_PCT=1.0`, `MAX_DAILY_LOSS_PCT=2.0`, `MAX_DRAWDOWN_PCT=5.0` |
| Ekspozimi | `MAX_ACTIVE_TRADES=3`, kufijtë A/B, instrument/currency 1.0%, pa duplicate/hedging |
| SL/TP | `ATR_STOP_MULTIPLE=1.5`, `MIN_DISTANCE_PIPS=3`, `RISK_REWARD_RATIO=3.0` |
| Margin & kuotimi | `MAX_MARGIN_FRACTION=0.5`, `MAX_QUOTE_AGE_SECONDS=20`, `MAX_SPREAD_PIPS=2.5`, `SLIPPAGE_ALLOWED=2` |
| Strategjia | `MAX_RETEST_BARS=4` (jo i lidhur plotësisht), `SIGNAL_CANDLE_MAX_AGE_MINUTES=45` |
| Sesioni | `ENABLE_SESSIONS=True`, IANA timezone + status OANDA |
| Lajmet | `NEWS_LOCKOUT_MINUTES_BEFORE/AFTER=30`, `NEWS_CACHE_TTL_MINUTES=15`, `NEWS_CACHE_MAX_AGE_MINUTES=30` |
| Auditimi | `CSV_ENABLED=True`, `CSV_PATH="csv"`, opsionet `CSV_LOG_*` |
| Heartbeat | `HEARTBEAT_PATH=".local/heartbeat.json"` |

`RISK_PER_TRADE=1.0` mbetet vlerë legacy dhe nuk është sinonim automatik i gjithë politikës moderne të `RiskPolicy`. Parametrat e modelimit duhen ndryshuar vetëm me testet përkatëse; nuk rriten për të detyruar botin të hapë tregti.

---

## 11. Testet dhe evidenca e verifikimit

### 11.1 Komandat lokale

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
python main.py --check
```

### 11.2 Gjendja e testimit në integrimin e Kategorisë 6

- Në PR #7, suite e dokumentuar **189/189** teste offline kaloi.
- Gjashtë grupet e kontrolleve të kategorive 1–6 për PR-në përfundimtare kaluan.
- `compileall` dhe `main.py --check` kaluan.
- Pas merge u verifikua përputhja e pemës së kodit me degën e testuar.
- Këto janë **evidenca të ekzekutimeve të mëparshme**, jo pretendim që testet u riekzekutuan sot mbi brokerin.

Shiko [PR #7](https://github.com/petabytedesigner/Algin/pull/7) dhe [GitHub Actions — Category 6](https://github.com/petabytedesigner/Algin/actions/runs/43763792994). Workflow-t e kategorive ndodhen në `.github/workflows/`. Ato përdorin Python 3.11 dhe testet offline. Në versionin aktual të workflow-ve, verifikimi në `main` pas merge nuk është ende portë e pavarur dhe e vazhdueshme për të gjitha ndryshimet.

### 11.3 Çfarë NUK mbulohet nga 189 teste

- Një cikël i plotë me porosi të vërtetë në OANDA Practice.
- Reconciliation mbi historik real me financime, komisione, stop amend, partial-close dhe restart të papritur.
- Sjellja e plotë kur klienti humbet përgjigjen por brokeri ka pranuar urdhrin.
- Testi i përbashkët i të gjitha moduleve në pipeline-in aktiv të hyrjes.
- Coverage i matur për çdo degë dhe property/fuzz testing i gjerë.
- Termux/Android si proces i vazhdueshëm në mjedisin përfundimtar.
- Backtest/replay, forward test, fitimshmëria neto dhe performanca live.

**Teste që kalojnë ≠ strategji fitimprurëse ≠ autorizim për tregtim.**

---

## 12. Statusi i 75 pikave origjinale

Pikat origjinale u ndanë në **63 pika të zhvillimit** të shpërndara në 6 kategori dhe **12 pika** që kërkojnë edhe prova, infrastrukturë ose punë të tjera jashtë implementimit fillestar.

| Kategoria | Pikat | Statusi në `main` |
| --- | --- | --- |
| **1. Themelet** | P1, P2, P4, P6, P7, P8, P63 | Implementim dhe teste offline të integruara |
| **2. Risku dhe plani** | P5, P9–P18 | Kontrata dhe sizing të integruara; input-et e verifikuara nga brokeri nuk lidhen ende automatikisht |
| **3. Broker order contract** | P19–P24, P26–P30 | Përgatitje, journal, klasifikim/reconciliation të testuar me fake transport; real writes OFF |
| **4. Gjendja dhe lifecycle** | P31–P40 | Reconciliation dhe menaxhim lokal; BE/partial/mbyllje reale OFF; disa lidhje financiare ende mungojnë |
| **5. Strategjia dhe të dhënat** | P41–P47, P50–P52, P54, P56 | Filtra dhe teste sintetike të integruara; performance e paverifikuar |
| **6. Auditimi dhe testet** | P57–P61, P64–P67, P70–P71, P74 | Konstruksionet offline të integruara; **P58/P60 pjesërisht të pambyllura** |

**63/63 pika të kategorive kanë pasur implementime të integruara, por kjo nuk është "63/63 përfundim operacional".** Për çdo pikë mbetet dallimi mes kodit ekzistues, testit sintetik dhe provës së vërtetë me brokerin. Punët e reja të auditimit përfshijnë edhe defekte që nuk ishin të shprehura në 75 pikat fillestare.

### 12 pikat që kërkojnë edhe validim jashtë kategorive

| Pika | Puna e nevojshme |
| --- | --- |
| P3 | Konfigurim, kredenciale, validim i llogarisë dhe instalimit në host-in përfundimtar |
| P25 | Spread real sipas instrumentit dhe ndikimi i filtrit |
| P48 | Kalibrim i score/Grade me rezultate |
| P49 | Test i ndjeshmërisë sipas instrumentit |
| P53 | Inventar instrumentesh i vlefshëm për llogarinë konkrete |
| P55 | Provider funksional, i verifikuar dhe i sigurt i lajmeve |
| P62 | Termux/Android ose host tjetër: uptime, rrjeti, restart, battery/power |
| P68 | Cikël i plotë Practice: open, SL/TP, BE, partial, close, recovery |
| P69 | Replay historik me logjikën reale dhe pa lookahead bias |
| P72 | Krahasim i menaxhimit BE/partial kundrejt varianteve bazë |
| P73 | Testim out-of-sample dhe në regjime të ndryshme tregu |
| P75 | Forward test me parametra të fiksuar, kosto reale/simuluese dhe raport neto |

---

## 13. Gjetjet e auditimit dhe detyrat e pambyllura

**Rregulli i statusit:** tabelat më poshtë përshkruajnë gjetje nga analiza statike dhe kontratat e kodit, jo dështime të riprodhuara të gjitha në ekzekutim real. Për çdo rregullim kërkohen test pozitiv, test negativ dhe rishikim i ndikimit mbi gjendjen ekzistuese.

### 13.1 Kritike: integrimi dhe kontabiliteti (K01–K14)

| ID | Mungesa / rreziku | Rezultati i kërkuar |
| --- | --- | --- |
| **K01** | `scan_once` nuk lidhet me riskun dhe submit-in | Orkestrator i vetëm i ciklit, fillimisht me fake broker |
| **K02** | Nuk ndërtohet automatikisht `ExposureBook` i verifikuar | Inventar i plotë OPEN/PENDING/UNKNOWN me rezervime risk/margin |
| **K03** | Nuk ndërtohet `LossState` i verifikuar | Daily NAV/P&L, loss streak, peak NAV, cooldown nga prova brokeri |
| **K04** | `ReconcileReport.entry_allowed=False` | Vendim i shprehur, me kushte të forta dhe prova, jo flamur arbitrar |
| **K05** | Journal `FILLED` nuk ka cikël të plotë terminal pas close | Ruajtje e fillit dhe lirimit të sigurt pas konfirmimit të mbylljes |
| **K06** | BE/partial janë vetëm lokale; `INTENT` mund të bllokojë | Tranzicione PREVIEW/PREPARED/SENT/UNKNOWN/CONFIRMED të verifikueshme |
| **K07** | Fill pa mbrojtje të verifikuar | Procedurë GET-only për verifikim/karantinë, pa ridërgim |
| **K08** | Realized P/L nuk është domosdoshmërisht total neto | Model i provuar për komisione, financing dhe tarifa pa dyfishim |
| **K09** | Transaksione të tjera financiare nuk pajtohen plotësisht | Politika për çdo lloj relevant transaksioni + cursor i besueshëm |
| **K10** | Nuk ekziston release workflow operacional | Gate e pavarur me prova dhe miratim para Practice writes |
| **K11** | `request_hash` nuk verifikohet gjatë leximit | Integritet payload/attempt + teste për manipulim |
| **K12** | Symlink i prishur mund të maskojë journal që mungon | Reject symlink përpara `exists()`, pa inicializim të heshtur |
| **K13** | Cursor mund të përditësohet para përfundimit të të gjithë bllokuesve | Tranzicion atomik që ndan provën e lexuar nga autorizimi i gjendjes |
| **K14** | Nuk ka procedurë të plotë për intent `UNKNOWN` | Pajtimi, arkivimi dhe re-entry gate pa asnjë retry të verbër |

### 13.2 Strategjia dhe market data (S01–S10)

| ID | Puna |
| --- | --- |
| S01 | Identitet i qëndrueshëm për setup-in breakout/retest, me expiry/used/cancelled |
| S02 | Lidhje e provuar BOS ↔ i njëjti pivot/level që përdor retesti |
| S03 | Një burim i vetëm për `MAX_RETEST_BARS` |
| S04 | Semantikë e qartë dhe teste për `ENABLE_SESSIONS` |
| S05 | Konfigurim dhe kufizim i instrumenteve sipas kontratave reale |
| S06 | Kalibrim i score-ve me rezultate reale, jo interpretim si probabilitet |
| S07 | Dallim i metrikave të pamatura nga metrikat e matura me vlerë zero |
| S08 | Politikë e shprehur për Grade C: statistikë apo refuzim para riskut |
| S09 | Teste të veçanta për FX, metale dhe instrumente me karakteristika të ndryshme |
| S10 | Replay historik pa lookahead me të njëjtat kontrata si runtime-i |

### 13.3 Brokeri dhe API (O01–O06)

| ID | Puna |
| --- | --- |
| O01 | Adapter i përbashkët për `tradeable` boolean dhe fallback të dokumentuar nga `status` deprecated |
| O02 | Kufij spread-i të matur sipas instrumentit, me statistikë bllokimesh |
| O03 | Verifikim i instrumenteve të listuara dhe të tregtueshme për llogarinë |
| O04 | Unifikim i kontratave të leximit HTTP/JSON dhe klasifikimit të gabimeve pa prishje interfacesh |
| O05 | Teste me payload/transaction real Practice, pa aktivizim të pakontrolluar |
| O06 | Timeout/rate limiting, limite cikli, circuit breaker dhe menaxhim i sekuencës së GET |

### 13.4 Auditimi dhe monitorimi (L01–L08)

| ID | Puna |
| --- | --- |
| L01 | **P58:** lidhja e të gjitha `CSV_LOG_*` me ngjarje të verifikuara |
| L02 | **P60:** gjurma e plotë signal → risk → request → result → trade → close |
| L03 | Zëvendësimi i `STRATEGY_NO_SIGNAL` gjenerik me kode refuzimi specifike |
| L04 | Përjashtimi i `csv/events.csv` nga Git dhe politika e ruajtjes së logeve |
| L05 | Detektim i CSV të ndërprerë, rotacion/retention dhe test rikuperimi |
| L06 | Monitor i jashtëm për heartbeat të vjetruar dhe proces të ndalur |
| L07 | Raport me loss streak, ekspozim të rezultateve ekstreme, net P/L të verifikuar |
| L08 | Trajtim i qëndrueshëm i dështimit të audit logging pa humbur arsyen e bllokimit |

### 13.5 Testet, siguria dhe operimi (T01–T10)

| ID | Puna |
| --- | --- |
| T01 | E2E me fake broker nga candle deri në close dhe net result |
| T02 | Crash-injection para/pas fsync, POST, fill dhe state commit |
| T03 | Teste konkurrencë, lock/lease të mbetur dhe rikuperim të kontrolluar |
| T04 | Property/fuzz tests për `Decimal`, NaN, JSON, precision, ID dhe transaksione |
| T05 | Coverage i matur për degët kritike |
| T06 | CI në `main` pas merge, përveç PR/branch |
| T07 | Paketim/lock të varësive dhe audit sigurie të dependency-ve |
| T08 | Teste të host-it final: rrjeti, ekrani, energjia, uptime, restart |
| T09 | Backup dhe restore të provuara për `.local/` dhe cursor |
| T10 | Rishikim i helper-ave legacy, dokumentimit dhe llogaritjeve të dyfishta |

### 13.6 GitHub / procesi

- **PR #1** është një draft i vjetër për hardening të gjashtë kategorive, i pambyllur dhe me konflikte ndaj `main` në auditimin e datës 2026-11-15. **Mos e bashkoni** pa trajtim të veçantë të historisë; ai nuk është baza e re e zhvillimit.
- Repository është **publik**. Duhet mbrojtje e degës, kontroll i sekreteve dhe review e ndryshimeve; statusi i plotë i branch protection nuk u verifikua për shkak të kufizimit të API-së.
- Dokumentet `docs/CATEGORY_*_REVIEW.md` ruajnë kontekstin historik të etapave. Referencat e tyre ndaj punëve "të ardhshme" përshkruajnë momentin e krijimit të kategorisë; për statusin e përmbledhur aktual përdoret ky README dhe kodi në `main`.

---

## 14. Projektimi i vazhdimësisë: F0–F7

**Ky është projektim i ardhshëm. Asnjë fazë F0–F7 nuk deklarohet e implementuar vetëm nga prania e saj këtu.** Faza të ndryshme mund të kenë komponentë ekzistues; qëllimi është mbyllja e boshllëqeve dhe verifikimi i kritereve të pranimit. Çdo fazë ka PR, prova dhe miratim të veçantë.

### F0 — Baseline, integritet dhe përgatitje për zhvillim

**Fusha:** K11–K13, L04, T03, T07/T09, menaxhimi i PR-ve të vjetra.

- Ruajtja e commit-it bazë dhe inventari i kontratave të gjitha moduleve.
- Testet për journal të dëmtuar, hash të ndryshuar, symlink, locks dhe state ndërmjet llogarive.
- Dallimi "gjendje e ruajtur si evidencë" nga "gjendje e pajtuar që lejon hyrje".
- Strategji backup/restore pa fshirje automatike.
- Kontroll i publikimit aksidental të CSV/logeve dhe i varësive.
- Rezultat: një baseline fail-closed që nuk humbet historik dhe nuk zhvendos cursor-in pa prova.

**Pranimi:** çdo state i dëmtuar/mospërputhje refuzohet; asnjë rivendosje në bosh; zero mutacione OANDA; teste negative + CI PASS.

### F1 — Kontabiliteti autoritativ dhe reconciliation

**Fusha:** K02–K05, K08–K09, K14, T02.

- Ndërtimi i `ExposureBook` nga inventari i llogarisë, journal-i dhe rezervimet e hapura/pending/unknown.
- Ndërtimi i `LossState` nga transaksione dhe rezultate të verifikuara, me day-start NAV, peak NAV, streak dhe cooldown.
- Model P/L që trajton saktë `realizedPL`, komisione, financime dhe tarifa, pa dyfishuar spread/slippage.
- Politika e transaksioneve të panjohura dhe e cursor-it gjatë ndryshimeve konkurrente.
- Tranzicioni pas një mbylljeje të konfirmuar që liron rezervimin pa humbur provën e tentativës.
- Zgjidhja e çështjeve UNKNOWN vetëm me evidencë brokeri, jo me timeout apo 404.

**Pranimi:** restart-i rikrijon të njëjtën gjendje risku dhe P/L ose bllokon; kursori nuk anashkalon transaksione relevante; asnjë order i ri gjatë UNKNOWN.

### F2 — Pipeline operativ me broker të simuluar

**Fusha:** K01, K04, S02/S07/S08, O01/O04, T01.

- Koordinator i qartë i hyrjes: candle → signal → news/session → reconciled risk → trade plan → final validation.
- Identitet i qëndrueshëm për sinjalin dhe të gjitha vendimet/arsyet e refuzimit.
- Rrugë pozitive LONG/SHORT me fake broker, plus bllokim për risk, quotes, precision, margin dhe përqendrim.
- Verifikim i planit kundrejt snapshot-it të fundit; asnjë "verified=True" i sajuar.

**Pranimi:** një sinjal i vlefshëm prodhon maksimumi një tentativë të simuluar; sinjali i pavlefshëm prodhon zero POST; të gjitha rastet kanë audit të riprodhueshëm.

### F3 — Lifecycle: mbrojtje, BE, partial dhe mbyllje

**Fusha:** K06–K07, K14, O05, T02/T03.

- Përkufizim formal i fazave PREVIEW/PREPARED/SENT/UNKNOWN/CONFIRMED.
- Validim i SL/TP pas fill-it dhe politikë për gjendje të pambrojtur.
- Menaxhim BE/partial që nuk përsërit veprimet pas timeout ose restart.
- Konfirmim i units të mbetura, i SL/TP aktual dhe i transaksionit të mbylljes.
- Regjistrim i një rezultati të vetëm për tregti, edhe në rast partial dhe disa transaksionesh.

**Pranimi:** me fake broker, e gjithë tregtia kalon nga hapja te mbyllja, përfshirë LONG/SHORT, humbjen e përgjigjes dhe restart; zero dublikime.

### F4 — Auditimi, siguria operacionale dhe raportimi

**Fusha:** L01–L08, T04–T07, P58/P59/P60/P61/P74.

- Gjurmë korrelative për çdo sinjal, vendim risku, kërkesë, përgjigje, transaksion dhe mbyllje.
- Zbatim i flamujve CSV pa emetuar evente të sajuara.
- Loge të sigurta për rrotullim, rikuperim dhe retention; asnjë credential.
- Heartbeat i monitoruar nga jashtë dhe klasifikim i defekteve.
- Raport neto i bazuar në P/L dhe kostot e verifikuara, jo në supozime.
- Coverage/fuzz/CI në `main`.

**Pranimi:** për çdo test E2E mund të ndiqet zinxhiri i identiteteve dhe të shpjegohet çdo refuzim ose `UNKNOWN`; raportet pajtohen me ledger-in.

### F5 — Të dhënat, strategjia, replay dhe validimi shkencor

**Fusha:** S01–S10, O02/O03/O06, P25/P48/P49/P53/P55/P69/P72/P73.

- Verifikimi i instrumenteve dhe orareve reale të tyre.
- Integrimi i një provider-i lajmesh me SLA të matshme për freshness dhe coverage.
- Identitet/state i setup-it breakout/retest dhe konfigurim konsistent.
- Dataset historik i gjurmueshëm, pa lookahead bias dhe me supozime të deklaruara për kosto/fill.
- Backtest/replay me motorin real, teste out-of-sample dhe sensitivity sipas instrumentit.
- Krahasim i menaxhimit me dhe pa BE/partial; parametra të fiksuar për çdo eksperiment.

**Pranimi:** rezultate të riprodhueshme, një baseline i pandryshuar, dallime të dokumentuara dhe kufij risku të respektuar. Nuk kërkohet fitim i garantuar.

### F6 — Verifikim operacional në OANDA Practice

**Fusha:** K10, O05, P3/P68, rikuperim me transaksione reale.

- Fillimisht **GET-only** me llogari Practice dhe kontroll të pronësisë, inventory, llojit të llogarisë, kuotimeve dhe instrumenteve.
- Gate e veçantë release; kontrolli i çdo kërkese që mund të ndryshojë llogarinë.
- Teste të kontrolluara Practice për open/SL/TP/BE/partial/close/restart, vetëm pas miratimit eksplicit.
- Regjistrim i ID-ve reale të brokerit, rezultateve UNKNOWN, slippage, kohëve dhe kostove.
- Procedurë e dokumentuar e ndalimit, rikuperimit dhe monitorimit.

**Pranimi:** prova reale të ciklit të plotë me llogari Practice; pas restart-it nuk ka dublikime, "ghost trades" ose mbrojtje të paverifikuara. **Asnjë kalim automatik në live.**

### F7 — Forward test dhe vendimi i performancës

**Fusha:** P62/P75, raporti neto, stabiliteti në kohë.

- Parametra të ngrirë për periudhën e testit.
- Regjistrim i sinjaleve, ekzekutimeve, spread/slippage, kostove dhe rezultatit final.
- Matje win rate, expectancy, profit factor, drawdown, loss streak dhe përqendrim të fitimit.
- Krahasim i forward test me replay; identifikim i devijimeve reale të ekzekutimit.
- Testim i host-it për ndërprerje energjie/rrjeti dhe rikthim.

**Pranimi:** periudhë dhe kampion i mjaftueshëm sipas një protokolli të përcaktuar paraprakisht; rezultat neto, rrezik dhe besueshmëri operacionale të matshme. Kalimi te tregtimi live do të kërkonte proces **të ri** miratimi dhe vlerësimi risku.

---

## 15. Problemet e zakonshme dhe rikuperimi i sigurt

| Simptoma | Shpjegimi i mundshëm | Veprimi i sigurt |
| --- | --- | --- |
| `--check` kalon, por nuk ka tregti | Është kontroll importesh, jo komandë tregtimi | Mos e interpretoni si aktivizim brokeri |
| `--once` jep `MISSING_CREDENTIALS` | Variablat e mjedisit nuk janë vendosur | Kontrollo lokalisht sesionin dhe llogarinë Practice, pa publikuar token |
| `UNRECONCILED_EXPOSURE` | Ekzistojnë tregti të hapura ose llogaria nuk është pajtuar | Kryej vetëm GET/reconciliation të kontrolluar |
| `M15_MISSING` / `M15_INVALID` | Qirinj të paplotë, të vjetër, të mangët ose të pasaktë | Kontrollo kohën, instrumentin, formatin dhe historinë |
| `QUOTE_OR_SESSION_UNVERIFIED` | Nuk ka çmim të freskët, instrumenti nuk është tradeable ose sesioni nuk përputhet | Mos e hiq filtrin për të detyruar sinjal |
| `M15_ALREADY_PROCESSED` | Checkpoint-i i qiriut është konsumuar | Prit qiri të ri të mbyllur; mos fshi checkpoint |
| `NEWS_UNKNOWN` | Burim lajmesh i pakonfiguruar, i vjetër ose i pavlefshëm | Përfundo verifikimin e P55; mos e kthe UNKNOWN në CLEAR |
| `STRATEGY_NO_SIGNAL` | Një ose disa filtra nuk kalojnë | Kërkon logim më granular të refuzimit |
| `RISK_NOT_RECONCILED` | Sinjali është vetëm vëzhgim; nuk ka vendim risku | F1/F2 duhet të ndërtojnë kontratën operative |
| `Lock/lease exists` | Proces tjetër ose lock i mbetur pas crash-it | Verifiko pronësinë dhe broker state; mos e fshi verbërisht |
| `Journal checksum/schema problem` | State i pambesueshëm ose i dëmtuar | Ruaj evidencën, blloko dhe pajto të dhënat |
| XAU/BTC nuk prodhojnë sinjale | Lista përfshin instrumente që politika FX-only i bllokon | P53 + politikë instrument-specifike |
| Shumë refuzime nga spread-i | `2.5` pips është kufi i përgjithshëm | Mat spread-et reale për instrument, pa rritur riskun arbitrarisht |
| `events.csv` shfaqet në Git status | Rregull i paplotë në `.gitignore` | Mos e bëj commit; korrigjimi i kodit bëhet në F0 me autorizim |
| Testet PASS, por rezultatet mungojnë | Testet përdorin fixtures offline | Nevojiten F5–F7, jo pretendime fitimi |

---

## 16. Rregullat e zhvillimit, miratimit dhe përfundimit

### 16.1 Parime të detyrueshme

1. **Correctness → safety → clarity → speed.** Mos rritni frekuencën e tregtive ose riskun për të shmangur një bllokim teknik.
2. Ndryshime **të vogla dhe të izoluara**, me kontroll të ndikimit në module dhe skema të tjera.
3. Ruani ndërfaqet ekzistuese, përveçse kur një migrim është projektuar dhe testuar.
4. Përgjigjet e brokerit trajtohen si **potencialisht të paplota, të vonuara ose të paqarta**.
5. **Asnjë blind retry** për POST/PUT/PATCH ose për mbyllje/BE/partial me rezultat të paqartë.
6. Gjendjet `UNKNOWN`, lock-et e dyshimta, state-i i dëmtuar dhe mungesa e historikut financiar kërkojnë **fail-closed**.
7. Mos adoptoni tregti manuale ose të sistemeve të tjera pa provë pronësie dhe politikë të shqyrtuar.
8. Ndryshimet e riskut dhe strategjisë kërkojnë krahasim me baseline-in dhe teste të izoluara.
9. Ndani **"implementuar"**, **"testuar offline"**, **"verifikuar me Practice"** dhe **"autorizuar për operim"** në çdo raport.
10. Për kodin e fazave F0–F7, përdorni review, CI dhe **miratim të veçantë të përdoruesit para çdo merge**; mos aktivizoni mutacione OANDA si efekt anësor të një patch-i tjetër.
11. Mos ndryshoni repository të tjera në kuadër të këtij projekti pa autorizim eksplicit.
12. Dokumentoni në PR: motivin, kontratën e ndryshuar, ndikimin, skenarët negativë, provat, rollback-un dhe kufizimet që mbeten.

### 16.2 Përkufizimi i "përfunduar"

Një detyrë e ardhshme quhet e mbyllur vetëm kur ekzistojnë:

- implementimi dhe kontrata e dokumentuar;
- test pozitiv, test negativ dhe regresion për defektin përkatës;
- verifikim i ruajtjes së gjendjes, kufizimeve të riskut dhe sjelljes pas crash-it kur është relevante;
- CI mbi commit-in e saktë dhe diff-i i rishikuar;
- provë se nuk janë aktivizuar broker writes pa autorizim;
- miratim i integrimit dhe verifikim i commit-it pas merge.

**Përfundimi i kodit** nuk është **përfundim operacional**. Përfundimi operacional kërkon F6. **Fitimshmëria** nuk deklarohet pa F5/F7 dhe prova statistikisht e metodologjikisht të mbrojtshme.

---

## 17. Referenca dhe dokumentet mbështetëse

### Dokumentet e fazave të integruara

- [Kategoria 1 — Themelet](docs/CATEGORY_1_REVIEW.md): P1, P2, P4, P6, P7, P8, P63.
- [Kategoria 2 — Risku / sizing](docs/CATEGORY_2_REVIEW.md): P5, P9–P18.
- [Kategoria 3 — Broker execution contracts](docs/CATEGORY_3_REVIEW.md): P19–P24, P26–P30.
- [Kategoria 4 — Reconciliation / lifecycle](docs/CATEGORY_4_REVIEW.md): P31–P40.
- [Kategoria 5 — Strategjia / data integrity](docs/CATEGORY_5_REVIEW.md): P41–P47, P50–P52, P54, P56.
- [Kategoria 6 — Auditimi / testet](docs/CATEGORY_6_REVIEW.md): P57–P61, P64–P67, P70–P71, P74.

### Burime zyrtare OANDA REST v20

- [API introduction](https://developer.oanda.com/rest-live-v20/introduction/)
- [Best practices dhe account snapshot/update](https://developer.oanda.com/rest-live-v20/best-practices/)
- [Account endpoints](https://developer.oanda.com/rest-live-v20/account-ep/)
- [Instrument definitions](https://developer.oanda.com/rest-live-v20/instrument-df/)
- [Pricing endpoints](https://developer.oanda.com/rest-live-v20/pricing-ep/)
- [Pricing definitions — tradeable, deprecated status](https://developer.oanda.com/rest-live-v20/pricing-df/)
- [Order endpoints](https://developer.oanda.com/rest-live-v20/order-ep/)
- [Order definitions — market order, FOK, priceBound, positionFill](https://developer.oanda.com/rest-live-v20/order-df/)
- [Trade endpoints](https://developer.oanda.com/rest-live-v20/trade-ep/)
- [Transaction endpoints](https://developer.oanda.com/rest-live-v20/transaction-ep/)
- [Transaction definitions](https://developer.oanda.com/rest-live-v20/transaction-df/)

### Gjendja e GitHub dhe historia

- Kategoritë 1–6 janë integruar në `main` në gjashtë PR të veçanta (#2–#7).
- [PR #7 — Category 6 merged](https://github.com/petabytedesigner/Algin/pull/7).
- [PR #1 — draft i vjetër, i pambyllur](https://github.com/petabytedesigner/Algin/pull/1): kërkon triage; nuk është burim aktual i kodit.
- `docs/CATEGORY_*_REVIEW.md` mbeten dokumente historike për kriteret e secilës kategori.
- Ky README është dokumenti i përmbledhur për **gjendjen aktuale, rreziqet e identifikuara dhe planin F0–F7**; nuk zëvendëson provat e ekzekutimit dhe nuk aktivizon tregtimin.

---

**Përmbledhje e gjendjes:** Algin ka komponentë të strukturuar për sinjalet, riskun, OANDA, rikuperimin dhe auditimin. **Puna prioritare është integriteti i state-it dhe kontabiliteti i plotë (`F0–F1`), pastaj lidhja dhe testimi i pipeline-it (`F2–F4`), vlerësimi i strategjisë (`F5`), verifikimi i kontrolluar Practice (`F6`) dhe forward test (`F7`).** Deri atëherë, **`OBSERVE_ONLY` dhe bllokimi i të gjitha mutacioneve të brokerit mbeten të detyrueshme.**
