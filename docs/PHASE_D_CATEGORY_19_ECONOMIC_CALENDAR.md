# Faza D — Kategoria 19: Integrimi i kalendarit ekonomik

## Burimi dhe kontrata

**Financial Modeling Prep (FMP)** është burimi i integruar për kalendarin
ekonomik global, me endpoint-in zyrtar
`https://financialmodelingprep.com/stable/economic-calendar`.

Referenca zyrtare: https://site.financialmodelingprep.com/developer/docs/stable/economics-calendar

Përgjigjja e dokumentuar ka `date` në **UTC**, `currency`,
`impact` (`High`, `Medium`, `Low`) dhe `event`. FMP përmend
kufizime aksesi sipas planit dhe mund të kthejë përgjigje jo të
përdorshme pa abonimin e duhur. **Çelësi OANDA nuk autentikon FMP.**

Kur ekziston një çelës aktiv FMP në variablin e mjedisit
`FMP_API_KEY`, `NewsFilter` mund të përdorë automatikisht këtë
adapter. Nuk nevojitet asnjë ndryshim në `.env.example` të OANDA,
GitHub Actions Secrets, `config.py`, apo në kodin e strategjisë.

Në mungesë të çelësit FMP ose kur autentikimi/abonimi i FMP nuk
mbulon endpoint-in, **NEWS_UNKNOWN mbetet i detyrueshëm**.
Kjo kategori nuk pretendon se çelësi FMP ekziston, apo se është
bërë një test live i autentikuar me FMP.

## Algoritmi

- `NewsFilter` ruan prioritetin e fixture-it statik të testit dhe
  të `NEWS_PROVIDER_URL + NEWS_PROVIDER_HOST` të aprovuar më parë.
  Adapter-i FMP aktivizohet vetëm kur këto nuk janë konfiguruar dhe
  `FMP_API_KEY` është i vlefshëm.
- Endpoint-i është **i fiksuar**, HTTPS, me `GET` dhe pa redirects,
  me timeout 10 s dhe **pa retry**. Çelësi dërgohet sipas
  dokumentacionit FMP me parametrin `apikey`; nuk printohet,
  nuk regjistrohet dhe nuk përfshihet në mesazhet e gabimit.
- Për çdo refresh kërkohet kalendari global i tri ditëve UTC:
  dita aktuale + një ditë para dhe një ditë pas, brenda kufirit
  zyrtar të datave. Përgjigjja jo-200, JSON i dëmtuar, inventar bosh,
  më shumë se 5000 rekorde ose më shumë se 2 MiB: **UNKNOWN**.
- Kontrollohen **të gjitha** rekordet për kohë UTC, monedhë me kod
  tre shkronja, ndikim të njohur dhe titull. Një rekord i pavlefshëm
  nuk hiqet në heshtje: **UNKNOWN**.
- Vetëm pas validimit përfshihen monedhat kryesore FX
  `AUD CAD CHF EUR GBP JPY NZD USD`; rekordet e tjera me monedhë
  të vlefshme nuk ndikojnë instrumentet që Algin analizon.
  Nëse nuk ka asnjë rekord për FX kryesor: **UNKNOWN**.
- Përgjigjja normalizohet në kontratën ekzistuese
  `events / fetched_at / coverage_start / coverage_end / currencies`
  të `NewsFilter`, pa ndryshuar kontratën publike.
- `NEWS_LOCKOUT_MINUTES_BEFORE=30` dhe
  `NEWS_LOCKOUT_MINUTES_AFTER=30` mbeten **të pandryshuara**.
  `HIGH` për njërën nga dy monedhat e instrumentit prodhon lockout.
  `LOW/MEDIUM` nuk e aktivizojnë këtë bllokim.
- Cache-i `NEWS_CACHE_TTL_MINUTES=15` dhe freshness maksimum
  30 minuta mbeten ekzistuesit. Ndryshimi i çelësit FMP ndryshon
  hash-in e identitetit të burimit dhe e zhvlerëson cache-in.
  Nuk ruhet çelësi në identitet, vetëm SHA-256 e tij.
- Në dështim të rifreskimit, cache-i i mëparshëm nuk shërben për të
  shpallur `CLEAR`.

## Kufijtë e provës

Kjo integron **një adapter real API**, por provat automatike janë
tërësisht me përgjigje sintetike HTTP. Në mungesë të aksesit të
autentikuar dhe përgjigjes së suksesshme nga FMP, statusi operacional
mbetet **FMP_LIVE_SOURCE_NOT_VERIFIED** dhe hyrjet mbeten të
bllokuara nga `NEWS_UNKNOWN`.

Fusha `fetched_at` është koha e marrjes nga klienti, **jo**
përditësimi i fundit i eventit në serverin FMP. Mbulimi i monedhave
bazohet në kontratën e endpoint-it global pa filtër shtetesh,
jo në një vërtetim të pavarur rresht-për-rresht të mungesës së
eventeve. Prandaj nuk është garanci absolute e plotësisë së kalendarit.
Nëse nevojitet garanci më e fortë, duhet SLA e burimit dhe monitorim
i pavarur i publikimeve.

**Asnjë** urdhër OANDA, ndryshim stop-loss/take-profit, ndryshim i
parametrave të riskut ose çaktivizim i portave të sigurisë nuk
është i përfshirë.

## Kontrollet

`python -m unittest discover -s tests -p 'test_phase_d_category19.py' -v`

Testet verifikojnë kontratën HTTP (host, interval, pa redirects, pa
retry), kufijtë UTC, `HIGH` në secilën monedhë, rastet `LOW/MEDIUM`,
stale cache, përgjigjet 301/401/429/500, JSON të pavlefshëm,
evente të dyfishta, monedha të panjohura dhe çelësa të munguar.

Integrimi bëhet vetëm pas **CI të gjelbër**, pa pretenduar
testim live me FMP pa autorizimin e vet.
