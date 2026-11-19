# Project Plan

This plan turns the brief in [CLAUDE.md](CLAUDE.md) into a sequence of pipeline stages and milestones. It covers stocks only.

The technology choices (models, framework, deployment, language, memory database) are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Book abbreviations: `ml4t` is *Machine Learning for Algorithmic Trading* (Third Edition) and `baa4f` is *Building AI Agents for Finance*. Chapter numbers below were checked against the local copies of both books.

## Pipeline overview

Each stage is its own agent, or a team of agents, with one responsibility. Each stage reads earlier stages' typed outputs and writes one typed output, so it can be tested, replaced, or rerun on its own.

The core follows the investment committee of baa4f ch. 8: four specialist analysts work in parallel and write reports, and those reports become one shared set of evidence that the portfolio manager, the debaters and the risk officer all read.

| # | Stage | Agent(s) | Kind | Main input | Main output |
|---|-------|----------|------|------------|-------------|
| 1 | Data and universe | `DataAgent` | Code | Run date, universe rules | `MarketDataSnapshot`, `Universe` |
| 2 | Market regime assessment | `RegimeAgent` | Model | `MarketDataSnapshot` | `RegimeAssessment` |
| 3 | Signal and alpha screen | `AlphaAgent` | ML models | `MarketDataSnapshot`, `Universe`, `RegimeAssessment` | `AlphaScores`, `ShortList` |
| 4 | Analyst team | `FundamentalAnalyst`, `TechnicalAnalyst`, `SentimentAnalyst`, `MacroStrategist`, `ReportValidator` | LLM agents with tools | `ShortList`, `AlphaScores`, `RegimeAssessment`, `MarketDataSnapshot` | `AnalystReports` (shared) |
| 5 | Stock and weight proposal | `PortfolioManager` | Optimizer + LLM | `AnalystReports`, `AlphaScores`, `RegimeAssessment` | `PortfolioProposal` |
| 6 | Adversarial debate | `BullAgent`, `BearAgent`, `DevilsAdvocate`, `Judge` | LLM agents | `PortfolioProposal`, `AnalystReports` | `DebateRecord` |
| 7 | Weight adjustment | `AdjustmentAgent` | Rules + LLM rationale | `PortfolioProposal`, `DebateRecord` | `AdjustedPortfolio` |
| 8 | Risk check | `RiskOfficer` | Code + LLM risk review | `AdjustedPortfolio`, `AnalystReports`, `MarketDataSnapshot`, risk limits | `ApprovedPortfolio` or `RiskRejection` |
| 9 | Cost-aware execution plan | `ExecutionAgent` | Code | `ApprovedPortfolio`, holdings, cost model | `RebalancePlan` |
| 10 | Backtest and evaluation | `EvaluationAgent` | Code + LLM judge (offline) | History, whole pipeline | `BacktestReport`, `AgentEvalReport` |
| 11 | Monitoring and decision journal | `MonitorAgent`, `Journal` | Code + LLM summary | Every stage's output, live prices | `JournalEntry`, `MonitoringReport` |

Stages 1 to 9 run in order on each rebalance date. Stage 10 replays the same stages over history, outside the live run. Stage 11 records every stage's input and output during a run and tracks the portfolio between runs.

Why this order:

- Data comes first because every other stage depends on it, and the universe filter keeps later stages from wasting effort on untradable names.
- The regime model and the alpha screen come before the analysts. They are cheap and quantitative, so they narrow thousands of stocks to a short list and give the analysts hard numbers to start from. Running four LLM analysts on the whole universe would be slow and costly.
- The four analysts write their reports once, and every later decision maker reads the same reports. The manager, the debaters and the risk officer therefore argue from the same evidence, and no stage re-does research.
- The debate is layered on top of the proposal rather than replacing it, as in the committee-plus-debate design of baa4f ch. 8.
- The risk check comes after the weight adjustment so that hard limits always have the last word over anything the agents argued for.
- Execution comes after the risk check so that only approved weights turn into trades.

## The analyst team and its shared reports

baa4f ch. 8 builds its committee on four "information lenses", each a distinct source of information about future returns. This plan uses the same four analysts:

