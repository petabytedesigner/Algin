# Faza C — Kategoria 13: Forward observation / Termux / stabiliteti

**Status i provës:** implementimi i supervisor-it dhe i testeve offline nuk është
i njëjtë me **24 orë funksionim real në telefon**, ekzekutim urdhrash,
ose performancë reale të strategjisë. Kjo kategori ofron një rrugë të
kontrolluar **OBSERVE_ONLY**; verifikimi afatgjatë kërkon provë të matur në
Termux dhe autentikim të vlefshëm OANDA Practice. Deri në testin e fundit
të Kategorisë 12, REST API kthente HTTP 401. Asnjë ndryshim këtu nuk e
anashkalon atë gabim.

## Objektivi dhe kontratat ekzistuese

Monitoro **procesin real** `python main.py`, i cili mban
`AccountReconciler` dhe lease-in e account-it gjatë gjithë jetës së
procesit. Ai lexon OANDA Practice, bën reconciliation, vëzhgon sinjalet
M15 dhe regjistron `Heartbeat` / `EventLog`. `main.py` mbetet
`OBSERVE_ONLY`, pa porosi ose ndryshime SL/TP. Supervisor-i është
**proces tjetër**, nuk futet në kodin e strategjisë, nuk ndryshon riskun
dhe nuk përpiqet të rikuperojë porosi.

Kontrata të ruajtura:
- `config.ORDER_WRITES_ENABLED is False`
- `broker.practice_transport._PRACTICE_WRITES_RELEASED is False`
- `config.OANDA_URL == https://api-fxpractice.oanda.com/v3`
- Account ID i saktë i llogarisë demo të autorizuar:
  `101-001-37674492-003` me monedhë USD.
- Nuk ka `POST/PUT/PATCH/DELETE` në supervisor; API thirret
  vetëm përmes `main.py` ekzistues (i bllokuar për mutacione).
- Nuk ka auto-retry të porosive, auto-respawn të procesit apo
  vazhdim në mënyrë optimiste pas `401`, timeout, `UNKNOWN`,
  state të korruptuar, audit failure ose heartbeat të vjetër.
- `BROKER_UNAVAILABLE`, `RECONCILIATION_BLOCKED`,
  `UNRECONCILED_EXPOSURE`, `AUDIT_FAILURE` dhe statuse
  të tjera `BLOCKED` e ndalin supervisor-in. `SESSION_BLOCK`
  regjistrohet vetëm si **GATE**, jo si hyrje e miratuar.
- Ora e heartbeat-it kontrollohet në UTC, me moshë maksimale
  të konfigurueshme (default 180 s) dhe me **PID-in e procesit
  të fëmijës**; një heartbeat nga sesioni i kaluar nuk llogaritet.

## Skedarët e rinj dhe roli i tyre

| Skedari | Përgjegjësia |
|---|---|
| `operations/forward_supervisor.py` | Niset në foreground; kontrollon policy/Practice, ngarkon kredencialet ekzistuese, nis `main.py` si subprocess, kontrollon PID + heartbeat, ndalon në gabim |
| `operations/forward_journal.py` | Regjistron START/SAMPLE/GATE/BLOCKED/STOP në JSONL me SHA-256 të lidhur, `fsync`, skedarë ekskluzivë dhe detektim të tail-it të prerë |
| `operations/forward_performance.py` | Raporton vetëm P/L të **mbylljeve me provenance të plotë** nga `LifecycleStore` dhe `financial_ledger`; refuzon të numërojë tregti të papajtuara |
| `.github/workflows/category13.yml` | Testet offline, compileall, CLI check, kontroll portash dhe stres sintetik 1500 ngjarjesh |
| `tests/test_phase_c_category13_*.py` | Regresion për ndërprerjet, duplicate instances, corrupt files, rezultatin `UNKNOWN`, foreign PID dhe statistikë të paverifikuar |

Nuk ndryshohen `main.py`, `config.py`, `broker/practice_transport.py`,
modulet e strategjisë, të riskut, apo skedarët e kredencialeve.

## Përdorimi në Termux

Instalimi fillestar (nëse nuk është bërë tashmë):

```sh
pkg update
pkg install python git tmux
git clone https://github.com/petabytedesigner/Algin.git
cd Algin
python -m pip install -r requirements.txt
```

Për një instalim ekzistues, kontrollo ndryshimet lokale para
`git pull --ff-only`; mos bëj `reset --hard` mbi state/journal
e llogarisë.

Verifikimi pa rrjet:

```sh
python -m operations.forward_supervisor --check
python -m unittest discover -s tests -p 'test_phase_c_category13_*.py' -v
```

Sipas kërkesës së pronarit, kredencialet demo merren automatikisht nga
`.env.example` ekzistues **vetëm në komandën `--run`** nëse
`OANDA_API_TOKEN` dhe `OANDA_ACCOUNT_ID` nuk janë në environment.
Skedari analizohet si tekst i thjeshtë (vetëm dy çelësa të pranuar), **nuk
ekzekutohet/shell-source**. Token-i nuk printohet dhe dërgohet te
`main.py` vetëm si variabël mjedisi. Një environment gjysmë i
konfiguruar ose llogari tjetër bllokohet.

Një seancë e kufizuar për provën fillestare:

```sh
python -m operations.forward_supervisor --run --samples 4 --interval 30
python -m operations.forward_supervisor --report
```

Ky test kryen rreth katër vrojtime me interval 30 s, nëse procesi
nuk bllokohet më parë. `--samples 4` nuk nënkupton katër
tregti dhe **nuk provon** 24/7.

