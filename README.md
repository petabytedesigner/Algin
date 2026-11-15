# Algin — OANDA Practice trading-bot development

Algin is the original small EMA20/EMA50 + ATR + market-structure + break/retest bot. This branch redesigns its interfaces, broker safety boundary, state recovery and offline verification. **It is an experimental development branch, not production-ready. No profit is guaranteed.**

## Repo boundaries
- Algin: the only trading-bot repository modified.
- logTX: historical evidence and ideas for test scenarios, **not** a proof that Algin has passed those tests. Some logTX records come from another project.
- Alvin: entirely out of scope; never access or modify it.

## Safety defaults
- API is pinned to **OANDA Practice** at https://api-fxpractice.oanda.com/v3.
- The default command runs in **read-only/observe mode**: no order POST/PUT requests.
- Actual OANDA Practice writes require **both** the CLI flag \`--submit-practice\` and the exact explicit environment opt-in \`ALGIN_PRACTICE_SUBMIT=I_ACCEPT_PRACTICE_ORDERS\`.
- Live OANDA trading is not implemented or authorized.
- Never commit credentials. Historic generated state, heartbeat, CSV and bytecode files are preserved in the baseline Git commit but excluded from this development branch.
- A lost POST/PUT reply is **UNKNOWN**, not automatically a rejection. No blind retry. New entry is blocked pending reconciliation.
- Unknown positions, pending orders, impaired protection or missing news data block entries. Positions owned by this bot are managed regardless of entry session restrictions.

## Installation (Python 3.11 or higher)

\`\`\`sh
git clone https://github.com/petabytedesigner/Algin.git
cd Algin
git switch development/6-category-hardening-2026-11-15
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
\`\`\`

Set the **Practice** credentials locally in environment variables \`OANDA_API_TOKEN\` and \`OANDA_ACCOUNT_ID\`. Do not publish token values. The included \`.env.example\` is a list of names, not an auto-loading \`.env\` implementation.

To read the broker state without placing orders:

\`\`\`sh
python main.py --once
\`\`\`

Default \`ALGIN_NEWS_MODE=UNCONFIGURED_BLOCK\` blocks new entries even when the account works. A trusted source may supply an atomic JSON file and set \`ALGIN_NEWS_MODE=ENABLED_JSON\`, \`ALGIN_NEWS_JSON_PATH=/path/events.json\`. Format:

\`\`\`json
{"generated_at":"2026-11-15T08:00:00Z","events":[{"time":"2026-11-15T08:30:00Z","impact":"HIGH","currency":"USD"}]}
\`\`\`

The file must be refreshed within the configured TTL. Alternatively, \`ALGIN_NEWS_MODE=DISABLED_ACKNOWLEDGED\` is an **explicit acceptance of trading without economic-news protection**; this does not add a data source or prove readiness.

## Development tests

\`\`\`sh
python -m compileall -q main.py config.py broker data execution risk runtime storage strategy utils analytics
python -m unittest discover -s tests -v
\`\`\`

GitHub Actions runs both automatically. All tests use local fixtures; passing them does **not** prove that a real order or partial-close lifecycle succeeds in OANDA Practice.

## Reporting

\`\`\`sh
python -m analytics.performance /path/trade_ledger.json --starting-equity 10000
\`\`\`

The ledger is a JSON list with \`instrument\`, \`gross_price_pl_home\` and optional \`spread_cost_home\`, \`commission_home\`, \`slippage_home\`, \`financing_home\`. Costs are explicit: do not subtract spread twice from P/L already based on executed bid/ask fills.

## Developer references
- [Six-category development and acceptance plan](docs/DEVELOPMENT_PLAN.md)
- [OANDA contract notes](docs/OANDA_CONTRACTS.md)
- [Readiness and limitations](docs/STATUS.md)
- [Official OANDA REST v20 docs](https://developer.oanda.com/rest-live-v20/introduction/)
