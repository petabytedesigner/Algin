# Faza D — Kategoria 21: auditimi final i gatishmërisë operacionale

## Vendimi i kësaj kategorie

**NO_GO_AUTONOMOUS_EXECUTION.** Algin ka provuar kontratat e kodit
dhe zhvillimin OBSERVE_ONLY, por **nuk ka autorizim për tregtim
automatik OANDA Practice ose Live**.

Për një sistem që mund të hapë pozicione reale, një suite testesh e
gjelbër nuk provon njëkohësisht autentikimin real të brokerit,
përputhshmërinë e inventarit me journal-in, gjendjen e SL/TP,
riskun aktual të llogarisë dhe disponueshmërinë 24/7 të Termux.

Vendimi NO-GO mbetet i detyrueshëm edhe kur `--report` kthen exit 0
(kjo provon vetëm që raporti u gjenerua pa shkelje të portave lokale).

## Porta të ndara dhe provat e kërkuara

| Porta | Prova për ta konsideruar për shqyrtim në një fazë të ardhshme | Statusi në auditin lokal |
| --- | --- | --- |
| CI e `main` | Teste/regresion/coverage të gjelbra për **të njëjtin commit**, jo rezultate nga një degë tjetër | `NOT_ATTESTED_BY_LOCAL_AUDIT`; kontrollohet në GitHub |
| OANDA Practice GET | Sukses i autentikuar, llogaria e saktë, summary, instrumente, quote dhe cursor | `NOT_ATTESTED_BY_LOCAL_AUDIT` |
| Llogaria dhe lifecycle | Snapshot i freskët i tregtive + urdhrave me journal intent, reconciliation, cursor, pa `UNKNOWN` | `NOT_ATTESTED_BY_LOCAL_AUDIT` |
| SL/TP | Përputhje e plotë e urdhrave mbrojtës me tregtinë përkatëse, ID, tip, çmim, gjendje dhe tradeID | `NOT_ATTESTED_BY_LOCAL_AUDIT` |
| Risk aktual | Kapitali/NAV, margin, risk total, per trade dhe per currency, drawdown, humbjet e ditës, historiku i humbjeve | `NOT_ATTESTED_BY_LOCAL_AUDIT` |
| Ekonomia | Kalendar FMP ose burim tjetër i aprovuar e **real dhe i freskët**; `NEWS_UNKNOWN` është bllokim | `NOT_ATTESTED_BY_LOCAL_AUDIT` |
| Termux | Evidencë e vazhdueshme në pajisje Android, PID, gaps, uptime, ndërprerje OS/rrjeti, provë nga monitor i jashtëm | `NOT_ATTESTED_BY_LOCAL_AUDIT` |
| Recovery pas pasigurisë | Simulim i rrjetit të ndërprerë, 401, timeout, POST/PUT me rezultat `UNKNOWN`, restart, lock/orphan; **zero blind retry** | `NOT_ATTESTED_BY_LOCAL_AUDIT` |
| Ekzekutim i kontrolluar Practice | Miratim i veçantë dhe një integrim i ndarë; journal dhe GET verifikojnë fill, SL/TP, BE, partial/full | `NOT_ATTESTED_BY_LOCAL_AUDIT` |
| Kill switch / miratim | Procedurë e testuar për ndalimin dhe pajtimin e ekspozimit, risk caps të palëvizshëm dhe miratim njerëzor për release | `NOT_GRANTED_BY_CODE` |

Një tregti teknike e kaluar në Kategorinë 12 ose ekzistenca e
`.local/category12_one_shot.jsonl` **nuk e zëvendëson**
reconciliation e tanishëm; skedari nuk analizohet si certifikatë.
Rruga `broker/practice_demo_adapter.py` është provë e veçantë,
me autorizim manual njëherësh, **jo porta e prodhimit**.

## Kontrata e mjetit

`operations/release_readiness.py` bën vetëm lexime lokale të
auditimit të Kategorisë 20 dhe evidencës së
`operations.forward_journal`. Nuk ngarkon, dërgon, kopjon,
printon apo ndryshon `.env.example` dhe nuk thërret OANDA.

Në rrënjë të repository-t:

