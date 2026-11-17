# Faza C — Kategoria 11: Replay historik dhe validim statistikor (OFFLINE)

**Gjendja:** dorëzohen një motor replay dhe instrumente vlerësimi që ripërdorin pjesë të kodit aktual. **Nuk ka dataset historik real të lidhur dhe nuk ka backtest empirik të performancës së Algin**. Çdo provë në këtë PR përdor qirinj **sintetikë** dhe, ku kërkohet për të testuar ekzekutimin, injekton sinjal sintetik. `main.py` mbetet `OBSERVE_ONLY`; dy portat `config.ORDER_WRITES_ENABLED=False` dhe `_PRACTICE_WRITES_RELEASED=False` nuk preken.

## Burimi zyrtar OANDA REST v20

- [Candles endpoint](https://developer.oanda.com/rest-live-v20/instrument-ep/) lejon `price=MBA` (mid, bid, ask) dhe `granularity=M15`.
- [Candlestick definitions](https://developer.oanda.com/rest-live-v20/instrument-df/): `time` është fillimi i intervalit; `complete` tregon që mbarimi nuk është në të ardhmen. `mid`, `bid`, `ask` kanë `o/h/l/c`.
- [Pricing components](https://developer.oanda.com/rest-live-v20/primitives-df/): `M`, `B` dhe `A` përfshijnë midpoint dhe anët ekzekutuese. **Bid/ask OHLC nuk garanton renditjen e tick-eve brenda qiririt**, ndaj mbështetet rregulli konservativ SL-first.

## 1. Çfarë ripërdoret nga runtime

`analytics/strategy_replay.py` nuk krijon strategji të re. Për çdo qiri të mbyllur M15:
- `data.candle_validation.parse_oanda_candles` dhe `validate_candles` normalizojnë dhe validojnë midpoint;
- `Strategy` përdor të njëjtat EMA20/EMA50/ATR, breakout/retest, BOS dhe grade A/B si `main.py` (dritare deri 200 qirinj);
- `MarketSessions.instrument_tradeable` dhe `NewsFilter.block_trading` zbatojnë kontrollet e tregut/kalendarit në kohën historike të vendimit, duke mos kërkuar asnjë burim në internet;
- `TradePlanner(build_risk_policy())` përdor parametrat aktualë të sizing/SL/TP/risk;
- `parse_market_quote` dhe `build_market_order` zbatojnë spread, precision, limitin `priceBound` dhe shrink-only sizing sipas politikës aktuale;
- `analytics.conservative_backtest.stop_first_bar` trajton SL/TP në të njëjtin qiri si **SL**; `net_result` bën kontabilitet vetëm me përfshirje eksplicite të kostove;
- `analytics.performance.report` prodhon statistika të përshkruara si `RESEARCH_ONLY`, jo fitim brokeri.

Sinjali analizohet **pas mbylljes** së qiririt i; hyrja më e hershme është **open i qiririt i+1**. Dalja LONG kryhet nga historiku **bid** dhe SHORT nga **ask**. Nuk përdoret `high/low` e qiririt të ardhshëm për të krijuar ose klasifikuar sinjalin e mëparshëm.

## 2. Modeli i kostove

`ReplayAssumptions` kërkon kapitalin, monedhën e llogarisë dhe komponentë **të deklaruar** të kostos: slippage hyrje/dalje në pip, komision home për njësi për secilën anë, financim home për njësi për bar M15, dhe politikën `HOLD` ose `BE_PARTIAL`.

- Hyrja ekzekutohet në çmimin më të keq të lejuar nga `priceBound`; kjo është supozim konservativ, **jo simulim i probabilitetit të mbushjes**.
- Spread është **brenda** diferencës midis `ask` të hyrjes dhe `bid` të daljes LONG (anasjelltas SHORT); nuk zbritet përsëri si komision. Slippage adverse i daljes aplikohet në çmimin e realizuar. Komisioni (hapje+dalje) dhe financimi zbriten një herë.
- Kur hapja e një bari kalon përtej stop-it ekzistues, dalja penalizohet në open më të keq dhe jo në stop-in ideal. Kur SL dhe TP preken në të njëjtin qiri pa tick path, vlen **stop-first**, edhe nëse supozimi është më konservativ se renditja reale.
- Faktorët `loss_to_home`, `gain_to_home`, `base_value_to_home` dhe `broker_tradeable` duhen dhënë **për secilin bar** si prova/hipoteza historike. Nuk nxirret automatikisht kursi nga çmimi mid. Shtresa nuk provon vlefshmërinë e burimit të këtyre faktorëve.

`financing_home_per_unit_bar` është skenar numerik, jo shkëmbim i vërtetë/financing OANDA. As spread, taksat, slippage, komisionet dhe financimi nuk pretendojnë kalibrim ndaj llogarisë reale.

## 3. BE dhe partial / kufijtë e paritetit

`BE_PARTIAL` ripërdor `TradeRecord.break_even_target` dhe `partial_close_units` me pragun ekzistues **1R** dhe fraction **50%**. Trigger merret vetëm nga close i një bari të mbyllur. Veprimi i synuar mund të materializohet vetëm në **open-in pasues**, pas kontrollit të gap/SL; nëse kushti 1R nuk vlen më, nuk simulohet ndryshim. Kjo është hipotezë për latency, jo emulim i vërtetë i request/response apo journal.

Modelet `HOLD` dhe `BE_PARTIAL` llogariten si rrugë të pavarura — ndikimi i partial në risk, njësi, P/L dhe sekuencën e tregtimeve **nuk** nxirret duke transformuar thjesht fitimin e regjistruar.

**Jo-paritetet operative të njohura:** mbetet vetëm një pozicion i hapur për instrument/ekzekutim replay; përdoret NAV e realizuar, jo mark-to-market intrabar; margin është input sintetik; nuk ka liquidity tiers, order-book depth, volume constraints, limit order queue, requote, partial fills në hyrje, shpenzime reale rollover/swap, evente asinkrone manuale apo reconciler/journal brokeri. Këto bëjnë që replay të jetë **production-contract-aligned**, por **jo production-equivalent**. Ndryshimi i parametrave në këtë modul nuk i ndryshon limitet e kodit operacional.

## 4. Protokolli i ndarjes kohore

`analytics/walk_forward.py` jep `holdout` me një ndarje UTC të vendosur paraprakisht; përdor **entry time** për testin jashtë kampionit. Tregtitë e hapura para ndarjes dhe të mbyllura më pas përjashtohen (`purged_cross_boundary`), jo numërohen në të dyja partitë. `cost_sensitivity` ekzekuton nga fillimi secilin skenar të përcaktuar shprehimisht; nuk bën kërkim parametrash ose tuning mbi holdout.

Raportohen numri i tregtimeve, P/L neto, drawdown i realizuar, win-rate, expectancy, profit factor, net-R, ndarja A/B dhe instrumenti. Intervali Wilson 95% për win-rate shfaqet **vetëm kur n ≥ 30**; edhe atëherë `statistically_validated_edge=False`, sepse indepenca e tregtimeve, përzgjedhja e instrumenteve, qëndrueshmëria e strategjisë dhe kalibrimi real i kostove nuk janë vërtetuar.

**Një rezultat i mirë në kampion nuk është arsye për të rregulluar parametrat dhe për të ridefinuar holdout-in.** Për analiza serioze duhen walk-forward me periudha të ngrira, instrumente të shumëfishta, regjime të ndryshme tregu, auditim i të dhënave dhe forward test.

## 5. Testet dhe kriteret e pranimit

`tests/test_phase_c_category11.py` ka 24 teste offline për:
- kërkesën e mid/bid/ask, sinjale mbi historik të mbyllur, data të pandryshuara nga qirinjtë e ardhshëm;
- ndalim të inputeve që mungojnë, qirinjtë e paplotë, kurset e konvertimit të panjohur, spread/precision/gap dhe news provider të munguar;
- skenarë sintetike TP, SL-first, komision/slippage, BE/partial 1R, pozicion të pambyllur që **nuk** shndërrohet në fitim;
- ndarje train/test, purge të tregtisë që kalon kufirin, A/B dhe instrument, sample-size dhe skenarë kosto të llogaritur në mënyrë të pavarur.

`test_phase_c_category11.py` përdor një `ScriptedStrategy` **vetëm në testet e ekzekutimit** për të prodhuar raste të kontrollueshme. Prova e veçantë pa patch thërret `Strategy` reale; asnjë nga këto rezultate nuk është evidencë profitabiliteti.

**Kriteri i merge:** regresioni i plotë ekzistues + testet e reja PASS, CI e pandryshuar për portat e shkrimit, kontroll i skedarëve/SHA dhe rikonfirmim pas merge. Nuk lejohet promovim i analizës sintetike si rezultat historik i verifikuar.
