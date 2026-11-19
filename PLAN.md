# Project Plan

This plan turns the brief in [CLAUDE.md](CLAUDE.md) into a sequence of pipeline stages and milestones. It covers stocks only.

Book abbreviations: `ml4t` is *Machine Learning for Algorithmic Trading* (Third Edition) and `baa4f` is *Build AI Agents for Finance*. Book references below name the topic and, where known, the chapter. Chapter numbers are provisional: confirm them against the local copies during Milestone 0 and correct this file.

## Pipeline overview

Each stage is its own agent with one responsibility. Each stage reads the previous stages' typed outputs and writes one typed output, so it can be tested, replaced, or rerun on its own.

| # | Stage | Agent | Main input | Main output |
|---|-------|-------|------------|-------------|
| 1 | Data and universe | `DataAgent` | Run date, universe rules | `MarketDataSnapshot`, `Universe` |
| 2 | Market regime assessment | `RegimeAgent` | `MarketDataSnapshot` | `RegimeAssessment` |
| 3 | Signal and alpha models | `AlphaAgent` | `MarketDataSnapshot`, `Universe`, `RegimeAssessment` | `AlphaScores` |
| 4 | Fundamental and news research | `ResearchAgent` | Top-ranked names from `AlphaScores` | `ResearchNotes` |
| 5 | Stock and weight proposal | `ProposalAgent` | `RegimeAssessment`, `AlphaScores`, `ResearchNotes` | `PortfolioProposal` |
| 6 | Adversarial debate | `BullAgent`, `BearAgent`, `JudgeAgent` | `PortfolioProposal`, `ResearchNotes` | `DebateRecord` |
| 7 | Weight adjustment | `AdjustmentAgent` | `PortfolioProposal`, `DebateRecord` | `AdjustedPortfolio` |
| 8 | Risk check | `RiskAgent` | `AdjustedPortfolio`, `MarketDataSnapshot`, risk limits | `ApprovedPortfolio` or `RiskRejection` |
| 9 | Cost-aware execution plan | `ExecutionAgent` | `ApprovedPortfolio`, current holdings, cost model | `RebalancePlan` |
| 10 | Backtest and evaluation | `EvaluationAgent` (offline) | Historical data, the whole pipeline | `BacktestReport`, `AgentEvalReport` |
| 11 | Monitoring and decision journal | `MonitorAgent`, `Journal` | Every stage's output, live prices | `JournalEntry`, `MonitoringReport` |

Stages 1 to 9 run in order on each rebalance date. Stage 10 runs the same stages over history, outside the live run. Stage 11 records every stage's input and output during a run and tracks the portfolio between runs.

Why this order:

- Data comes first because every other stage depends on it, and the universe filter keeps later stages from wasting effort on untradable names.
- The regime comes before alpha so that models and the proposal can condition on it.
- Quantitative scores come before LLM research so the research agent only reads up on a short list, which keeps cost down and anchors the proposal in scored evidence.
- The risk check comes after the weight adjustment so that hard limits always have the last word over anything the agents argued for.
- Execution comes after the risk check so that only approved weights turn into trades.

## Stages

### 1. Data and universe (`DataAgent`)

Responsibility: fetch prices and fundamentals, check data quality, and filter to a tradable universe of individual stocks.

- **Inputs:** run date, universe rules (exchange, minimum price, minimum average dollar volume, minimum market cap, listing age).
- **Outputs:** `MarketDataSnapshot` (adjusted OHLCV, fundamentals, benchmark index series, all point-in-time as of the run date) and `Universe` (tickers that pass the rules, with the reason any ticker was dropped).
- **Quality checks:** missing bars, stale prices, split and dividend adjustment, outlier returns, survivorship bias (include delisted names in history), and look-ahead in fundamentals (use the date a filing became public, not the period it covers).
- **Notes:** this is a deterministic data step, not an LLM agent. Cache raw data locally and never commit it.
- **References:** ml4t chapters on market and fundamental data sources and on data quality; ml4t feature engineering chapter for point-in-time handling.

### 2. Market regime assessment (`RegimeAgent`)

Responsibility: classify the current market state.

- **Inputs:** `MarketDataSnapshot` (benchmark index, market breadth, volatility measures, rates if available).
- **Outputs:** `RegimeAssessment`: trend label (up, down, sideways), volatility label (low, normal, high), risk-on or risk-off, a confidence score, and the evidence behind each label.
- **Approach:** start with transparent rules (moving-average trend, realized volatility percentile). Add a statistical model such as a hidden Markov model or clustering once a baseline exists. An LLM may write the narrative summary, but the labels come from the model.
- **References:** ml4t chapters on time series models and unsupervised learning (regime detection); baa4f chapter on market analysis agents.

