"""Typed, versioned contracts between pipeline stages (PLAN.md, "Shared design rules").

Every stage reads earlier stages' models and returns one model. Models are frozen so a
later stage cannot change an earlier stage's output, and every model carries a
``schema_version`` so journal entries stay readable when a contract changes.

The second half of the module holds the schemas the LLM agents fill in. They contain
judgments and text only: every number shown to or used by an agent is computed by code
(PLAN.md: "Numbers come from code, not from the LLM").
"""

from __future__ import annotations

from datetime import date
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: int = SCHEMA_VERSION


# ---------------------------------------------------------------- 1. Data and universe


class Fundamentals(Contract):
    market_cap: float | None = None
    pe_ratio: float | None = None
    price_to_book: float | None = None
    return_on_equity: float | None = None
    debt_to_equity: float | None = None
    profit_margin: float | None = None
    revenue_growth: float | None = None
    dividend_yield: float | None = None
    next_earnings_date: date | None = None


class NewsItem(Contract):
    id: str
    ticker: str
    published: date  # the date it became known; retrieval filters on this (point in time)
    title: str
    source: str
    url: str | None = None


class DataQualityReport(Contract):
    checked_tickers: int
    failed: dict[str, str] = Field(default_factory=dict)  # ticker -> reason
    warnings: list[str] = Field(default_factory=list)


class MarketDataSnapshot(Contract):
    """Point-in-time market data as of ``as_of``. No row is dated after ``as_of``."""

    as_of: date
    benchmark: str
    dates: list[date]
    close: dict[str, list[float | None]]  # adjusted close per ticker, aligned with ``dates``
    volume: dict[str, list[float | None]]
    sectors: dict[str, str] = Field(default_factory=dict)
    fundamentals: dict[str, Fundamentals] = Field(default_factory=dict)
    news: dict[str, list[NewsItem]] = Field(default_factory=dict)
    quality: DataQualityReport

    def close_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.close, index=pd.DatetimeIndex(pd.to_datetime(self.dates))).astype(float)

    def volume_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.volume, index=pd.DatetimeIndex(pd.to_datetime(self.dates))).astype(float)

    def journal_view(self) -> dict:
        """Compact form for the journal; full price history is reproducible from the provider."""
        return self.model_dump(mode="json", exclude={"close", "volume", "dates"}) | {
            "first_date": str(self.dates[0]) if self.dates else None,
            "last_date": str(self.dates[-1]) if self.dates else None,
            "tickers": sorted(self.close),
        }


class Universe(Contract):
    as_of: date
    tickers: list[str]
    dropped: dict[str, str] = Field(default_factory=dict)  # ticker -> reason


# ---------------------------------------------------------------- 2. Regime


class RegimeAssessment(Contract):
    as_of: date
    trend: Literal["up", "down", "sideways"]
    volatility: Literal["low", "normal", "high"]
    risk_mode: Literal["risk_on", "risk_off"]
    confidence: float = Field(ge=0, le=1)
    evidence: dict[str, float]
    days_in_regime: int = 0
    model_version: str = "rules-v1"


# ---------------------------------------------------------------- 3. Alpha


class AlphaScore(Contract):
    ticker: str
    score: float
    rank: int
    features: dict[str, float]
    contributions: dict[str, float]


class AlphaScores(Contract):
    as_of: date
    model_version: str
    forecast_horizon_days: int
    oos_ic: float | None = None
    scores: list[AlphaScore]

    def by_ticker(self) -> dict[str, AlphaScore]:
        return {s.ticker: s for s in self.scores}


class ShortList(Contract):
    as_of: date
    tickers: list[str]
    from_holdings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- 4. Analyst reports

Lens = Literal["fundamental", "technical", "sentiment", "macro"]
View = Literal["bullish", "neutral", "bearish"]
RiskType = Literal[
    "earnings_date",
    "valuation",
    "leverage",
    "litigation",
    "crowding",
    "macro_shock",
    "momentum_reversal",
    "volatility",
    "liquidity",
    "regulatory",
    "news",
    "other",
]
MARKET = "MARKET"


class Metric(Contract):
    name: str
    value: float | str | None
    source: str  # the tool that computed it


class FlaggedRisk(Contract):
    type: RiskType
    description: str
    severity: Literal["low", "medium", "high"] = "medium"


class AnalystReport(Contract):
    report_id: str  # "<lens>:<ticker>", cited by later stages
    ticker: str  # or MARKET for the macro report
    lens: Lens
    run_date: date
    view: View
    confidence: float = Field(ge=0, le=1)
    summary: str
    key_metrics: list[Metric]
    risks: list[FlaggedRisk]
    sources: list[str]
    validated: bool = False
    validation_notes: list[str] = Field(default_factory=list)
    agent_version: str = ""


class AnalystReports(Contract):
    """The shared reports, written once in stage 4 and read-only afterwards."""

    run_date: date
    reports: tuple[AnalystReport, ...]

    def for_ticker(self, ticker: str) -> list[AnalystReport]:
        return [r for r in self.reports if r.ticker == ticker]

    def macro(self) -> AnalystReport | None:
        return next((r for r in self.reports if r.lens == "macro"), None)

    def ids(self) -> set[str]:
        return {r.report_id for r in self.reports}


# ---------------------------------------------------------------- 5. Proposal


