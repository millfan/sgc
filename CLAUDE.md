# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

This project builds a personal stock trading assistant. It has two phases:

1. **Learn** from two books and their companion code:
   - *Machine Learning for Algorithmic Trading* (Third Edition), abbreviated `ml4t`
   - *Build AI Agents for Finance*, abbreviated `baa4f`
2. **Build** a multi-agent pipeline that turns market data into a stock portfolio with weights.

## Scope: stocks only

The assistant deals only with individual stocks. Do not add support for options, futures, bonds, crypto, FX, funds, or other instruments, and do not branch into other finance areas such as personal budgeting, tax, or credit. When adapting book examples that cover other asset classes, keep only the parts that apply to stocks.

## Planned architecture

The pipeline is a sequence of stages, and **each stage is its own agent** with a single responsibility. A stage takes earlier stages' typed outputs as input. The full stage list, with each stage's agent, inputs and outputs, book references, and the milestones, is in [PLAN.md](PLAN.md). Keep PLAN.md current when stages change. The stages, in run order:

1. **Data and universe**: fetch prices and fundamentals, check quality, filter to a tradable stock universe.
2. **Market regime assessment**: classify the current market state, for example trend, volatility, or risk-on versus risk-off.
3. **Signal and alpha screen**: score stocks with ML factor and return forecasts under leakage-safe cross-validation, and pick a short list.
4. **Analyst team**: four analysts (fundamental, technical, sentiment, macro) write cited reports in parallel. The reports are one shared input for the portfolio manager, the debaters, and the risk officer.
5. **Stock and weight proposal**: the portfolio manager combines the analyst reports, scores, and regime into candidate stocks and initial weights.
6. **Adversarial debate**: bull, bear, devil's advocate, and judge argue over the proposal, citing the analyst reports, to expose weak picks and hidden risks.
7. **Weight adjustment**: revise the weights based on the debate's outcome.
8. **Risk check**: enforce hard limits on position size, sector exposure, volatility, drawdown, and liquidity, plus a review of the risks the analysts flagged.
9. **Cost-aware execution plan**: turn approved weights into rebalancing trades with transaction costs and turnover limits.
10. **Backtest and evaluation**: test the whole pipeline historically and score agent outputs (offline).
11. **Monitoring and decision journal**: track live performance and keep an audit trail of every decision.

Keep each agent separate and give stages explicit, typed inputs and outputs, so that a stage can be tested, replaced, or rerun on its own.

## Project status and commands

The pipeline is implemented end to end in Python (see [README.md](README.md)): all eleven stages, the LangGraph wiring, the CLI, Docker Compose with Postgres + pgvector, and tests that mock the LLM. Paper trading only: nothing places orders.

Defaults for the questions [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) left open: data from `yfinance` behind the `MarketDataProvider` protocol, monthly rebalancing, and no Java dashboard.

Commands (Python 3.12+, `uv`):

- Install: `uv sync --extra postgres`
- All tests: `uv run pytest` (no network or API key needed; the LLM is mocked)
- One test: `uv run pytest tests/test_risk_and_execution.py::test_review_can_only_cut`
- Lint and format: `uv run ruff check . && uv run ruff format --check .`
- Types: `uv run pyright` (basic mode; pandas 3's inline types still report false positives, so it is not yet a gate)
- Offline run: `uv run stockassistant --provider synthetic run --date 2024-06-28 --llm offline`
- Live run: `ANTHROPIC_API_KEY=... uv run stockassistant run`, then `approve <run-id>` and `monitor`
- Backtest: `uv run stockassistant backtest --start 2023-01-01 --end 2025-12-31 [--mode full --llm offline]`

Module layout:

- `src/stockassistant/schemas.py`: every stage contract (frozen, versioned Pydantic models) and the LLM output schemas.
- `src/stockassistant/stages/<stage>.py`: one module per stage, each a plain `run()` function. Keep LangGraph out of them.
- `src/stockassistant/graph.py`: the LangGraph wiring, the journal hook and the risk retry loop.
- `src/stockassistant/agents/<role>/SKILL.md`: one prompt file per agent role with its model and effort in the front matter.
- `src/stockassistant/llm/`: the Anthropic client, the offline rule-based client and the skill loader.
- `src/stockassistant/data/`: the provider protocol and its implementations. `src/stockassistant/memory/`: the journal.
- `config/default.toml`: every tunable value, including risk limits and per-role model overrides. `db/`: the Postgres schema.

Rules to keep when changing code: numbers given to an agent come from code, never from the model; every agent output is a schema; citations are checked in code; the risk review can only cut; secrets come only from environment variables.

## Reference material is local only

`.gitignore` excludes `ml4t/`, `ml4t-code/`, `baa4f/`, and `baa4f-code/`. These directories hold the books and their companion code on the developer's machine. They are never committed, so they won't be in a fresh clone. When they are present, use them as reference, but write the project's own code outside them and never copy them into tracked paths.
