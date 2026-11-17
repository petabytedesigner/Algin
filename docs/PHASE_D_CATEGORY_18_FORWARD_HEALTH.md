# Faza D — Kategoria 18: semantika e saktë e heartbeat dhe forward evidence

## Problemi i verifikuar

Përpara këtij ndryshimi, `ForwardSupervisor._inspect` e kthente si
`SAMPLE` si një `ACTIVE` të freskët, ashtu edhe një `CYCLE_COMPLETE`.
`report_runs` i numëronte të dy si `healthy_samples`, pa identitet
cikli. Duke lexuar disa herë të njëjtin heartbeat, raporti mund të
pretendonte cikle të reja që nuk kishin ndodhur. Për më tepër,
`recent_block_reason` përdorte rendin leksikografik të emrave UUID
të skedarëve, jo kohën e ngjarjes.

## Kontrata e përmirësuar

- `LIVENESS/ACTIVE`: procesi i monitoruar dha një heartbeat të freskët,
  me PID e pritur. **Nuk është një cikël i përfunduar**.
- `SAMPLE/CYCLE_COMPLETE`: një heartbeat i përfunduar me `cycle_id`
  të vlefshëm që nuk është numëruar më parë në këtë seancë.
- `LIVENESS/CYCLE_ALREADY_OBSERVED`: i njëjti `cycle_id` u lexua
  sërish. Numërohet si vëzhgim i gjallërisë, jo si cikël i ri.
- `GATE/SESSION_BLOCK`: gjendje e kontrolluar e mbylljes së sesionit,
  jo cikël tregtimi apo autentikim brokeri.
- `BLOCKED`: ndalim në PID të huaj, heartbeat të vjetër, ID të
  pavlefshme, gabim brokeri ose evidencë të paplotë.
- Për `--samples N`, një seancë vetëm me `ACTIVE` dhe pa asnjë
  `CYCLE_COMPLETE` të ri apo `SESSION_BLOCK` mbyllet me kod 3,
  `NO_COMPLETED_CYCLE`. `--samples` numëron përsëri kontrolle,
  jo porosi apo cikle tregtimi.
- Të gjitha vëzhgimet përdorin një lexim të vetëm të heartbeat-it në
  çdo kontroll; nuk bëhet retry ose restart automatik.

## Evidenca dhe përputhshmëria

Regjistrimet e reja të `ForwardJournal` janë **schema v2**:
ruajnë `cycle_id` brenda hash chain SHA-256 me `fsync`.
`read_run` lexon ende historikun **v1**, por `SAMPLE` të vjetër
pa `cycle_id` raportohet si `legacy_unverified_samples`, jo si
cikël unik i verifikuar. Dublikimet e `SAMPLE` v2 me të njëjtën
ID në një run refuzohen; timestamp-et duhet të jenë monotone.

Raporti ruan emrat e mëparshëm të fushave dhe shton:

| Fusha | Kuptimi |
|---|---|
| `samples` | Numri total i vëzhgimeve `SAMPLE + LIVENESS + GATE` |
| `healthy_samples` | Numri i `cycle_id` unike të `CYCLE_COMPLETE` v2 |
| `completed_cycle_samples` | E njëjta metrikë, me emër eksplicit |
| `liveness_samples` | Pulse `ACTIVE` dhe `CYCLE_ALREADY_OBSERVED` |
| `gated_samples` | Vëzhgime të gjendjes `SESSION_BLOCK` |
| `legacy_unverified_samples` | `SAMPLE` historike v1 pa ID cikli |
| `recent_block_reason` | Nga ngjarja `BLOCKED` me timestamp-in më të ri |
| `broker_verified_cycles` | Gjithmonë 0 nga vetëm heartbeat evidence |
| `broker_validation_status` | `NOT_ATTESTED_BY_HEARTBEAT` |

**E rëndësishme:** `CYCLE_COMPLETE` tregon përfundimin e rrjedhës
lokale të skanimit. Ai **nuk është vetë provë** e një GET-i të
autentikuar OANDA, pajtimit financiar të brokerit, 24 orëve uptime
në Termux ose tregtisë së realizuar. Një provë e tillë kërkon evidencë
të pavarur me transaksione dhe inventar OANDA të verifikuar.
`uptime_24h` mbetet `NOT_MEASURED_ON_DEVICE`.

## Kufijtë e ndryshimit

Vetëm supervisor-i, formati dhe raportimi i forward journal, testet,
workflow i stresit sintetik dhe ky dokument. Nuk ndryshohen skedarët
e kredencialeve, `main.py`, `Heartbeat`, portat e brokerit,
risk/strategy, ndonjë urdhër OANDA ose procesi i restart-it.

## Kriteret e pranimit

- Testet `--check`, `unittest discover`, CI branch coverage dhe
  stresi sintetik me 1500 `cycle_id` të ndryshme: PASS.
- Teste regresioni për heartbeat `ACTIVE` pa cikël, ID të përsëritura,
  heartbeat pa ID, rendin kohor të bllokimeve dhe skedarë v1/v2.
- Integrim vetëm pas verifikimit të gjithë workflows në PR.
