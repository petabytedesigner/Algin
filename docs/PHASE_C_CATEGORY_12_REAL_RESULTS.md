# Faza C — Kategoria 12: Rezultatet empirike nga OANDA Practice

**Status faktik i përditësuar më 17 nëntor 2026:** lidhja
e mëparshme e autentikuar, hapja reale e një tregtie me SL/TP dhe
reduktimi i pjesshëm janë provuar nga OANDA. **Përdoruesi ka raportuar
më pas mbylljen manuale të 10 njësive të mbetura me +9.0 pip në momentin
e mbylljes.** Ky informacion është deklaratë e pronarit, jo një
`ORDER_FILL` i verifikuar nga API. Prova e fundit e suksesshme
në 02:43:19 UTC tregonte ende 10 njësi LONG; kjo është një fotografi
historike, **jo gjendja aktuale e pohuar**.

Auditimi i ri GET-only është implementuar dhe testuar, por OANDA
ktheu **HTTP 401** në dy endpoint-e autentikimi më 17 nëntor
2026 rreth orës 03:27 UTC. Për këtë arsye mbyllja finale,
P/L neto dhe anulimi i SL/TP nuk mund të certifikohen ende nga
përgjigjet aktuale të brokerit. Nuk u dërgua asnjë porosi e re.
Faza 12 ka **implementim dhe testim të zgjeruar**, por verifikimi
operacional i mbylljes dhe BE reale mbeten të papërfunduara.

## 1. Prova e autentikuar e llogarisë