| Analyst | Lens | Scope | Built on |
|---------|------|-------|----------|
| `FundamentalAnalyst` | Financial health and valuation | Per ticker | Statements, ratios, DCF and peer multiples (tools); stage 3 quality and value factors |
| `TechnicalAnalyst` | Price action, momentum and volatility | Per ticker | Stage 3 momentum and volatility features; price data |
| `SentimentAnalyst` | News flow, filings and market narratives | Per ticker | News search, filings and transcripts (RAG); sentiment scoring |
| `MacroStrategist` | The broader economic and market environment | Once per run | Stage 2 `RegimeAssessment`; index, rates and breadth data |

Every analyst report has the same schema, so later stages can read them side by side:

- `ticker` (or `market` for the macro report), `lens`, `run_date`
- `view`: bullish, neutral or bearish, with a `confidence` from 0 to 1
- `summary`: a short thesis
- `key_metrics`: the numbers the view rests on, each computed by a tool
- `risks`: risks this lens sees, each tagged with a type (for example earnings date, leverage, litigation, crowding, macro shock)
- `sources`: citations for every claim

`AnalystReports` is the set of all reports for a run, stored in the shared run state (the "shared workspace" of baa4f ch. 8) and written once. Three stages read it, each for its own purpose:

| Reader | What it uses the reports for |
|--------|------------------------------|
| `PortfolioManager` (stage 5) | Combines the four views per ticker into a thesis and decides tilts away from the optimizer baseline |
| Debaters (stage 6) | The bull and bear must ground every argument in a report; the devil's advocate and the judge check arguments against them |
| `RiskOfficer` (stage 8) | Reads the `risks` fields and the macro report to flag event and qualitative risks that the hard limits cannot see |

## Agent design patterns

Each agent is specified with the four pattern families from baa4f ch. 2 ("Exploring Design Patterns for AI Agents"). Chapter 6 covers the reasoning patterns in depth and chapter 7 the orchestration styles.

| Family | What it decides | Patterns used in this project |
|--------|-----------------|-------------------------------|
| **Capability** | What an agent can do | Tool use; memory (working, episodic, semantic, procedural); retrieval-augmented generation (RAG) |
| **Reasoning** | How an agent thinks through a task | Planning (plan-then-execute, plan-and-execute); ReAct; chain-of-thought (CoT); self-consistency; self-refine; CRITIC; tree-of-thoughts |
| **Quality** | How an agent's output is checked | Reflection; LLM-as-a-judge (evaluator–optimizer); guardrails |
| **Orchestration** | How agents work together | Sequential pipeline; parallelization with fan-out to specialists and fan-in to the manager (ch. 8); shared state as a workspace; evaluator–optimizer; peer-to-peer debate (ch. 8) |

Rules that apply across all agents:

- **Numbers come from code, not from the LLM.** baa4f ch. 4 found that "LLMs should not classify values they did not compute". Every ratio, score, weight and risk figure is computed by a tool or by plain code, and the LLM only reads and explains it.
- **Structured outputs.** Every LLM agent returns a typed schema, like the judge's strict `VERDICT`, `CONFIDENCE`, `RATIONALE` format in baa4f ch. 8, so outputs parse reliably and stay consistent across runs.
- **Guardrails at three places.** Inputs, tool outputs (news and filings can carry injected instructions) and final outputs are checked, as in baa4f ch. 12.
- **Bounded loops.** Every reasoning or refinement loop has a maximum number of rounds and a token budget (baa4f ch. 12, "Termination conditions and budgets").
- **Model diversity where agents disagree.** Analysts and debate roles can use different models or temperatures, to reduce the correlated reasoning that baa4f ch. 8 warns about.

### Pattern summary per agent