class ProposedPosition(Contract):
    ticker: str
    weight: float = Field(ge=0, le=1)
    baseline_weight: float
    tilt_units: float
    thesis: str
    cited_reports: list[str]


class PortfolioProposal(Contract):
    as_of: date
    method: str
    positions: list[ProposedPosition]
    cash_weight: float

    def weights(self) -> dict[str, float]:
        return {p.ticker: p.weight for p in self.positions}


# ---------------------------------------------------------------- 6. Debate


class Argument(Contract):
    target: str  # a ticker or "PORTFOLIO"
    claim: str
    cited_reports: list[str]


class DebateSide(Contract):
    side: Literal["bull", "bear"]
    arguments: list[Argument]
    rejected_arguments: int = 0  # dropped by the citation guardrail


Action = Literal["keep", "trim", "add", "drop"]


class Verdict(Contract):
    ticker: str
    action: Action
    confidence: float = Field(ge=0, le=1)
    rationale: str


class DebateRecord(Contract):
    as_of: date
    bull: DebateSide
    bear: DebateSide
    bull_weakness: str
    bear_weakness: str
    verdicts: list[Verdict]
    portfolio_comment: str


# ---------------------------------------------------------------- 7. Adjustment


class WeightChange(Contract):
    ticker: str
    old_weight: float
    new_weight: float
    reason: str


class AdjustedPortfolio(Contract):
    as_of: date
    weights: dict[str, float]
    changes: list[WeightChange]
    attempt: int = 0  # 0 on the first pass, 1 after a risk rejection


# ---------------------------------------------------------------- 8. Risk


class LimitCheck(Contract):
    name: str
    value: float
    limit: float
    passed: bool
    action: str = ""


class RiskReviewFlag(Contract):
    ticker: str
    risk_type: RiskType
    scale: float = Field(ge=0, le=1)  # 0 is a veto, 1 means no change
    reason: str


class ApprovedPortfolio(Contract):
    as_of: date
    weights: dict[str, float]
    cash_weight: float
    checks: list[LimitCheck]
    review_flags: list[RiskReviewFlag]
    metrics: dict[str, float]


class RiskRejection(Contract):
    as_of: date
    breaches: list[LimitCheck]
    review_flags: list[RiskReviewFlag]
    message: str
    attempt: int


# ---------------------------------------------------------------- 9. Execution


class Holdings(Contract):
    cash: float
    shares: dict[str, float] = Field(default_factory=dict)


class Order(Contract):
    ticker: str
    side: Literal["buy", "sell"]
    shares: int
    est_price: float
    notional: float
    est_cost: float
    current_weight: float
    target_weight: float


class SkippedTrade(Contract):
    ticker: str
    reason: str
    target_weight: float
    current_weight: float


class RebalancePlan(Contract):
    as_of: date
    portfolio_value: float
    orders: list[Order]
    skipped: list[SkippedTrade]
    turnover: float
    est_total_cost: float
    target_weights: dict[str, float]
    status: Literal["pending_approval", "approved"] = "pending_approval"
    note: str = "Paper trading only. Nothing here is sent to a broker; the person places trades."


# ---------------------------------------------------------------- 10. Backtest


class BacktestReport(Contract):
    start: date
    end: date
    mode: Literal["quant", "full"]
    rebalances: int
    cagr: float
    volatility: float
    sharpe: float
    max_drawdown: float
    avg_turnover: float
    total_costs: float
    benchmark_cagr: float
    benchmark_sharpe: float
    per_regime_return: dict[str, float]
    equity_curve: dict[str, float]
    caveats: list[str]


class AgentEvalReport(Contract):
    rebalances: int
    analyst_hit_rate: dict[str, float]  # lens -> share of non-neutral views whose sign matched
    analyst_calls: dict[str, int]
    verdict_hit_rate: float | None = None
    note: str = ""


# ---------------------------------------------------------------- 11. Monitoring


class MonitoringReport(Contract):
    as_of: date
    plan_date: date
    portfolio_return: float
    benchmark_return: float
    drift: dict[str, float]
    max_drift: float
    alerts: list[str]
    summary: str = ""


class RunResult(Contract):
    run_id: str
    as_of: date
    status: Literal["planned", "rejected", "failed"]
    plan: RebalancePlan | None = None
    message: str = ""


# ================================================================ LLM output schemas
# What the model fills in. Code turns these into the contracts above.


class AnalystDraft(BaseModel):
    view: View
    confidence: float = Field(ge=0, le=1)
    summary: str
    cited_metrics: list[str] = Field(description="Names of evidence metrics the view rests on")
    cited_sources: list[str] = Field(description="Source ids from the evidence pack")
    risks: list[FlaggedRisk]


class ValidationVerdict(BaseModel):
    passed: bool
    issues: list[str]


class TiltDecision(BaseModel):
    ticker: str
    tilt_units: int = Field(ge=-2, le=2)
    thesis: str
    cited_reports: list[str]


class ManagerDecision(BaseModel):
    tilts: list[TiltDecision]


class DebateArguments(BaseModel):
    arguments: list[Argument]


class DevilsAdvocateNote(BaseModel):
    bull_weakness: str
    bear_weakness: str


class JudgeRuling(BaseModel):
    verdicts: list[Verdict]
    portfolio_comment: str


class RiskReview(BaseModel):
    flags: list[RiskReviewFlag]
    summary: str


class MonitorSummary(BaseModel):
    summary: str