- Burim: [GitHub Actions / direct GET, run 44003632905](https://github.com/petabytedesigner/Algin/actions/runs/44003632905)
- Llogaria e verifikuar: `101-001-37674492-003`; environment Practice,
  home currency USD, balance përputhej me referencën `$10,000`.
- Katalogu OANDA përmbante 68 çifte FX të pranuara nga parseri;
  kuota EUR_USD ishte e freskët, e tregtueshme dhe brenda limitit të spread-it.
- Para testit: zero pozicione të hapura, zero urdhra në pritje, transaction
  cursor i pandryshuar.
- Kredencialet u lexuan **nga skedari ekzistues i përdoruesit `.env.example`**,
  pa i printuar ose ndryshuar. Nuk u kërkuan GitHub Secrets.

## 2. Prova e porosisë reale dhe e zvogëlimit

- Burim: [GitHub Actions / one-shot demo, run 44004560576](https://github.com/petabytedesigner/Algin/actions/runs/44004560576)
- OANDA pranoi `POST /orders` me HTTP 201, market FOK, `OPEN_ONLY`,
  `clientExtensions.id=algin-c12-20261117-once` dhe 20 njësi EUR_USD LONG.
- Order ID `5`, Trade ID `6`, OrderFillTransaction `6`.
- U verifikua `GET /trades/6`: pozicioni i përkiste eksperimentit dhe
  kishte **të dy urdhrat SL/TP** të brokerit.
- Spread-i i lexuar ishte `1.6` pip; humbja **nominale në çmimin bound/SL**
  ishte `0.08520 USD`. Kjo nuk është maksimum i garantuar kundër gap-it,
  slippage-it të stop-it, rollover ose financimit.
- BE **nuk** u dërgua, sepse çmimi real nuk kishte lëvizur në favor me
  më shumë se 10 pip.
- OANDA pranoi `PUT /trades/6/close` me `{"units":"10"}`:
  **HTTP 200 + ORDER_FILL**. Pasi u lexua historiku real, u identifikua
  MarketOrderTransaction `9` dhe OrderFillTransaction `10`.
- `tradeReduced.tradeID="6"` dhe `tradeReduced.units="-10"`.
  **Kjo është shenjë SELL kundër një LONG**, jo reduktim prej -10 në madhësi.
  Snapshot-i i brokerit konfirmoi `currentUnits="10"`, pra mbyllja e
  pjesshme u ekzekutua.

## 3. Root cause dhe trajtimi konservativ

Runner-i fillestar i eksperimentit priste
`tradeReduced.units="10"` në vend të vlerës së nënshkruar `"-10"`
të OANDA-s. Ky gabim **nuk shkaktoi dërgimin e një urdhri tjetër**:
në vend të kësaj ndërpreu procesin para full-close. OANDA kishte
ekzekutuar saktë partial-close. Kontrata e runner-it është korrigjuar
dhe fixtures e regression-it tani përdorin vlerën reale negative.

Reconciler-i operacional `broker/account_reconciler.py` tashmë
normalizon reduktimin me `.copy_abs()`; ky gabim ishte i izoluar
në runner-in e testit real.

## 4. Snapshot-i i fundit GET-only

Burim: [GitHub Actions / Account & Transaction GET,
run 44005031533](https://github.com/petabytedesigner/Algin/actions/runs/44005031533).

| Fusha | Vlera e vëzhguar më 17-11-2026, 02:43:19 UTC |
|---|---|
| Account | Practice `...003`, USD |
| Trade ID | `6` |
| Status | `OPEN`, 10 EUR_USD LONG |
| SL | `1.09553` |
| TP | `1.11369` |
| Pending protective orders | `2` |
| Balance | `9999.9984 USD` |
| NAV | `9999.9987 USD` |
| lastTransactionID | `10` |

**Më pas përdoruesi deklaroi se e mbylli manualisht tregtinë** me
`+9.0 pips` për pjesën e mbetur. Prandaj udhëzimi i mëparshëm për
mbyllje nuk është më veprim i kërkuar. Mbetet vetëm verifikimi read-only
i mbylljes së raportuar, numrit të pozicioneve/urdhrave dhe P/L neto.
Asnjë urdhër tjetër nuk duhet nisur vetëm mbi këtë snapshot të vjetër.

## 5. Kufijtë e këtij PR-je

Në `main` nuk aktivizohen `ORDER_WRITES_ENABLED` ose
`_PRACTICE_WRITES_RELEASED`. Nuk lidhet asnjë rrugë mutacioni
me `main.py`: vazhdon `OBSERVE_ONLY`. Adapteri i provës është
i ndarë nga `PracticeTransport` operacional; runner-i ka
ID fikse, hard-cap 20 njësi, kontroll të brokerit për
deduplikim, journal fsync para veprimit dhe ndalim pa retry në
`UNKNOWN`. Workflow-t **one-shot** të përdorura u hoqën nga dega
pas ekzekutimit që të mos aktivizojnë aksidentalisht transaksione
të tjera. History e GitHub Actions mbetet si provë reale.

Ky test nuk mat win-rate, profitabilitet, BE të suksesshëm,
financimin neto të të gjithë ciklit apo forward-testing 24/7.
Kategoria 12 mbetet **pjesërisht e përfunduar** derisa të provohet
flat account dhe cikli i menaxhimit/financimit.

## 6. Auditimi i mbylljes manuale dhe HTTP 401

- [Run 44010127960](https://github.com/petabytedesigner/Algin/actions/runs/44010127960): **15/15 teste offline PASS**; kontrolli real GET u bllokua nga përgjigje jo-200.
- [Run 44010242985](https://github.com/petabytedesigner/Algin/actions/runs/44010242985): diagnostikim vetëm me URL të kontrolluara në kod; `GET /accounts/{id}` ktheu **HTTP 401**.
- [Run 44010354053](https://github.com/petabytedesigner/Algin/actions/runs/44010354053): `GET /v3/accounts` i OANDA Practice ktheu **HTTP 401** dhe `GET /v3/accounts/{id}` ktheu **HTTP 401**. Kjo tregon se autorizimi aktual me token-in ekzistues nuk po pranohet as për listën e llogarive. Shkaku i saktë (token i pavlefshëm, i revokuar, ose refuzim nga shërbimi) **nuk përcaktohet vetëm nga 401**.
- SHA e blob-it të `.env.example` në GitHub mbeti e pandryshuar krahasuar me commit-in e provës së mëparshme të suksesshme. Token-i nuk u printua dhe skedari nuk u ndryshua.

`broker/practice_close_audit.py` është një auditues **GET-only** që:
1. Kërkon inventar llogarie pa tregti dhe pa urdhra të hapur, USD dhe ID të saktë.
2. Kërkon `GET /trades/6` me `state=CLOSED`, `currentUnits=0`, identitetin e trade-it dhe kohën e mbylljes.
3. Verifikon `GET /transactions/sinceid?id=10`, transaksionet `6` (OPEN), `10` (PARTIAL, `units=-10`) dhe ID-në terminale `ORDER_FILL` me `tradesClosed` në të njëjtën tregti.
4. Pajton `Trade.realizedPL` me shumën e `tradeReduced.realizedPL` dhe `tradesClosed.realizedPL`; raporton veçmas `financing`, `commission`, bilancin dhe ndryshimin nga referenca `$10,000`.
5. **Nuk e paraqet** ndryshimin e bilancit si neto të atribuueshme vetëm tregtisë dhe nuk supozon kosto zero kur fusha mungon.

Në këtë version nuk ka urdhra të rinj, retry të mutacioneve, ndryshim të
`main.py`, release të transportit real apo ndryshim të riskut. Kalimi i
testeve nuk zëvendëson provën e mbylljes në OANDA.

**Përfundim i auditimit:** hapja e pozicionit dhe reduktimi janë
`BROKER_CONFIRMED`; mbyllja finale është `USER_REPORTED_API_UNVERIFIED`.
BE real nuk është provuar, sepse kushti i çmimit nuk u realizua gjatë
eksperimentit. Statusi i Kategorisë 12 për të dy pikat mbetet
`BLOCKED_EXTERNAL_EVIDENCE`, jo `PASS`.