| Stage | Capability | Reasoning | Quality | Orchestration role |
|-------|------------|-----------|---------|--------------------|
| 1 Data | Tool use (data APIs) | None (deterministic) | Guardrails: data-quality checks | Pipeline step 1 |
| 2 Regime | Tool use; episodic memory of past regimes | None (model labels) | Programmatic checks on model stability | Pipeline step |
| 3 Alpha | Tool use (model library) | None (ML models) | Programmatic checks: leakage, IC thresholds | Pipeline step |
| 4 Analysts | Tool use; RAG over filings and news; semantic memory | ReAct per analyst; CoT for the view | Reflection and LLM-as-a-judge by `ReportValidator`; citation and injection guardrails | Parallel fan-out; writes the shared reports |
| 5 Proposal | Tool use (optimizer); episodic memory | CoT; self-consistency | Self-refine; weight guardrails | Fan-in from the analysts |
| 6 Debate | Working memory (shared debate state) | CoT; CRITIC-style tool-checked critique | LLM-as-a-judge | Peer-to-peer debate: bull and bear in parallel, then devil's advocate, then judge |
| 7 Adjustment | Procedural memory (adjustment rules) | CoT for the rationale | Guardrails: bounded changes | Pipeline step |
| 8 Risk | Tool use (risk calculators); reads shared reports | CoT for the risk review | Guardrails: hard limits, approval gate; review can only tighten | Checkpoint; can send work back to stage 7 once |
| 9 Execution | Tool use (cost model) | None (deterministic) | Guardrails: turnover cap, human approval | Pipeline step, human-in-the-loop gate |
| 10 Evaluation | Tool use (backtester); RAG over the journal | Plan-then-execute | LLM-as-a-judge; reference-based and trajectory evaluation | Runs the whole pipeline offline |
| 11 Monitoring | Episodic memory (journal); tool use | CoT for summaries | Drift checks; alerts | Observer of every stage |

## How the agents integrate

The top level is a **sequential pipeline** run by a deterministic runner (baa4f ch. 7, "Sequential (pipeline)"), chosen because a portfolio decision must be reproducible and traceable. Inside that backbone:

- The analyst team is **parallelization**: a fan-out to four specialists, then a validator, then the reports land in **shared state** (baa4f ch. 8, "Fan-out to specialists, fan-in to the portfolio manager").
- The shared `AnalystReports` fan in to three readers: the portfolio manager, the debate, and the risk officer.
- The debate is a **peer-to-peer** exchange settled by a judge.
- The risk check is a **guardrail checkpoint** between stages.
- The journal is **episodic memory** shared by all stages and read back by the regime, proposal and monitoring agents.

```mermaid
flowchart TD
    classDef code fill:#e8eef7,stroke:#4a6fa5,color:#1b2a41
    classDef llm fill:#f6ecd9,stroke:#b5832a,color:#3d2c0c
    classDef gate fill:#f7dede,stroke:#b03a3a,color:#4a1010
    classDef store fill:#e3f1e3,stroke:#3f8a3f,color:#123512

    RUN["Pipeline runner<br/>sequential orchestration"]:::code

    DATA["1 DataAgent<br/>tool use + quality guardrails"]:::code
    REG["2 RegimeAgent<br/>regime model"]:::code
    ALPHA["3 AlphaAgent<br/>factors + ML, short list"]:::code

    subgraph S4["4 Analyst team (parallel)"]
        FUND["FundamentalAnalyst<br/>ReAct + valuation tools"]:::llm
        TECH["TechnicalAnalyst<br/>ReAct + price tools"]:::llm
        SENT["SentimentAnalyst<br/>ReAct + RAG over news"]:::llm
        MACRO["MacroStrategist<br/>CoT over regime"]:::llm
        VAL["ReportValidator<br/>reflection + LLM-as-a-judge"]:::llm
        FUND --> VAL
        TECH --> VAL
        SENT --> VAL
        MACRO --> VAL
    end

    REPORTS[("AnalystReports<br/>shared state, written once")]:::store

    PM["5 PortfolioManager<br/>optimizer + self-consistency"]:::llm

    subgraph S6["6 Debate (peer-to-peer)"]
        BULL["BullAgent"]:::llm
        BEAR["BearAgent"]:::llm
        DEV["DevilsAdvocate<br/>one weakness per side"]:::llm
        JUDGE["Judge<br/>LLM-as-a-judge"]:::llm
        BULL --> DEV
        BEAR --> DEV
        DEV --> JUDGE
    end

    ADJ["7 AdjustmentAgent<br/>bounded rule-based changes"]:::code
    RISK{"8 RiskOfficer<br/>hard limits + risk review"}:::gate
    EXEC["9 ExecutionAgent<br/>cost model + turnover cap"]:::code
    HUMAN{"Person reviews<br/>the trade list"}:::gate

    JOURNAL[("Decision journal<br/>episodic memory, audit trail")]:::store
    KB[("Research knowledge base<br/>semantic memory for RAG")]:::store
    MON["11 MonitorAgent<br/>performance and drift"]:::code
    EVAL["10 EvaluationAgent<br/>walk-forward backtest + agent evals"]:::code

    RUN --> DATA
    DATA -- "MarketDataSnapshot, Universe" --> REG
    REG -- "RegimeAssessment" --> ALPHA
    ALPHA -- "ShortList" --> FUND
    ALPHA -- "ShortList" --> TECH
    ALPHA -- "ShortList" --> SENT
    REG -- "RegimeAssessment" --> MACRO
    VAL -- "validated reports" --> REPORTS

    REPORTS == "reports" ==> PM
    REPORTS == "reports" ==> BULL
    REPORTS == "reports" ==> BEAR
    REPORTS == "risk fields + macro view" ==> RISK
    ALPHA -. "AlphaScores" .-> PM

    PM -- "PortfolioProposal" --> BULL
    PM -- "PortfolioProposal" --> BEAR
    JUDGE -- "DebateRecord" --> ADJ
    PM -. "PortfolioProposal" .-> ADJ
    ADJ -- "AdjustedPortfolio" --> RISK
    RISK -- "ApprovedPortfolio" --> EXEC
    RISK -. "RiskRejection, one retry" .-> ADJ
    EXEC --> HUMAN

    KB <-.-> FUND
    KB <-.-> SENT
    JOURNAL -. "past regimes" .-> REG
    JOURNAL -. "past decisions" .-> PM
    RUN -. "logs every stage output" .-> JOURNAL
    JOURNAL --> MON
    JOURNAL --> EVAL
    EVAL -. "replays stages 1 to 9 over history" .-> RUN
```

