# Faza C — Kategoria 09: Saktësia dhe determinismi i strategjisë

**Status:** përmirësime të kufizuara në strategjinë e qirinjve M15 të mbyllur, identitetin e BOS/pivot-it, filtrimin e sinjaleve dhe kalendarin e sesioneve. Ky PR është **offline/read-only**. Nuk ndryshon kufijtë e riskut, endpoint-et, kredencialet, portat e shkrimit ose mekanizmin e porosive. `main.py` mbetet `OBSERVE_ONLY`.

## 1. Fusha sipas planit

Kategoria 09 trajton (a) identitetin e breakout/retest dhe lidhjen e tij me BOS/pivot, (b) `MAX_RETEST_BARS`, skadimin/anulimin dhe konfirmimin e parë, (c) scoring/Grade A–B dhe refuzimin e Grade C, (d) përdorimin konsistent të filtrit të sesionit. **Burimet reale OANDA, instrumentet/kuotat, lajmet dhe validimi i tyre operacional i përkasin Kategorisë 10**, jo këtij ndryshimi.

## 2. BOS dhe pivot-i historik

`BreakoutRetest.confirm` zgjedh swing-un e fundit të **konfirmuar me dy qirinj të mbyllur PARA breakout-it**, kërkon kalim të `close` përtej nivelit me buffer ATR, pastaj retest dhe candle confirmation.

`StructureEngine.detect_breakout` është kontrolli i ri i saktësisë, pa hequr `detect` ekzistues. Merr indeksin dhe çmimin e pivot-it të `BreakoutRetest`, ndërton swing-et vetëm nga `candles[:breakout_index]`, pastaj vlerëson BOS/CHoCH te qiriri `breakout_index`. Nuk pranon strukturë që konfirmon një swing tjetër nga ai që ka përcaktuar zonën e retest-it. Prandaj, të dhënat pas breakout-it nuk e rishkruajnë retroaktivisht identitetin strukturor.

## 3. Skadimi dhe sinjalet e dyfishta

- `BreakoutRetest()` lexon `config.MAX_RETEST_BARS` në inicializim. Nuk ekziston më konstanta paralele `expiry_bars=4` në `Strategy`; kontrata ruan vlerën e projektit `4`, por respekton ndryshimet e ardhshme të konfigurimit. Vlera duhet të jetë integer 1–12, jo boolean.
- Një breakout mund të konfirmohet vetëm nga **qiriri i parë** i retest-it që plotëson kushtet për drejtimin, prekjen, thellësinë dhe wick-un. Nëse një qiri historik ka konfirmuar tashmë të njëjtin setup, qirinjtë e mëvonshëm nuk japin sinjal të ri për të njëjtin breakout. Kjo është logjikë deterministe mbi historikun e mbyllur, pa state të fshehtë.
- `StrategySignal.setup_id` është një fushë e re **opsionale** dhe e llogaritur nga `instrument`, `direction`, koha UTC e pivot-it dhe koha UTC e breakout-it. Nuk varet nga indekset e dritares historike ose koha e retest-it. `TradeCoordinator` e përdor këtë identitet (kur ekziston dhe ka format të saktë) për të derivuar një `signal_id` të përhershëm në journal, që nuk ndryshon mes qirinjve pasues të të njëjtit breakout. Sinjalet e vjetra pa `setup_id` ruajnë **pikërisht hash-in ekzistues** të bazuar te qiriri M15; nuk migrohen apo riemërtohen sinjale të ruajtura.
- `CandleGate` vazhdon të bllokojë skanimin e dyfishtë të qiririt të njëjtë. Idempotenca e journal-it është shtresa e dytë për të njëjtin setup midis qirinjve. Nuk ka retry të porosive.

**Trade-off:** nëse retest-i i parë kalon konfirmimin teknik por sinjali përfundimtar refuzohet nga një filtër tjetër, një retest i dytë i të njëjtit breakout gjithashtu nuk jep sinjal. Është sjellje konservative, jo një supozim se filtri i parë u ekzekutua.

## 4. Scoring dhe Grade

`ScoringEngine` kërkon të gjitha metrikat aktive të peshëzuara, përveç `liquidity_sweep` që është shprehimisht opsionale derisa të matet. Mungesa e një metrike nuk trajtohet më si `0` i supozuar. Kufijtë e politikës **nuk ndryshojnë**: Grade A nga 80, Grade B nga 60, Grade C nga 40. `Strategy.evaluate_signal` nuk lëshon më `StrategySignal` të tregtueshëm për Grade C; kthen `last_rejection_reason=GRADE_NOT_TRADEABLE`. Grade D mbetet `SCORE_BELOW_THRESHOLD`. `TradePlanner` vazhdon të lejojë vetëm A/B. **Regresion historik i sqaruar:** fixture-i LONG i `test_category5.py` ka dy penalizime pattern-i dhe me kufijtë standardë është Grade C. Testi pozitiv i strukturës tani përdor kufij eksplicitë **vetëm brenda testit** për të ruajtur provën BOS/retest, ndërsa një test i veçantë kërkon refuzimin e Grade C në konfigurimin standard.

## 5. Filtri i sesionit

`MarketSessions.instrument_tradeable(..., require_session=True)` ruan kontratën e vjetër si default. `main.scan_once` e thërret me `require_session=config.ENABLE_SESSIONS`:
- kur `ENABLE_SESSIONS=True`, orari i sesioneve London/NY/Asia zbatohet;
- kur `ENABLE_SESSIONS=False`, hiqet vetëm kufizimi opsional i sesioneve brenda ditës, **jo** mbyllja javore FX e New York-ut, verifikimi `broker_tradeable is True`, apo ndalimi i instrumenteve jo-FX.

Ky kontroll nuk është verifikim i kalendarit specifik të tregtimit në OANDA; ai mbetet i detyrueshëm në Kategorinë 10.

## 6. Testimi dhe kriteret e pranimit

`tests/test_phase_c_category09.py` shton 21 teste deterministe me fixtures offline: përputhjen e pivot/BOS për LONG/SHORT, skadim konfigurimi, retest i dyfishtë, mutacione të të dhënave të ardhshme pa lookahead (100 raste me seed), metrikat e detyrueshme dhe kufijtë A/B/C/D, ruajtjen e sinjalit ekzistues pozitiv, ndarjen e sesionit nga mbyllja javore dhe verifikimi i kuotës.

Testet shtesë sigurojnë se `setup_id` mbetet identik kur dritarja historike lëviz, se journal-i refuzon një sinjal të dytë nga i njëjti BOS, se hash-i legacy nuk ndryshon, dhe se koordinatori e përdor identitetin e ri në planifikimin e tij offline.

Kategoria pranohet kur ekzekutohet suita e plotë në GitHub Actions, `main.py --check` kalon, sigurohet scope-i i skedarëve dhe merge-i bëhet me SHA të pritur. **Offline PASS nuk garanton sukses të strategjisë ose sjellje identike nga data-feed real**; këto verifikohen në Kategoritë 10–13.
