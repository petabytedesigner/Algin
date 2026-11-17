# Faza C — Kategoria 12: Rezultatet empirike nga OANDA Practice

**Status faktik:** autentikimi, katalogu, kuota, hapja reale me SL/TP dhe
mbyllja e pjesshme janë verifikuar kundrejt OANDA Practice. **Mbyllja e
plotë dhe Break-Even NUK janë përfunduar.** Në snapshot-in e fundit të
autentikuar (2026-11-17 02:43:19 UTC) mbeteshin 10 njësi EUR_USD LONG,
me dy urdhra mbrojtës. Ky dokument nuk e klasifikon llogarinë si flat.

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

**Nuk u verifikua mbyllje e plotë.** Përpjekja për të iniciuar një
workflow të ri rikuperimi u bllokua para dërgimit të një kërkese
tjetër; prandaj s'ka evidencë transaksioni full-close. Mos e
interpreto `CI PASS` ose `HTTP 200` të partial-close si
mbyllje të pozicionit. Për të shmangur ekspozimin, pronari mund
të mbyllë **Trade ID 6, të gjitha 10 njësitë**, përmes ndërfaqes
Practice të OANDA-s dhe të verifikojë `openTradeCount=0`.
Kjo nuk kërkon konfigurime në GitHub.

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
