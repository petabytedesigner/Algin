# Faza B — Kategoria 05: Koordinatori qendror i hyrjes (vetem offline)

## Qellimi dhe kufiri i zbatimit

`execution/trade_coordinator.py` lidh **sinjalin e strukturuar te Strategy** me filtret e hyrjes, `AccountReconciler`, `build_risk_readiness`, `TradePlanner` dhe me **OrderEngine ekzistues**. Qellimi eshte prova E2E e kontratave ne nje transport sintetik. **Nuk eshte operator i llogarise OANDA** dhe nuk lidhet ende automatikisht me ciklin `main.py`. Ai mbetet `OBSERVE_ONLY`.

`config.ORDER_WRITES_ENABLED=False`, `broker.practice_transport._PRACTICE_WRITES_RELEASED=False` dhe `ReconcileReport.entry_allowed=False` mbeten te pandryshuara. `TradeCoordinator.simulate` pranon vetem tipin ekzakt `SimulatedOrderTransport`, nje fixture ne memorie pa HTTP. `PracticeTransport`, implementimet e tjera ose subclass-et me metoda te mbishkruara refuzohen. Nje portë konfigurimi e hapur refuzon gjithashtu simulimin; kjo rruge nuk eshte release per live/PRACTICE.

## Rrjedha

1. `CoordinatorRequest` kerkon `StrategySignal`, kohen e qiririt te konfirmuar M15, ATR, rregulla te instrumentit, `QuoteConversions`, `AccountEvidence` dhe `EntryGates` me verifikim te shprehur. `EntryGates` jane atestime te jashtme; nje vlere True ne fixture **nuk certifikon** kalendar real apo status real brokeri.
2. `stable_signal_id` eshte determinist nga account, instrument, drejtim, timeframe dhe koha e qiririt. Score, grade dhe ora e procesit nuk krijojne identitet te ri. `IntentJournal` ndalon riperdorimin e sinjalit edhe pas refuzimit/filled, duke ruajtur gjurmen e tij.
3. `preview()` kontrollon filtret, moshen e qiririt, ID e llogarise, journal snapshot, cursor-in e reconciler-it dhe ledger-in/historikun e riskut permes `build_risk_readiness`. `TradePlanner` prodhon vetem `TradePlan(preview_only=True)`. Mungesa e history-anchor, transaksionet `UNKNOWN`, porosite e papajtuara, filtrat e pa verifikuar ose nje signal i perseritur bllokojne planin.
4. `simulate(initial, refresh, transport)` kerkon nje funksion sinkron `refresh()` dhe ribën te gjitha kontrollet e riskut pas planit fillestar. Nese identiteti, kursi, balanca, NAV, margin apo plani ndryshon, refuzon. `SimulatedOrderTransport.matches` verifikon edhe kuotën dhe account-in sintetik para kalimit ne `OrderEngine.submit`.
5. `OrderEngine` ruan request-in ne journal para nje tentative te vetme sintetike, kontrollon preflight, spread, `priceBound`, units, SL/TP, dhe pas simulimit lexon pozicionin sintetik per te verifikuar mbrojtjen. `UNKNOWN` nga timeout-i sintetik **nuk ridërgohet**. Kontrolli i deduplikimit perdor journal-in durabel, jo vetem një set ne memorie.

## Kontratat e rezultateve

`CoordinationResult.status`:
- `PREVIEW`: plan i verifikuar nga fixture sintetik, pa u dërguar ndonjë porosi.
- `SIMULATED`: `OrderEngine` kreu vetëm nje POST sintetik dhe konfirmoi SL/TP në fixture.
- `BLOCKED`: nje input/filtrim/risk/preflight u refuzua, ose nje rezultat sintetik nuk u konfirmua.
- `UNKNOWN`: pas tentatives sintetike rezultati eshte i paqartë; **ndalohet ridërgimi**.

`entry_allowed` eshte **gjithmone False** ne kete modul. Kjo eshte e qellimshme: as `RiskReadiness.inputs_verified`, as `PREVIEW`, as `SIMULATED` nuk jane autorizim tregtimi real.

## Varet nga kategorite vijuese

- **06**: adapteret e brokerit OANDA, validimi i perputhshmerise v20 dhe rezultateve te paqarta ne transport real.
- **07**: menaxhimi operacional i tregtive, SL/TP, BE/partial/close dhe reconciliimi me intentet.
- **08**: auditimi/pergjimet end-to-end, crash testing dhe CI me kontrolle te pavarura.
- **09–10**: identiteti i plote i BOS/retest nga market-data, session/news/quote atestime reale dhe kalendar funksional.
- **12–13**: release i vecante vetem ne OANDA Practice pas verifikimit te historikut, account lease, risqeve dhe forward test. Asnje autorizim per shkrime te brokerit jepet ne kategorine 05.

## Evidence offline

`tests/test_phase_b_category05.py` provon rrugen pozitive ne `OrderEngine`, filtrat negative, NAV/price recheck, identitetin e qiririt, idempotencen e journal-it, restart/duplicate, `UNKNOWN` dhe faktin qe transportet reale ose te personalizuara nuk thirren. Testet automatike GitHub Actions duhet te kalojne ne PR para merge.

`main.py` dhe `OrderEngine` publik nuk ndryshohen nga kjo kategori. Nuk ka kredenciale apo porosi OANDA ne testim.