### 3. Signal and alpha models (`AlphaAgent`)

Responsibility: score every stock in the universe with an expected-return forecast or factor rank.

- **Inputs:** `MarketDataSnapshot`, `Universe`, `RegimeAssessment`.
- **Outputs:** `AlphaScores`: per-ticker score, forecast horizon, model version, and the main feature contributions.
- **Approach:** engineer alpha factors (momentum, value, quality, low volatility), then train models of rising complexity: linear models, then gradient-boosted trees, then neural models if they earn their place. Use leakage-safe cross-validation (purged, embargoed walk-forward splits) and evaluate with the information coefficient and factor return spreads.
- **References:** ml4t chapters on alpha factor research, factor evaluation, linear models, gradient boosting, deep learning for time series, and the cross-validation section on purging and embargo.

### 4. Fundamental and news research (`ResearchAgent`)

Responsibility: write a short research note on each top-ranked candidate.

- **Inputs:** top N tickers from `AlphaScores`, fundamentals from `MarketDataSnapshot`, recent filings and news.
- **Outputs:** `ResearchNotes`: per-ticker thesis, key risks, recent catalysts, a sentiment score, and cited sources.
- **Approach:** an LLM agent with tools for filings, earnings call transcripts, and news. Every claim must cite a source; uncited claims are dropped.
- **References:** baa4f ch. 4 and 5 (research and news agents); ml4t chapters on text data, sentiment, and NLP for trading.

### 5. Stock and weight proposal (`ProposalAgent`)

Responsibility: given the regime, propose candidate stocks and initial weights.

- **Inputs:** `RegimeAssessment`, `AlphaScores`, `ResearchNotes`.
- **Outputs:** `PortfolioProposal`: tickers, weights that sum to at most 1 (remainder is cash), and a rationale per position.
- **Approach:** build a baseline portfolio with an optimizer (for example mean-variance, risk parity, or hierarchical risk parity) on the alpha scores. The agent may tilt that baseline using the regime and research, and must explain each tilt.
- **References:** ml4t chapter on portfolio optimization and performance; baa4f chapter on portfolio construction agents.

### 6. Adversarial debate (`BullAgent`, `BearAgent`, `JudgeAgent`)

Responsibility: expose weak picks and hidden risks in the proposal.

- **Inputs:** `PortfolioProposal`, `ResearchNotes`, `RegimeAssessment`.
- **Outputs:** `DebateRecord`: arguments for and against each position and for the portfolio as a whole, plus the judge's per-position verdict (keep, trim, add, drop) with reasons.
- **Approach:** a fixed number of rounds. The bear agent must raise concentration, correlation, and regime-mismatch concerns as well as stock-specific ones. The judge scores arguments on evidence, not on tone.
- **References:** baa4f chapters on multi-agent systems and debate patterns.

### 7. Weight adjustment (`AdjustmentAgent`)

Responsibility: revise the weights based on the debate's outcome.

- **Inputs:** `PortfolioProposal`, `DebateRecord`.
- **Outputs:** `AdjustedPortfolio`: new weights and a change log linking each change to a debate verdict.
- **Approach:** apply bounded, rule-based changes from the verdicts (for example, a "trim" cuts a weight by a set fraction), then re-normalize. No new tickers are added at this stage.
- **References:** baa4f multi-agent coordination chapters; ml4t portfolio optimization chapter.

### 8. Risk check (`RiskAgent`)

Responsibility: enforce hard limits before anything becomes a trade.

- **Inputs:** `AdjustedPortfolio`, `MarketDataSnapshot`, a risk limits config.
- **Outputs:** `ApprovedPortfolio` (possibly scaled down) or `RiskRejection` with the breached limits.
- **Limits:** maximum weight per stock, maximum sector exposure, minimum number of holdings, maximum portfolio volatility and beta, maximum estimated drawdown, liquidity (position size as a share of average daily volume).
- **Approach:** deterministic code, not an LLM. Limits are config values the agents cannot change. If a limit is breached, scale or clip the weights; if it cannot be fixed that way, reject the run.
- **References:** baa4f ch. 8 (risk management and guardrails); ml4t ch. 19 (risk management).

### 9. Cost-aware execution plan (`ExecutionAgent`)

Responsibility: turn approved weights into a list of trades.