Për seancën e monitorimit në foreground, pas konfirmimit të
autentikimit, mund të përdoret:

```sh
termux-wake-lock
tmux new -s algin-forward
python -m operations.forward_supervisor --run
```

`Ctrl+C` për ndalim të kontrolluar; `tmux` mund të ndahet nga sesioni
pa ndaluar procesin. Në sesion tjetër:

```sh
cd Algin
python -m operations.forward_supervisor --report
```

Pas ndalimit të monitorimit dhe `tmux`, kur nuk nevojitet më:

```sh
termux-wake-unlock
```

Wake lock ndihmon të mos pezullohet procesi kur ekrani fiket, por
rrit konsumimin e baterisë dhe **nuk garanton** disponueshmëri 24/7:
Android mund të ndërpresë Termux ose të kufizojë rrjetin për
battery optimization, mbinxehje dhe mungesë memorie. Duhet provë
reale mbi pajisjen dhe, nëse është e nevojshme, konfigurim
i lejeve të baterisë në Android. Referencat:
[Termux wake-lock](https://github.com/termux/termux-tools/blob/master/doc/termux.1.md.in),
[Termux battery optimizations](https://github.com/termux/termux-app/wiki/RUN_COMMAND-Intent).

## Monitorimi dhe raportet

`ForwardSupervisor` krijon një lock të vetëm lokal
`.local/forward-supervisor.lock`. Një instancë e dytë bllokohet.
Lock-u jetim pas crash-it **nuk fshihet automatikisht**: duhet kontrolluar
procesi dhe gjendja broker/journal përpara rikuperimit nga operatori.
Lease-in e llogarisë e mban ende `main.py` ekzistues.

- `.local/forward-runs/<run-id>.jsonl`: prova të fsynced të
  eventeve, me hash chain/sequence. Nuk përmban API token ose
  përmbajtje të papërpunuar brokeri.
- `.local/forward-logs/<run-id>.log`: output lokal i procesit
  `main.py`, file mode 0600; kufi kontrolli 16 MiB.
- `--report`: verifikon integritetin e runs, raporton
  `healthy_samples`, `gated_samples`, `blocked_runs`,
  `interrupted_runs` dhe gabimin e fundit.
- Raporti nuk shpall *uptime 24 h* bazuar te disa pulse, as
  ekzekutim tregtie nga ndonjë `SIGNAL_OBSERVED`. Një run
  i ndërprerë pa STOP/BLOCKED shënohet `UNVERIFIED_INTERRUPTED_RUNS`.
- Statistikat e tregtimit lexohen **vetëm** nga lifecycle state
  i verifikuar dhe `financial_ledger` në disk. Pa opening/final fill
  provenance, pa komision/financim të qartë ose me `UNKNOWN`,
  `win_rate_pct`, `expectancy`, `net_pl_home` dhe profit factor
  mbeten `None`, jo zero. `realized_only_drawdown_home`
  është vetëm rënia nga kulmi e **P/L të realizuar sipas tregtive të
  mbyllura**, jo drawdown i llogarisë.
- **Drawdown total NAV, disponueshmëria reale 24/7, statistikat për
  seanca, kostot e tregut, rollover dhe forward edge i strategjisë
  mbeten të paverifikuara** pa histori NAV/live telemetry dhe rezultatet
  e pajtuara të brokerit.

## Protokolli i incidentit

- `HTTP 401/403`: ndalo; nuk ka restart loop, nuk ka riciklim
  automatik token-i. Kontrollo endpoint Practice dhe autorizimin
  me kërkesa GET të kontrolluara.
- `UNKNOWN`/timeout/5xx: dështim i ciklit; verifiko cursor,
  urdhrat, tregtitë dhe journal, **mos ridërgo urdhra**.
- Heartbeat `STALE`, i dëmtuar ose me PID tjetër: ndalo pa
  restart dhe ruaj provat e disk-ut.
- `SESSION_BLOCK`: log `GATE`, jo porosi e autorizuar.
- Android e mbylli procesin: `report` mund të gjejë run të
  papërfunduar. Monitorim **i jashtëm** është i nevojshëm për të
  vërejtur që telefoni ose Termux janë plotësisht offline.
- CSV audit i dëmtuar, lock i paqartë, state me cursor të humbur:
  mos kryej `rm -rf .local`; nevojitet rikuperim i kontrolluar.

## Kriteret e pranimit për Kategorinë 13

| Kriteri | Provë që kërkohet |
|---|---|
| Testet offline, 1500 sample synthetic | GitHub Actions PASS |
| Zero broker mutations, production guards OFF | CLI+CI invariant, audit file scope |
| Singleton, watchdog/PID, timeout & orphan lock | Offline adversarial tests |
| Forward test OANDA Practice | Sesion i autorizuar GET me telemetry real dhe pa `401` |
| Termux 24 h+ i qëndrueshëm | Journal real on-device, timestamps, pa boshllëqe, uptime i matur nga monitor i jashtëm |
| BE/partial/full-close reale e automatizuar | Nuk certifikohet nga OBSERVE_ONLY; kërkon release veçmas |
| Win-rate/P/L neto/drawdown real | Ledger financiar i plotë, NAV series, trade outcomes me provenance brokeri |
| Përfundim | **Code/CI ready; operacionalisht i hapur** derisa provat reale të plotësohen |

**Kujdes:** Kategoria 12 ka mbetur me mbyllje manuale të raportuar,
por jo të vërtetuar nga API pas përgjigjeve `401`; monitorimi
i kategorisë 13 nuk e mbyll automatikisht atë detyrë. Nuk mund të
deklarojmë botin gati për autotrading ose 24/7 pa prova operative.
