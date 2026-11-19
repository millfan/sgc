"""Stage 4, the analyst team: four lenses plus a validator, publishing the shared ``AnalystReports``.

Each analyst gets an evidence pack built by code (its "tools"): metrics with the name of the
tool that computed them, and source records it can cite. The model returns a view, a short
thesis, the metric names and source ids it relied on, and the risks it sees. Code then
builds the report, keeping only metrics and sources that exist (citation guardrail).

The ``ReportValidator`` checks each report in two steps, code first and then an LLM judge,
and sends a weak report back to its analyst once with the issues found (evaluator-optimizer).

Analysts run in parallel across tickers and lenses with a bounded thread pool.
References: baa4f ch. 8 (four lenses), ch. 4, ch. 5, ch. 10; ml4t ch. 10, ch. 22.
"""

from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from stockassistant.guardrails import looks_like_injection, valid_citations
from stockassistant.llm import AgentSpec, LLMClient, load_skill
from stockassistant.quant import technical_metrics
from stockassistant.schemas import (
    MARKET,
    AlphaScores,
    AnalystDraft,
    AnalystReport,
    AnalystReports,
    Lens,
    MarketDataSnapshot,
    Metric,
    RegimeAssessment,
    ShortList,
    ValidationVerdict,
)

log = logging.getLogger(__name__)
ROLE_BY_LENS: dict[str, str] = {
    "fundamental": "fundamental_analyst",
    "technical": "technical_analyst",
    "sentiment": "sentiment_analyst",
    "macro": "macro_strategist",
}
MAX_WORKERS = 8


@dataclass
class Evidence:
    """What one analyst sees: metrics (name -> (value, tool)) and citable sources (id -> text)."""

    ticker: str
    lens: Lens
    run_date: date
    metrics: dict[str, tuple[Any, str]]
    sources: dict[str, str]
    context: dict[str, Any] = field(default_factory=dict)
    screened_out: list[str] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "lens": self.lens,
            "run_date": self.run_date.isoformat(),
            "metrics": {k: _round(v) for k, (v, _) in self.metrics.items()},
            "sources": [{"id": k, "text": v} for k, v in self.sources.items()],
            "context": self.context,
        }


def _round(v: Any) -> Any:
    if isinstance(v, float):
        return None if math.isnan(v) else round(v, 4)
    return v


# ------------------------------------------------------------------ evidence tools


def fundamental_evidence(ticker: str, snapshot: MarketDataSnapshot) -> Evidence:
    tool = "fundamentals_tool"
    f = snapshot.fundamentals.get(ticker)
    sector = snapshot.sectors.get(ticker, "Unknown")
    metrics: dict[str, tuple[Any, str]] = {"sector": (sector, tool)}
    sources: dict[str, str] = {}
    if f is None:
        metrics["fundamentals_available"] = ("no", tool)
        return Evidence(
            ticker,
            "fundamental",
            snapshot.as_of,
            metrics,
            sources,
            context={"note": "No point-in-time fundamentals for this date. Say so; do not guess."},
        )
    peers = [
        snapshot.fundamentals[t].pe_ratio
        for t, s in snapshot.sectors.items()
        if s == sector and t in snapshot.fundamentals and snapshot.fundamentals[t].pe_ratio
    ]
    peer_pe = float(np.median([p for p in peers if p is not None])) if peers else float("nan")
    for name in (
        "market_cap",
        "pe_ratio",
        "price_to_book",
        "return_on_equity",
        "debt_to_equity",
        "profit_margin",
        "revenue_growth",
        "dividend_yield",
    ):
        metrics[name] = (getattr(f, name), tool)
    metrics["earnings_yield"] = ((1 / f.pe_ratio) if f.pe_ratio else None, "valuation_tool")
    metrics["sector_median_pe"] = (peer_pe, "peer_multiples_tool")
    metrics["pe_vs_sector"] = (
        (f.pe_ratio / peer_pe - 1) if f.pe_ratio and peer_pe == peer_pe else None,
        "peer_multiples_tool",
    )
    if f.next_earnings_date:
        metrics["days_to_earnings"] = ((f.next_earnings_date - snapshot.as_of).days, "calendar_tool")
    sources[f"fundamentals:{ticker}:{snapshot.as_of}"] = f"Company fundamentals for {ticker} as of {snapshot.as_of}"
    return Evidence(ticker, "fundamental", snapshot.as_of, metrics, sources)