- **Inputs:** `ApprovedPortfolio`, current holdings and cash, a transaction cost model.
- **Outputs:** `RebalancePlan`: per-ticker share orders, estimated costs, turnover, and the trades skipped because their cost exceeded their expected benefit.
- **Approach:** model commissions, spread, and market impact; apply a turnover cap and a no-trade band around current weights. The assistant produces a plan for the person to review; it does not place orders.
- **References:** ml4t ch. 18 (transaction costs and execution).

### 10. Backtest and evaluation (`EvaluationAgent`)

Responsibility: measure whether the pipeline works, both as a strategy and as a set of agents.

- **Inputs:** historical point-in-time data, the pipeline stages, cost model.
- **Outputs:** `BacktestReport` (returns, Sharpe, drawdown, turnover, costs, benchmark comparison, results per regime) and `AgentEvalReport` (quality of research notes, debate verdicts, and proposals against what happened afterwards).
- **Approach:** walk-forward backtest that replays stages 1 to 9 at each historical rebalance date with only data available at that date. Guard against LLM look-ahead: the model may know what happened after a historical date, so compare LLM-stage results with a version that skips those stages, and treat LLM-stage backtest gains with suspicion. Account for multiple testing when comparing many variants (for example the deflated Sharpe ratio).
- **References:** ml4t backtesting chapters (vectorized and event-driven backtests, backtest pitfalls); baa4f chapter on evaluating agents.

### 11. Monitoring and decision journal (`MonitorAgent`, `Journal`)

Responsibility: keep an audit trail and track the live portfolio.

- **Inputs:** every stage's input and output for each run, live or daily prices.
- **Outputs:** `JournalEntry` per run (all stage outputs, model and prompt versions, timestamps) and `MonitoringReport` (performance against benchmark, drift from target weights, limit breaches, alerts).
- **Approach:** the journal is append-only and is written by the pipeline runner, not by any agent. The monitor flags when live results diverge from backtest expectations.
- **References:** baa4f chapters on observability and production agents; ml4t chapter on strategy evaluation in live trading.

## Shared design rules

- Every stage output is a typed schema (for example Pydantic models in Python) with a version number. Stages talk only through these schemas.
- Each stage can run on a saved input from the journal, so it can be tested and rerun alone.
- Deterministic stages (data, risk, execution) are plain code. LLM agents are used where judgment and text are involved (research, proposal tilts, debate).
- Each LLM agent has a fixed prompt file, a model setting, and recorded outputs for regression tests.
- Stocks only. Anything in the books about other instruments is skipped.

## Milestones

### Milestone 0: Learn

Work through both books with a focus on the parts each stage needs. For each stage, write a short note in `docs/notes/` that summarizes the relevant book sections, the companion code worth adapting, and open questions.

- Confirm the chapter numbers in this plan against the local copies and correct them.
- ml4t: data sources, alpha factors, cross-validation, gradient boosting, portfolio optimization, backtesting, transaction costs, risk.
- baa4f: agent fundamentals, research and news agents (ch. 4 and 5), multi-agent and debate patterns, risk guardrails (ch. 8), evaluation, observability.
- Done when every stage above has a note and the open questions are listed.

### Milestone 1: Foundations

- Choose the language, dependency manager, test runner, and linter, and record the commands in CLAUDE.md.
- Define every stage schema and a pipeline runner that passes outputs between stages and writes the journal.
- Implement stage 1 (data and universe) with quality checks and tests.
- Done when a run produces a validated `MarketDataSnapshot` and `Universe` for a given date.

### Milestone 2: Quantitative core

- Stage 2 (regime) with a rule-based baseline.
- Stage 3 (alpha) with a factor baseline and one ML model under purged walk-forward CV.
- Stage 5 (proposal) as an optimizer-only baseline, without the LLM tilt.
- Stage 8 (risk check) and stage 9 (execution plan).
- Stage 10 (backtest) for this quantitative-only pipeline.
- Done when the quantitative pipeline backtests end to end and its report is the baseline that later milestones must beat.

### Milestone 3: Agent layer

- Stage 4 (research agent).
- LLM tilt in stage 5.
- Stage 6 (debate) and stage 7 (weight adjustment).
- Agent evaluation in stage 10, comparing the full pipeline with the Milestone 2 baseline.
- Done when the full pipeline runs end to end and the evaluation shows whether the agents add value.

### Milestone 4: Operate

- Stage 11 (monitoring and journal reports).
- Paper-trade the pipeline on a schedule and compare live results with the backtest.
- Done after a paper-trading period with no unexplained divergences.

### Later

- Better regime models, more alpha models, and more debate roles, each added only if evaluation shows a gain.