Legend: blue boxes are deterministic code, amber boxes use an LLM, red shapes are gates that can stop the run, and green cylinders are shared state or memory stores. Thick arrows show the shared analyst reports feeding their three readers. Solid arrows carry a stage's main output; dotted arrows carry side inputs, feedback, or memory.

## Stages

### 1. Data and universe (`DataAgent`)

Responsibility: fetch prices and fundamentals, check data quality, and filter to a tradable universe of individual stocks.

- **Inputs:** run date, universe rules (exchange, minimum price, minimum average dollar volume, minimum market cap, listing age).
- **Outputs:** `MarketDataSnapshot` (adjusted OHLCV, fundamentals, benchmark index series, all point-in-time as of the run date) and `Universe` (tickers that pass the rules, with the reason any ticker was dropped).
- **Quality checks:** missing bars, stale prices, split and dividend adjustment, outlier returns, survivorship bias (include delisted names in history), and look-ahead in fundamentals (use the date a filing became public, not the period it covers).
- **Patterns:**
  - Capability: tool use for market data and fundamentals APIs, with a shared cache so tools are not called twice (baa4f ch. 4).
  - Reasoning: none. This is deterministic code.
  - Quality: guardrails as programmatic data-quality assertions; the run stops if they fail.
  - Orchestration: first step of the sequential pipeline.
- **References:** ml4t ch. 2 (The Financial Data Universe: point-in-time correctness, survivorship bias), ch. 4 (Fundamental and Alternative Data: the point-in-time pipeline), ch. 6 (Strategy Research Framework: universe and tradability rules).

### 2. Market regime assessment (`RegimeAgent`)

Responsibility: classify the current market state with a model. The narrative interpretation is written by the `MacroStrategist` in stage 4.

- **Inputs:** `MarketDataSnapshot` (benchmark index, market breadth, volatility measures, rates if available).
- **Outputs:** `RegimeAssessment`: trend label (up, down, sideways), volatility label (low, normal, high), risk-on or risk-off, a confidence score, and the evidence behind each label.
- **Approach:** start with observable rules (moving-average trend, realized volatility percentile). Add a hidden Markov model or distribution-based regime features once a baseline exists.
- **Patterns:**
  - Capability: tool use to run the regime models; episodic memory of past regime calls from the journal, so later stages know how long the current regime has lasted.
  - Reasoning: none. The labels come from the model.
  - Quality: programmatic checks that labels do not flip on small data changes.
  - Orchestration: sequential pipeline step.
- **References:** ml4t ch. 9 (Model-Based Feature Extraction, section 9.5 Regime features), ch. 1 (regime detection with labeling, conditioning and monitoring).

### 3. Signal and alpha screen (`AlphaAgent`)

Responsibility: score every stock in the universe and pick the short list the analysts will study.

