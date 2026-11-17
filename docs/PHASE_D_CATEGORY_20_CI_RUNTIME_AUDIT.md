# Faza D — Kategoria 20: auditimi i CI, varësive dhe përputhshmërisë Termux

## Rezultatet dhe kufijtë e auditimit

Kjo kategori kontrollon ndërfaqet operative të Algin pa prekur strategjinë,
riskun, pozicionet OANDA dhe skedarët e kredencialeve. Gjendja e një
workflow me rezultat `success` **nuk provohet** si autentikim i brokerit
nëse workflow ka ekzekutuar vetëm teste offline.

### Problemet e identifikuara

1. Workflows përdornin referenca lëvizëse `checkout@v4` dhe
   `setup-python@v5`, që nuk jepnin identitet të pandryshueshëm
   të kodit të GitHub Actions.
2. CI kryesore përdorte vetëm Python 3.11; mungonte testi i
   kompatibilitetit sintaksor dhe regresionit me versione të tjera
   që mund të instalohen në Termux.
3. Job-i i kontrollit të OANDA Practice mund të përfundonte me
   `success` pa pasur provë të një `GET` të autentikuar.
4. `requirements.txt` ka intervale versionesh dhe jo një lockfile
   me hash-e; integriteti i varësive transitore nuk është
   i riprodhueshëm në nivel bit-per-bit.
5. Raportimi mund të ngatërrohej nëse testet offline, një
   `--check` i Termux dhe një seancë 24-orëshe reale trajtoheshin
   si prova ekuivalente.

### Ndryshimet

- Referencat e të gjitha workflows ekzistuese `actions/checkout`
  dhe `actions/setup-python` lidhen me **SHA 40-shifrore**
  të rezolvuara nga tag-et zyrtare:
  - checkout v4: `11d5960a326750d5838078e36cf38b85af677262`
  - setup-python v5: `a26af69be951a213d495a4c3e4e4022e16d87065`
  Kontrolli ruan versionet madhore të mëparshme; asnjë action
  e re me privilegje shkrimi nuk është shtuar.
- `.github/workflows/category20.yml` kryen CI **offline** në
  Python `3.11`, `3.12`, `3.13`, `3.14`, mbi runner Linux
  të GitHub. Për secilin version: instalon `requirements.txt`,
  kryen `pip check`, `compileall`, të gjitha testet,
  `runtime_audit`, kontrollet offline të Practice/forward dhe
  `main.py --check`.
- `operations/runtime_audit.py` raporton JSON: versionin Python,
  intervalet e kërkuara për `requests` dhe `tzdata`,
  ruajtjen e portave të brokerit, referencat e pandryshueshme
  të GitHub Actions, lejet minimale të workflows dhe
  kufijtë e provës. Analiza e YAML-it është **kontroll statik
  tekstual**, jo analizues i plotë YAML dhe jo garanci sigurie.
- `operations/practice_ci_probe.py` lexon **skedarin ekzistues
  `.env.example`** me parserin e sigurt ekzistues,
  `account_environment`. Nuk e ndryshon, nuk e ekzekuton si
  shell, nuk printon token-in dhe nuk bën retry.
  `--check` është offline; `--run` kryen vetëm GET të
  autentikuara në OANDA Practice dhe kthen kod jo-zero kur
  nuk ka evidencë.
- Në `category12.yml`, ky kontroll autentikimi është
  **vetëm për nisje të shprehur** `workflow_dispatch` nga
  `main`. Kështu, një build normal ose PR mund të kalojë
  vetëm verifikimet offline, pa u paraqitur si provë lidhjeje
  reale me brokerin. Në një ekzekutim manual, një GET i
  dështuar shënohet si failure, jo success i heshtur.
- Testet e Kategorisë 20 verifikojnë mungesën dhe
  papajtueshmërinë e varësive, devijimin nga action pins,
  lejet e rritura, ndalimin e PR-së për brokerin,
  rrezikun e ekspozimit të token-it, falsifikimin e
  provës së rrjetit dhe ndërfaqen e parserit ekzistues.

### Komandat lokale — Termux

Nga rrënja e repository-t:

```sh
python -m pip install -r requirements.txt
python -m pip check
python -m unittest discover -s tests -p 'test_phase_d_category20.py' -v
python -m operations.runtime_audit
python -m operations.practice_ci_probe --check
python -m operations.forward_supervisor --check
```

Vetëm për një kontroll real **GET-only** të kërkuar
nga operatori me llogarinë ekzistuese Practice:

```sh
python -m operations.practice_ci_probe --run
```

Kjo komandë lexon automatikisht `.env.example` të përdoruesit,
por nuk garanton aksesin nëse OANDA refuzon autentikimin.
Nuk hap dhe nuk mbyll tregti.

### Përkufizimi i statusit

| Provë | Çfarë vërteton |
|---|---|
| `OFFLINE_CONTRACT_PASS` | Versionet, importet, kontrolli statik i CI, portat, paketat dhe testet e provuara |
| `AUTHENTICATED_PRACTICE_VERIFIED` | Vetëm një ekzekutim i suksesshëm **real** i `--run` GET-only (jo testi sintetik) |
| `NOT_TESTED_BY_THIS_AUDIT` | Mungon provë OANDA/FMP live në këtë ekzekutim |
| `NOT_TESTED_BY_GITHUB_HOSTED_RUNNER` | Runner Linux nuk është një pajisje reale Android/Termux |
| `NOT_MEASURED_ON_DEVICE` | Nuk ka vëzhgim 24-orësh të llogaritur nga pajisja |
| `NOT_VERIFIED` | Nuk mund të nxirret P/L, win rate ose edge nga kontrollet e infrastrukturës |

### Kufizime që mbeten pas CI

- Intervalet e versioneve të `requirements.txt` nuk janë
  të kyçura me hash-e. `pip check` vërteton konsistencën e
  paketave të instaluara, **jo** riprodhueshmëri kriptografike
  të të gjithë zinxhirit të varësive.
- Testimi në GitHub-hosted Ubuntu nuk provon paketimin
  specifik të Termux, proceset Android, kufizimet e baterisë
  ose qëndrueshmërinë 24/7. Kërkohet test real në pajisje.
- Kjo kategori nuk certifikon furnizuesin FMP me të dhëna live,
  account reconciliation real pa një GET të autentikuar,
  autonominë e tregtimit ose fitimprurjen.
- Workflow manual OANDA është i kufizuar në `main` dhe
  kryen vetëm GET. Nuk aktivizohen rrugët e broker writes.
- Përdorimi i credentials demo në skedarin ekzistues është
  ruajtur siç është kërkuar: **nuk ka modifikim, migrim,
  fshirje, printim apo ndryshim të ndonjë credential-i**.

## Kriteret e pranimit

1. Matrica Python 3.11–3.14: testet dhe `pip check` PASS.
2. Kontrolli i pin-eve dhe i policy-ve offline: PASS.
3. Prova e vetme e autentikimit është një `--run`
   real dhe i suksesshëm; përndryshe qëndron `UNVERIFIED`.
4. Nuk ka broker writes, retry automatik, ndryshim të
   strategy/risk, account state ose credentials.
5. Integrimi bëhet vetëm pas CI të plotë në PR dhe
   verifikimit pas merge.
