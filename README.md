# Stock assistant

A personal, multi-agent pipeline that turns market data into a stock portfolio with weights and a
trade list for you to review. It follows [PLAN.md](PLAN.md) (stages and agent patterns) and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (Python, LangGraph, Claude in three tiers, Postgres
journal). Stocks only. **Paper trading only: nothing is ever sent to a broker.**

## Defaults chosen for the open questions

The architecture doc left three questions open. Until you decide otherwise, the code uses:

| Question | Default | Where to change it |
|----------|---------|--------------------|
| Data provider | Free Yahoo Finance data through `yfinance`, behind the `MarketDataProvider` interface in `src/stockassistant/data/provider.py`. An offline `synthetic` provider is included for tests and demos. | `[data] provider` in `config/default.toml`; add a class implementing the protocol for a paid provider |
| Rebalance frequency | Monthly | `[run] rebalance_frequency` (`monthly` or `weekly`) |
| Starting universe | All S&P 500 constituents, from `config/universe/sp500.csv` (ticker, name, GICS sector, date added). Stage 1 filters them by price, liquidity, market cap and history. Refresh the list with `uv run stockassistant update-universe`. | `[data] universe_file` and `[data] tickers` (extra names) |
| Java dashboard | None | Not started; the journal schema in `db/01-schema.sql` is ready for one later |

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra postgres          # install dependencies (the extra adds the Postgres journal driver)

# Offline: synthetic data and the rule-based stand-in for the LLM. No API key, no network.
uv run stockassistant --provider synthetic run --date 2024-06-28 --llm offline

# Real run: Yahoo Finance data and Claude. The key is read from the environment only.
export ANTHROPIC_API_KEY=...       # never commit it; see .env.example
uv run stockassistant run          # today's date by default
```

`run` prints the trade plan and a run id. After you place the trades yourself, record them so the
next run and the monitor start from your new holdings:

```bash
uv run stockassistant approve <run-id>
uv run stockassistant monitor                    # performance, drift and alerts since the last plan
uv run stockassistant monitor --summarize        # adds a short plain-language summary from Claude
```

Pass `--holdings holdings.json` (`{"cash": 50000, "shares": {"AAPL": 10}}`) to start from an existing portfolio.

### Backtests

```bash
# Quantitative baseline (stages 1, 2, 3, 5 optimizer, 8 hard limits, 9): no LLM calls.
uv run stockassistant backtest --start 2023-01-01 --end 2025-12-31 --output runs/bt.json

# Whole agent pipeline on a short window. With Claude this costs real money (about $9 per rebalance
# date by the estimate in docs/ARCHITECTURE.md); use --llm offline to check the machinery first.
uv run stockassistant backtest --start 2025-01-01 --end 2025-06-30 --mode full --llm offline
```

Full-mode backtests also report each analyst lens's hit rate and the judge's verdict hit rate.
Read the caveats in the output: survivorship bias, no point-in-time fundamentals from `yfinance`,
and LLM look-ahead on historical dates.

### Docker Compose

```bash
cp .env.example .env               # fill in ANTHROPIC_API_KEY and the database passwords
docker compose up -d db            # Postgres 17 + pgvector, schema from db/
docker compose run --rm app run    # the pipeline, journaling to Postgres
```

## Development

```bash
uv run pytest                                  # all tests (the LLM is mocked; no network needed)
uv run pytest tests/test_risk.py               # one file
uv run pytest tests/test_risk.py::test_review_can_only_cut   # one test
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

## Layout

```
config/universe/sp500.csv    the starting universe (S&P 500 constituents with sectors and dates added)
config/default.toml          all tunable values: universe rules, alpha, tilts, debate rules, risk limits, costs, models
db/                          Postgres schema (journal, knowledge base) and the insert/select-only app role
src/stockassistant/
  schemas.py                 typed, versioned, frozen contracts between stages, and the LLM output schemas
  config.py                  typed settings
  graph.py                   LangGraph wiring of stages 1 to 9, with the risk retry loop
  cli.py                     run, approve, backtest, monitor
  quant.py                   feature and portfolio-statistics tools
  guardrails.py              injection screen, citation check, weight checks
  data/                      provider protocol, yfinance, synthetic, backtest cache
  llm/                       Anthropic client, offline rule-based client, SKILL.md loader
  agents/<role>/SKILL.md     one prompt file per agent role, with its model and effort
  memory/journal.py          append-only decision journal (file, in-memory, Postgres)
  stages/                    one module per stage, each a plain run() function
tests/                       unit tests per stage and end-to-end tests with a mocked LLM
```

## Stages and agents

| # | Stage | Module | Kind | Model |
|---|-------|--------|------|-------|
| 1 | Data and universe | `stages/data_universe.py` | Code, quality guardrails | none |
| 2 | Regime | `stages/regime.py` | Rules (trend, volatility percentile, breadth) | none |
| 3 | Alpha screen | `stages/alpha.py` | Factors + walk-forward ridge with purge and embargo | none |
| 4 | Analyst team | `stages/analysts.py` | Fundamental, technical, macro (Sonnet 5.5), sentiment (Haiku 5.5), validator (Sonnet 5.5) | per SKILL.md |
| 5 | Proposal | `stages/portfolio_manager.py` | Inverse-vol baseline + tilts with self-consistency | Opus 5.5 |
| 6 | Debate | `stages/debate.py` | Bull and bear in parallel, devil's advocate, judge | Sonnet 5.5, judge Opus 5.5 |
| 7 | Adjustment | `stages/adjustment.py` | Bounded rules from the verdicts | none |
| 8 | Risk | `stages/risk.py` | Hard limits in code + review that can only cut; one retry | Opus 5.5 |
| 9 | Execution plan | `stages/execution.py` | Cost model, no-trade band, turnover cap | none |
| 10 | Backtest and evaluation | `stages/backtest.py` | Walk-forward replay; agent hit rates | as configured |
| 11 | Monitoring and journal | `stages/monitor.py`, `memory/journal.py` | Mark to market, drift, alerts | optional Sonnet summary |

Rules that hold across the agents, enforced in code:

- Numbers come from code. Each analyst gets an evidence pack of tool-computed metrics and source ids;
  the model returns a view, a thesis, the metric names and sources it used, and the risks it sees.
- Citations are checked. Unknown metrics, sources or report ids are dropped; uncited tilts become zero;
  uncited debate arguments are discarded.
- News text is screened for injected instructions, and every payload is marked as data, not instructions.
- Every LLM output is a Pydantic schema enforced with structured outputs. No `temperature` is sent
  (the current Claude API rejects it); effort is set per role; refusal fallbacks are on.
- The risk review can only scale positions down or veto them. Limits are frozen config.
- Each run has a call budget (`[llm] max_calls_per_run`), and token usage per role is journaled.