- **Inputs:** `MarketDataSnapshot`, `Universe`, `RegimeAssessment`.
- **Outputs:** `AlphaScores` (per-ticker score, forecast horizon, model version, and the main feature contributions) and `ShortList` (the top N tickers, plus any current holdings so they are always reviewed).
- **Approach:** engineer alpha factors (momentum, value, quality, low volatility, regime interactions), then train models of rising complexity: linear models, then gradient-boosted trees, then deep models if they earn their place. Use walk-forward cross-validation with purging and an embargo, and evaluate with the information coefficient and factor return spreads.
- **Patterns:**
  - Capability: tool use for the feature and model library.
  - Reasoning: none in the LLM sense. Scores come from trained models.
  - Quality: programmatic checks for leakage, minimum out-of-sample IC, and SHAP-based feature drift.
  - Orchestration: sequential pipeline step; its features feed the fundamental and technical analysts.
- **References:** ml4t ch. 6 (Strategy Research Framework: walk-forward cross-validation), ch. 7 (Defining the Learning Task), ch. 8 (Financial Feature Engineering), ch. 11 (The ML Pipeline), ch. 12 (Advanced Models for Tabular Data), ch. 13 (Deep Learning for Time Series), ch. 14 (Latent Factor Models).

### 4. Analyst team (`FundamentalAnalyst`, `TechnicalAnalyst`, `SentimentAnalyst`, `MacroStrategist`, `ReportValidator`)

Responsibility: write one report per lens, per ticker on the short list (and one macro report per run), and publish them as the shared `AnalystReports`.