```sh
python -m operations.release_readiness --report
```

Kthen JSON me `decision=NO_GO_AUTONOMOUS_EXECUTION`,
`execution_authorized=false`, `broker_mutations=0`,
`observe_only_code_status`, `gate_checks` dhe `blockers`.

Kontrolli i detyrueshëm i lirimit të urdhrave:

```sh
python -m operations.release_readiness --require-execution-ready
```

Kjo komandë **kthehet gjithmonë me kodin 3** në këtë kategori,
pavarësisht sukseseve sintetike. Nuk ekziston opsion
`--force`, `--approve`, `--release` ose parametrizim
i komandës që mund të anashkalojë rezultatin.

`OBSERVE_ONLY` do të thotë vëzhgim dhe përgatitje kodi,
**jo** leje për hyrje tregtie. `FORWARD_JOURNAL_INCIDENT_UNRESOLVED`
do të thotë skedar i korruptuar, run i ndërprerë ose
ngjarje `BLOCKED`, që kërkon kontroll nga operatori.
Nuk fshihen lock-et, state, journal apo urdhrat e brokerit.

## Verifikimet pa urdhra

```sh
python -m unittest discover -s tests -p 'test_phase_d_category21.py' -v
python -m operations.runtime_audit
python -m operations.release_readiness --report
python -m operations.forward_supervisor --check
```

Për një test të **ndarë** të autentikimit real Practice,
operatori mund të përdorë mjetin e Kategorisë 20:

```sh
python -m operations.practice_ci_probe --run
```

Ai lexon skedarin ekzistues të kredencialeve vetëm për
GET dhe raporton `AUTHENTICATED_PRACTICE_VERIFIED` në
sukses. Nuk jep leje për urdhra dhe raporti NO-GO i
Kategorisë 21 **nuk pranon një shënim të thjeshtë Boolean**
si provë të brokerit.

Për test forward të kufizuar në Termux mund të përdoret
komanda ekzistuese:

```sh
python -m operations.forward_supervisor --run --samples 4 --interval 30
python -m operations.forward_supervisor --report
```

Ato prova janë për monitorim dhe nuk certifikojnë 24 orë
uptime; 24/7 dhe Android power management kërkojnë evidencë
nga pajisja dhe një kontroll të pavarur të disponueshmërisë.

## Procedura e kalimit në fazë të ardhshme

1. Ruaj si baseline commit-in e `main` dhe provat e
   gjelbra CI të këtij commit-i.
2. Verifiko me GET real OANDA Practice identitetin,
   inventarin, SL/TP, cursor-in dhe riskun; mos
   vendos urdhra gjatë auditimit.
3. Verifiko kalendarin ekonomik me një burim real
   dhe fail-closed për `NEWS_UNKNOWN`.
4. Mat monitorimin në Termux në pajisje dhe
   shpjego çdo ndërprerje; mos numëro heartbeat
   të përsëritur si cikle të reja.
5. Kryej skenarët e recovery dhe konfirmo
   `UNKNOWN -> STOP / RECONCILE / NO BLIND RETRY`.
6. Përpilo një **PR të ri dhe të izoluar** për
   një provë të kufizuar Practice me risk të
   caktuar e të miratuar, rollback dhe kill switch.
   Release-i duhet të marrë **miratim të veçantë**
   pas review dhe CI. Nuk lejohet të armatoset
   nga një test readiness ose nga ky dokument.
7. Kalimi në llogari reale (Live) do të ishte
   një proces **krejt tjetër**, jo pasojë automatike
   e suksesit të një testi Practice.

## Kufizimet e provës

Raporti e trajton `report_runs` vetëm si evidencë lokale
të integritetit të ngjarjeve; nuk vërteton se OANDA e ka
përfunduar një veprim. Mungesa e një lock-u nuk provon
mungesën e një procesi tjetër. Prania e një lock-u nuk
autorizon fshirjen e tij. Provat e të ardhurave,
`win_rate`, drawdown të llogarisë dhe disponueshmëria
24/7 nuk rrjedhin nga testet CI.

**Asnjë kontratë ekzistuese e brokerit, strategjisë, riskut
ose kredencialeve nuk ndryshohet në këtë kategori.**
