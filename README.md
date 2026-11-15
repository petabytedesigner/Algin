# Algin — development baseline: Categories 1 and 2

Categories 1 and 2 implement the foundation, read-only broker contracts and
pure risk-based trade preparation. The active entrypoint remains **OBSERVE ONLY**.
No order placement, amendment or closing is enabled. A trade plan is not a
broker instruction, and risk computed from a stop does not guarantee the
maximum realized loss in markets with gaps, slippage or extra fees.

## Python 3.10+ setup

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python main.py --check
python -m unittest discover -s tests -v
```

For a read-only OANDA Practice observation, locally export `OANDA_API_TOKEN`
and `OANDA_ACCOUNT_ID`, then `python main.py --once`.
Secrets are never stored in Git. `.env.example` is documentation only.

## Category 2 risk policy (provisional)

Grade A 1.0%, Grade B 0.5%; total reserved exposure 1.0% of conservative
NAV/balance; at most 3 open/pending intents and no duplicate/hedged
instrument. Stop 1.5×ATR or at least 3 OANDA pips, TP >= 3R. Margin cap
50% of available margin. Lock new entries after 3 consecutive losses, 2% verified daily realized
loss, or 5% drawdown. A configurable shared-currency risk cap limits
correlated USD/JPY/etc. exposure. Broker-external structure support/resistance
may widen ATR stops; an unverified structure level is never invented. These explicit values are in `config.py` and can be reviewed
before deployment; they are not changed automatically by grade/scoring.

`risk/trade_plan.py` is a pure planning engine with Decimal arithmetic,
LONG/SHORT signed units, broker-defined pip/price/unit precision, conservative
currency-loss conversion and post-quantization revalidation. A plan is allowed
only with a caller-verified exposure reservation book, including unresolved
order outcomes, plus a caller-verified loss history. No real account read
automatically asserts this verification.

`broker/risk_data.py` reads OANDA Practice full account details, instrument
metadata and pricing with modern `homeConversions`. Broker GET failures,
missing conversions, unrecognized instrument contract types and inconsistent
trade/order counts reject preparation. No production-ready margin/fill
assumptions are made.

See [Category 2 review](docs/CATEGORY_2_REVIEW.md) for P5/P9–P18, test
expectations and constraints. [Category 1 review](docs/CATEGORY_1_REVIEW.md)
documents the prior integrated baseline.

## Work remaining

Category 3 must implement broker mutation with write-ahead intent and
unambiguous response handling; Category 4 must implement durable
reconciliation, open-position management and authenticated loss statistics.
Category 5 addresses strategy/data quality; Category 6 event reporting,
regression and performance analysis. Real OANDA Practice integration and
Termux behavior remain untested.