def technical_evidence(ticker: str, snapshot: MarketDataSnapshot, alpha: AlphaScores, close: pd.DataFrame) -> Evidence:
    tm = technical_metrics(close[ticker], close[snapshot.benchmark])
    metrics: dict[str, tuple[Any, str]] = {k: (v, "price_tool") for k, v in tm.items()}
    score = alpha.by_ticker().get(ticker)
    if score:
        metrics["alpha_score"] = (score.score, "alpha_model")
        metrics["alpha_rank"] = (score.rank, "alpha_model")
        for k, v in score.features.items():
            metrics[f"factor_{k}"] = (v, "alpha_model")
    sources = {
        f"prices:{ticker}:{snapshot.as_of}": f"Adjusted daily prices for {ticker} up to {snapshot.as_of}",
        f"alpha:{alpha.model_version}:{snapshot.as_of}": f"Alpha model {alpha.model_version} scores",
    }
    return Evidence(ticker, "technical", snapshot.as_of, metrics, sources)


def sentiment_evidence(ticker: str, snapshot: MarketDataSnapshot) -> Evidence:
    items = [n for n in snapshot.news.get(ticker, []) if n.published <= snapshot.as_of]
    clean, screened = [], []
    for n in items:
        (screened if looks_like_injection(n.title) else clean).append(n)
    sources = {n.id: f"[{n.published}] {n.source}: {n.title}" for n in clean}
    latest = max((n.published for n in clean), default=None)
    metrics: dict[str, tuple[Any, str]] = {
        "headline_count": (len(clean), "news_search_tool"),
        "days_since_latest_headline": ((snapshot.as_of - latest).days if latest else None, "news_search_tool"),
        "headlines_screened_out": (len(screened), "injection_guardrail"),
    }
    return Evidence(ticker, "sentiment", snapshot.as_of, metrics, sources, screened_out=[n.id for n in screened])


def macro_evidence(
    snapshot: MarketDataSnapshot, regime: RegimeAssessment, members: list[str], close: pd.DataFrame
) -> Evidence:
    metrics: dict[str, tuple[Any, str]] = {f"regime_{k}": (v, "regime_model") for k, v in regime.evidence.items()}
    metrics["regime_trend"] = (regime.trend, "regime_model")
    metrics["regime_volatility"] = (regime.volatility, "regime_model")
    metrics["regime_risk_mode"] = (regime.risk_mode, "regime_model")
    metrics["regime_confidence"] = (regime.confidence, "regime_model")
    metrics["days_in_regime"] = (regime.days_in_regime, "journal")
    b = close[snapshot.benchmark]
    metrics["benchmark_return_3m"] = (float(b.iloc[-1] / b.iloc[-64] - 1), "price_tool")
    sector_ret: dict[str, list[float]] = {}
    for t in members:
        s = close[t]
        if len(s) > 64:
            sector_ret.setdefault(snapshot.sectors.get(t, "Unknown"), []).append(float(s.iloc[-1] / s.iloc[-64] - 1))
    for sector, rets in sector_ret.items():
        metrics[f"sector_return_3m_{sector.replace(' ', '_')}"] = (float(np.mean(rets)), "price_tool")
    sources = {
        f"regime:{regime.model_version}:{snapshot.as_of}": "Rule-based regime model output",
        f"prices:{snapshot.benchmark}:{snapshot.as_of}": f"Benchmark {snapshot.benchmark} prices",
    }
    return Evidence(MARKET, "macro", snapshot.as_of, metrics, sources)