- **Inputs:** `ShortList`, `AlphaScores`, `RegimeAssessment`, `MarketDataSnapshot`, recent news, filings and transcripts.
- **Outputs:** `AnalystReports`, using the common report schema in [The analyst team and its shared reports](#the-analyst-team-and-its-shared-reports).
- **Agents:**
  - `FundamentalAnalyst` reads statements, computes ratios, DCF and peer multiples with tools, and judges financial health and valuation.
  - `TechnicalAnalyst` reads price action, momentum, volatility and the stage 3 technical features.
  - `SentimentAnalyst` searches news, filings and transcripts and scores the narrative around each stock.
  - `MacroStrategist` interprets the `RegimeAssessment` and market data into a market-wide view, including which sectors and styles the regime favors.
  - `ReportValidator` checks every claim against its source and every number against the tool that produced it, and sends weak reports back once.
- **Patterns:**
  - Capability: tool use (statements, ratios, valuation, prices, news search); RAG over filings and transcripts; semantic memory in a research knowledge base, so facts found in earlier runs are reused.
  - Reasoning: ReAct for each analyst's tool loop; chain-of-thought for the final view, built into the instructions rather than left implicit (baa4f ch. 4).
  - Quality: reflection and LLM-as-a-judge by the validator in an evaluator–optimizer loop (baa4f ch. 2 news agent lab); a citation guardrail drops any claim without a source; a tool-output guardrail screens news text for injected instructions.
  - Orchestration: parallel fan-out to the four analysts and across tickers (baa4f ch. 7, Parallelization; ch. 8 fan-out to specialists); reports are written to shared state once and are read-only afterwards.
- **References:** baa4f ch. 8 (Part A: the four canonical information lenses, the six roles in the model committee, implementing the four specialist analyst nodes), ch. 4 (Building a Fundamental Analysis Agentic System), ch. 5 (Deep Search), ch. 10 (Agentic RAG); ml4t ch. 10 (Text Feature Engineering), ch. 22 (RAG for Financial Research).

### 5. Stock and weight proposal (`PortfolioManager`)

Responsibility: combine the analysts' views into a portfolio with initial weights.

- **Inputs:** `AnalystReports`, `AlphaScores`, `RegimeAssessment`.
- **Outputs:** `PortfolioProposal`: tickers, weights that sum to at most 1 (the remainder is cash), and per position a thesis that names which analyst views it rests on.
- **Approach:** build a baseline portfolio with an optimizer (risk parity, shrinkage mean-variance, or hierarchical risk parity) on the alpha scores. The manager then tilts that baseline within set bounds, using the four views per ticker and the macro view, and must explain each tilt with reference to the reports.
- **Patterns:**
  - Capability: tool use for the optimizer; episodic memory of past proposals and how they turned out.
  - Reasoning: chain-of-thought for each tilt; self-consistency, sampling several tilt decisions and keeping the majority view (baa4f ch. 6 lab).
  - Quality: self-refine on the written thesis; guardrails on tilt size and weight sums.
  - Orchestration: fan-in from the analyst team, like the portfolio manager in the baa4f ch. 8 committee.
- **References:** ml4t ch. 17 (Portfolio Construction); baa4f ch. 8 (synthesizing recommendations with the portfolio manager), ch. 6 (Self-consistency, Self-refine).

### 6. Adversarial debate (`BullAgent`, `BearAgent`, `DevilsAdvocate`, `Judge`)

Responsibility: expose weak picks and hidden risks in the proposal.

- **Inputs:** `PortfolioProposal`, `AnalystReports`.
- **Outputs:** `DebateRecord`: arguments for and against each position and the portfolio as a whole, plus the judge's per-position verdict (keep, trim, add, drop) with confidence and rationale.
- **Approach:** the bull and bear run in parallel without seeing each other's arguments, and every argument must cite an analyst report. The bear is told to look for disagreement between the four lenses (for example, a strong fundamental view with a bearish technical view) and for concentration, correlation and regime-mismatch problems. The devil's advocate names the single most important weakness on each side. The judge decides in a strict structured format.
- **Patterns:**
  - Capability: working memory in a shared debate state; read access to `AnalystReports`.
  - Reasoning: chain-of-thought for each argument; CRITIC-style critique, where a challenge to a number must be checked with a tool before it counts.
  - Quality: LLM-as-a-judge, scoring arguments on evidence rather than tone; the judge is calibrated in stage 10.
  - Orchestration: peer-to-peer debate layered on top of the proposal (baa4f ch. 8, Part B), with parallel bull and bear; different models or temperatures per side to reduce correlated reasoning.
- **References:** baa4f ch. 8 (Part B, the four debate roles), ch. 7 (Peer-to-peer, resolving conflicts when agents disagree); ml4t ch. 24 (the debate-supervisor pattern).

### 7. Weight adjustment (`AdjustmentAgent`)

Responsibility: revise the weights based on the debate's outcome.

- **Inputs:** `PortfolioProposal`, `DebateRecord` (and a `RiskRejection` on a retry).
- **Outputs:** `AdjustedPortfolio`: new weights and a change log linking each change to a debate verdict or risk finding.
- **Approach:** apply bounded, rule-based changes from the verdicts (for example, a "trim" cuts a weight by a set fraction, scaled by the judge's confidence), then re-normalize. No new tickers are added at this stage.
- **Patterns:**
  - Capability: procedural memory, the adjustment rulebook kept as config.
  - Reasoning: chain-of-thought only for the written change log.
  - Quality: guardrails on the size of each change.
  - Orchestration: sequential pipeline step; together with stages 5, 6 and 8 it forms a bounded evaluator–optimizer loop.
- **References:** baa4f ch. 7 (Evaluator–optimizer), ch. 8 (closing the loop); ml4t ch. 17 (Portfolio Construction).

### 8. Risk check (`RiskOfficer`)

Responsibility: act as the independent control function before anything becomes a trade.

- **Inputs:** `AdjustedPortfolio`, `AnalystReports`, `MarketDataSnapshot`, a risk limits config.
- **Outputs:** `ApprovedPortfolio` (possibly scaled down) or `RiskRejection` with the breached limits and flagged risks.
- **Two layers:**
  - **Hard limits, in code.** Maximum weight per stock, maximum sector exposure, minimum number of holdings, maximum portfolio volatility and beta, maximum estimated drawdown, liquidity (position size as a share of average daily volume), and regime-triggered exposure caps. Limits are config values the agents cannot change.
  - **Risk review, with an LLM.** Reads the `risks` fields of every analyst report and the macro report, and flags what the hard limits cannot see: an earnings date inside the holding period, pending litigation, several holdings exposed to the same macro shock, or analysts who disagree sharply on a large position. The review can only scale positions down or veto them, never raise a weight.
- **Approach:** if a limit is breached, scale or clip the weights. If that cannot fix it, send a `RiskRejection` back to stage 7 once, and stop the run if the retry also fails.
- **Patterns:**
  - Capability: tool use for risk calculators; read access to `AnalystReports`.
  - Reasoning: chain-of-thought for the risk review.
  - Quality: guardrails as hard limits and an approval gate; the review is one-directional (it can only tighten).
  - Orchestration: independent checkpoint between stages, like the risk officer in the baa4f ch. 8 committee.
- **References:** ml4t ch. 19 (Risk Management: drawdowns, kill switches, regime-triggered exposure caps); baa4f ch. 8 (validating recommendations with the risk officer), ch. 12 (guardrails, autonomy tiers and approval gates).

### 9. Cost-aware execution plan (`ExecutionAgent`)

Responsibility: turn approved weights into a list of trades.

- **Inputs:** `ApprovedPortfolio`, current holdings and cash, a transaction cost model.
- **Outputs:** `RebalancePlan`: per-ticker share orders, estimated costs, turnover, and the trades skipped because their cost exceeded their expected benefit.
- **Approach:** model commissions, spread and market impact; apply a turnover cap and a no-trade band around current weights. The assistant produces a plan for the person to review; it does not place orders.
- **Patterns:**
  - Capability: tool use for the cost model.
  - Reasoning: none. This is deterministic code.
  - Quality: guardrails on turnover and cost; a human-in-the-loop gate before any trade (baa4f ch. 12).
  - Orchestration: last step of the sequential pipeline.
- **References:** ml4t ch. 18 (Transaction Costs), ch. 25 (Live Trading Systems).

### 10. Backtest and evaluation (`EvaluationAgent`)

Responsibility: measure whether the pipeline works, both as a strategy and as a set of agents.

- **Inputs:** historical point-in-time data, the pipeline stages, the cost model, the journal.
- **Outputs:** `BacktestReport` (returns, Sharpe, drawdown, turnover, costs, benchmark comparison, results per regime) and `AgentEvalReport` (scores for each analyst's views, the debate verdicts and the proposals against what happened afterwards).
- **Approach:** a walk-forward backtest that replays stages 1 to 9 at each historical rebalance date with only the data available at that date. Guard against LLM look-ahead: the model may know what happened after a historical date, so compare the full pipeline with a version that skips the LLM stages, and treat LLM-stage backtest gains with suspicion. Track each analyst's hit rate separately, so a lens that adds nothing can be dropped. Correct for testing many variants (backtest overfitting statistics).
- **Patterns:**
  - Capability: tool use for the backtester; RAG over the journal to find comparable past decisions.
  - Reasoning: plan-then-execute for an evaluation run.
  - Quality: the three evaluation layers of baa4f ch. 11: reference-based checks, trajectory checks (did the agent take the right path), and LLM-as-a-judge for reasoning quality, with the judge calibrated against hand labels; adversarial tests for the analysts and debaters.
  - Orchestration: runs the whole pipeline offline; results feed back into prompt and model changes.
- **References:** ml4t ch. 16 (Strategy Simulation: backtest protocol, regime-sliced performance, backtest overfitting), ch. 20 (Strategy Synthesis); baa4f ch. 8 (backtesting the architecture, walk-forward), ch. 11 (Evaluating AI Agents in Finance).

### 11. Monitoring and decision journal (`MonitorAgent`, `Journal`)

Responsibility: keep an audit trail and track the live portfolio.

- **Inputs:** every stage's input and output for each run, live or daily prices.
- **Outputs:** `JournalEntry` per run (all stage outputs including every analyst report, model and prompt versions, timestamps, token cost) and `MonitoringReport` (performance against benchmark and backtest expectations, drift from target weights, data and concept drift, limit breaches, alerts).
- **Approach:** the journal is append-only and is written by the pipeline runner, not by any agent. The monitor flags when live results diverge from backtest expectations.
- **Patterns:**
  - Capability: the journal is the system's episodic memory, read by the regime, proposal and evaluation agents; tool use for performance and drift metrics.
  - Reasoning: chain-of-thought only to summarize a report.
  - Quality: drift detection and alerts; structured tracing of every agent call (baa4f ch. 12).
  - Orchestration: observes every stage rather than taking part in the pipeline.
- **References:** ml4t ch. 26 (MLOps and Governance: performance monitoring, drift detection), ch. 25 (Live Trading Systems); baa4f ch. 12 (observing agents in production, the anatomy of an audit record), ch. 7 (observing and tracing multi-agent behavior).

## Shared design rules

- Every stage output is a typed schema (for example Pydantic models in Python) with a version number. Stages talk only through these schemas.
- `AnalystReports` is written once per run and is read-only for every later stage.
- Each stage can run on a saved input from the journal, so it can be tested and rerun alone.
- Deterministic stages (data, regime, alpha, adjustment, the risk limits, execution) are plain code or trained models. LLM agents are used where judgment and text are involved (analysts, proposal tilts, debate, risk review).
- Each LLM agent has a versioned prompt file, a model setting, a loop budget, and recorded outputs for regression tests.
- Stocks only. Anything in the books about other instruments is skipped.

## Implementation status

The first implementation covers all eleven stages end to end (see [README.md](README.md)). Where it is simpler than the design above, so far:

- Analysts receive an evidence pack computed by code before the call (plan-then-execute) instead of calling tools in a ReAct loop.
- No retrieval yet: the sentiment analyst reads point-in-time headlines from the data provider. The `kb_chunks` table for semantic memory exists, but nothing fills it.
- The alpha model is a walk-forward ridge regression over price factors, blended with a factor composite; no gradient-boosted or deep models yet. The regime model is rule-based.
- The manager's self-consistency is in place; self-refine of the thesis and CRITIC-style tool checks in the debate are not.
- The bear uses Sonnet 5.5; the optional second vendor is not wired in.
- The person approves a plan with `stockassistant approve` rather than a LangGraph `interrupt()`, and runs are not checkpointed.
- Episodic memory feeds the regime stage (time in regime) and the monitor; the manager does not read past proposals yet.

## Milestones

### Milestone 0: Learn

Work through both books with a focus on the parts each stage needs. For each stage, write a short note in `docs/notes/` that summarizes the relevant book sections, the companion code worth adapting, and open questions.

- baa4f: ch. 2 (design patterns), ch. 4 and 5 (research agents), ch. 6 (reasoning paradigms), ch. 7 (multi-agent architectural styles), ch. 8 (committee and debate; the closest match to this whole pipeline), ch. 11 (evaluation), ch. 12 (operations, guardrails, governance).
- ml4t: ch. 2, 4 and 6 (data, universe, research framework), ch. 7 to 9 and 11 to 14 (features, regimes, models), ch. 16 (backtesting), ch. 17 (portfolio construction), ch. 18 (transaction costs), ch. 19 (risk), ch. 24 to 26 (agents, live trading, monitoring).
- Done when every stage above has a note and the open questions are listed.

### Milestone 1: Foundations

- Choose the language, dependency manager, test runner and linter, and record the commands in CLAUDE.md.
- Define every stage schema, including the common analyst report schema, and the pipeline runner that passes outputs between stages and writes the journal.
- Implement stage 1 (data and universe) with quality checks and tests.
- Done when a run produces a validated `MarketDataSnapshot` and `Universe` for a given date.

### Milestone 2: Quantitative core

- Stage 2 (regime) with a rule-based baseline.
- Stage 3 (alpha screen) with a factor baseline and one ML model under purged walk-forward CV.
- Stage 5 (proposal) as an optimizer-only baseline, without analyst tilts.
- Stage 8 hard limits and stage 9 (execution plan).
- Stage 10 (backtest) for this quantitative-only pipeline.
- Done when the quantitative pipeline backtests end to end and its report is the baseline that later milestones must beat.

### Milestone 3: Agent layer

- Shared agent harness: tool registry, structured outputs, guardrails at input, tool output and output, loop budgets, and tracing.
- Stage 4 analyst team: start with the fundamental and macro analysts, then add technical and sentiment, with the validator.
- Analyst-driven tilts in stage 5 and the risk review in stage 8.
- Stage 6 (debate) and stage 7 (weight adjustment), including the one-retry loop from stage 8.
- Agent evaluation in stage 10, comparing the full pipeline with the Milestone 2 baseline and scoring each analyst separately.
- Done when the full pipeline runs end to end and the evaluation shows whether the agents add value.

### Milestone 4: Operate

- Stage 11 (monitoring and journal reports).
- Paper-trade the pipeline on a schedule and compare live results with the backtest.
- Done after a paper-trading period with no unexplained divergences.

### Later

- Better regime models, more alpha models, and stronger reasoning patterns such as tree-of-thoughts or LATS for the proposal (baa4f ch. 6), each added only if evaluation shows a gain.
