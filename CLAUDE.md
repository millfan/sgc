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

## Project status

There is no source code yet: the only tracked files are this one, `PLAN.md`, and `.gitignore`. No language, dependency manager, build, lint, or test commands have been chosen. Once they exist, update this file with the commands (including how to run a single test) and the actual module layout.

## Reference material is local only

`.gitignore` excludes `ml4t/`, `ml4t-code/`, `baa4f/`, and `baa4f-code/`. These directories hold the books and their companion code on the developer's machine. They are never committed, so they won't be in a fresh clone. When they are present, use them as reference, but write the project's own code outside them and never copy them into tracked paths.