# ------------------------------------------------------------------ agents


def _build_report(ev: Evidence, draft: AnalystDraft, spec: AgentSpec) -> tuple[AnalystReport, list[str]]:
    notes: list[str] = []
    cited_metrics = valid_citations(draft.cited_metrics, set(ev.metrics))
    if len(cited_metrics) < len(set(draft.cited_metrics)):
        notes.append("dropped citations of metrics that no tool produced")
    sources = valid_citations(draft.cited_sources, set(ev.sources))
    if len(sources) < len(set(draft.cited_sources)):
        notes.append("dropped citations of unknown sources")
    if ev.screened_out:
        notes.append(f"{len(ev.screened_out)} source(s) removed by the injection guardrail")
    metrics = [Metric(name=m, value=_round(ev.metrics[m][0]), source=ev.metrics[m][1]) for m in cited_metrics]
    report = AnalystReport(
        report_id=f"{ev.lens}:{ev.ticker}",
        ticker=ev.ticker,
        lens=ev.lens,
        run_date=ev.run_date,
        view=draft.view,
        confidence=draft.confidence,
        summary=draft.summary.strip(),
        key_metrics=metrics,
        risks=draft.risks,
        sources=sources,
        agent_version=f"{spec.name}@{spec.version}",
    )
    return report, notes


def code_checks(report: AnalystReport, ev: Evidence) -> list[str]:
    issues = []
    if not report.key_metrics:
        issues.append("the view cites no tool-computed metric")
    if not report.sources:
        issues.append("the report cites no source")
    if not report.summary:
        issues.append("empty summary")
    if report.view != "neutral" and report.confidence > 0.9 and len(report.key_metrics) < 2:
        issues.append("very high confidence resting on fewer than two metrics")
    return issues


def _analyse(ev: Evidence, llm: LLMClient, settings: Any, validator: AgentSpec) -> AnalystReport:
    spec = load_skill(ROLE_BY_LENS[ev.lens], settings)
    payload = ev.payload()
    report, notes = _build_report(ev, llm.structured(spec, payload, AnalystDraft), spec)
    for attempt in range(2):
        issues = code_checks(report, ev)
        if not issues:
            verdict = llm.structured(
                validator, {"evidence": payload, "report": report.model_dump(mode="json")}, ValidationVerdict
            )
            issues = [] if verdict.passed else (verdict.issues or ["validator rejected the report"])
        if not issues:
            return report.model_copy(update={"validated": True, "validation_notes": notes})
        if attempt == 0:  # one revision with the validator's feedback
            report, notes = _build_report(
                ev, llm.structured(spec, payload | {"validator_feedback": issues}, AnalystDraft), spec
            )
        else:
            notes = [*notes, *issues]
    # Still weak after one revision: keep it, but unvalidated and with confidence halved.
    return report.model_copy(
        update={"validated": False, "confidence": report.confidence / 2, "validation_notes": notes}
    )


def run(
    snapshot: MarketDataSnapshot,
    short_list: ShortList,
    alpha: AlphaScores,
    regime: RegimeAssessment,
    llm: LLMClient,
    settings: Any = None,
) -> AnalystReports:
    close = snapshot.close_frame().ffill()
    evidence: list[Evidence] = [macro_evidence(snapshot, regime, short_list.tickers, close)]
    for t in short_list.tickers:
        evidence.append(fundamental_evidence(t, snapshot))
        evidence.append(technical_evidence(t, snapshot, alpha, close))
        evidence.append(sentiment_evidence(t, snapshot))
    validator = load_skill("report_validator", settings)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        reports = list(pool.map(lambda ev: _analyse(ev, llm, settings, validator), evidence))
    return AnalystReports(run_date=snapshot.as_of, reports=tuple(reports))
