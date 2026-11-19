"""A deterministic, rule-based stand-in for the LLM.

It answers every agent schema from the same evidence payload the real model gets, using
simple transparent rules. It makes no network calls, so the whole pipeline can run offline
(``llm.provider = "offline"``), in CI and in tests. Its outputs are a smoke-test baseline,
not investment judgment.
"""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel

from stockassistant.llm.client import UsageLog
from stockassistant.llm.skills import AgentSpec
from stockassistant.schemas import (
    AnalystDraft,
    Argument,
    DebateArguments,
    DevilsAdvocateNote,
    FlaggedRisk,
    JudgeRuling,
    ManagerDecision,
    MonitorSummary,
    RiskReview,
    RiskReviewFlag,
    TiltDecision,
    ValidationVerdict,
    Verdict,
)

T = TypeVar("T", bound=BaseModel)
VIEW_SCORE = {"bullish": 1, "neutral": 0, "bearish": -1}


def _num(metrics: dict[str, Any], key: str) -> float | None:
    v = metrics.get(key)
    return float(v) if isinstance(v, int | float) else None


def _view(score: float) -> tuple[str, float]:
    if score > 0.5:
        return "bullish", min(0.5 + score / 6, 0.85)
    if score < -0.5:
        return "bearish", min(0.5 + abs(score) / 6, 0.85)
    return "neutral", 0.4


def analyst_draft(p: dict[str, Any]) -> AnalystDraft:
    m, lens = p["metrics"], p["lens"]
    score, cited, risks = 0.0, [], []
    if lens == "technical":
        for key, w in (("return_3m", 8), ("price_vs_ma200", 6), ("alpha_score", 3)):
            v = _num(m, key)
            if v is not None:
                score += w * v
                cited.append(key)
        vol = _num(m, "volatility_3m")
        if vol is not None and vol > 0.4:
            risks.append(FlaggedRisk(type="volatility", description=f"3-month volatility {vol:.0%}", severity="medium"))
        rsi = _num(m, "rsi_14")
        if rsi is not None and rsi > 75:
            risks.append(FlaggedRisk(type="momentum_reversal", description=f"RSI {rsi:.0f}", severity="low"))
    elif lens == "fundamental":
        if m.get("fundamentals_available") == "no":
            return AnalystDraft(
                view="neutral",
                confidence=0.2,
                summary="No point-in-time fundamentals available.",
                cited_metrics=["fundamentals_available", "sector"],
                cited_sources=[],
                risks=[],
            )
        for key, w in (("return_on_equity", 4), ("revenue_growth", 5), ("earnings_yield", 20), ("pe_vs_sector", -1)):
            v = _num(m, key)
            if v is not None:
                score += w * v
                cited.append(key)
        score -= 0.8
        de = _num(m, "debt_to_equity")
        if de is not None and de > 2:
            risks.append(FlaggedRisk(type="leverage", description=f"debt to equity {de:.1f}", severity="medium"))
            cited.append("debt_to_equity")
        days = _num(m, "days_to_earnings")
        if days is not None and 0 <= days <= 31:
            risks.append(
                FlaggedRisk(type="earnings_date", description=f"earnings in {days:.0f} days", severity="medium")
            )
            cited.append("days_to_earnings")
    elif lens == "sentiment":
        tones = [s["text"] for s in p["sources"]]
        score = sum(1.0 for t in tones if "beats" in t or "grows" in t) - sum(
            1.0 for t in tones if "pressure" in t or "cut" in t
        )
        cited = ["headline_count"]
        if not tones:
            risks.append(FlaggedRisk(type="news", description="no recent headlines", severity="low"))
    else:  # macro
        mode = m.get("regime_risk_mode")
        score = 1.0 if mode == "risk_on" else -1.0
        score += 0.5 if m.get("regime_trend") == "up" else (-0.5 if m.get("regime_trend") == "down" else 0.0)
        cited = ["regime_risk_mode", "regime_trend", "regime_volatility"]
        if m.get("regime_volatility") == "high":
            risks.append(FlaggedRisk(type="macro_shock", description="high market volatility", severity="high"))
    view, conf = _view(score)
    return AnalystDraft(
        view=view,  # type: ignore[arg-type]
        confidence=round(conf, 2),
        summary=f"Offline {lens} rule score {score:+.2f} gives a {view} view.",
        cited_metrics=cited,
        cited_sources=[s["id"] for s in p["sources"]][:3],
        risks=risks,
    )


def manager_decision(p: dict[str, Any]) -> ManagerDecision:
    tilts = []
    for c in p["candidates"]:
        net = sum(VIEW_SCORE[r["view"]] * r["confidence"] for r in c["reports"])
        units = max(-2, min(2, round(net * 1.5)))
        tilts.append(
            TiltDecision(
                ticker=c["ticker"],
                tilt_units=units,
                thesis=f"Net analyst view {net:+.2f} across {len(c['reports'])} reports.",
                cited_reports=[r["report_id"] for r in c["reports"]],
            )
        )
    return ManagerDecision(tilts=tilts)


def debate_arguments(p: dict[str, Any]) -> DebateArguments:
    side = p["side"]
    want = "bullish" if side == "bull" else "bearish"
    args = []
    for pos in p["proposal"]["positions"]:
        hits = [r for r in p["reports"] if r["ticker"] == pos["ticker"] and r["view"] == want]
        if hits:
            args.append(
                Argument(
                    target=pos["ticker"],
                    claim=f"{len(hits)} {want} report(s) on {pos['ticker']}.",
                    cited_reports=[r["report_id"] for r in hits],
                )
            )
    if side == "bear":
        for d in p["lens_disagreements"]:
            args.append(
                Argument(
                    target=d["ticker"],
                    claim="The lenses disagree on this position.",
                    cited_reports=[f"{lens}:{d['ticker']}" for lens in d["views"]],
                )
            )
    return DebateArguments(arguments=args)


def judge_ruling(p: dict[str, Any]) -> JudgeRuling:
    verdicts = []
    for pos in p["proposal"]["positions"]:
        t = pos["ticker"]
        bull = sum(1 for a in p["bull"]["arguments"] if a["target"] == t)
        bear = sum(1 for a in p["bear"]["arguments"] if a["target"] == t)
        views = [r["view"] for r in p["reports"] if r["ticker"] == t]
        if views and all(v == "bearish" for v in views):
            action, conf = "drop", 0.7
        elif bear > bull:
            action, conf = "trim", 0.6
        elif bull > bear + 1:
            action, conf = "add", 0.5
        else:
            action, conf = "keep", 0.5
        verdicts.append(
            Verdict(
                ticker=t,
                action=action,
                confidence=conf,  # type: ignore[arg-type]
                rationale=f"{bull} bull vs {bear} bear arguments.",
            )
        )
    return JudgeRuling(verdicts=verdicts, portfolio_comment="Offline ruling by argument count.")


def risk_review(p: dict[str, Any]) -> RiskReview:
    flags = []
    for pos in p["positions"]:
        if pos["earnings_inside_holding_period"] and pos["weight"] > 0.1:
            flags.append(
                RiskReviewFlag(
                    ticker=pos["ticker"],
                    risk_type="earnings_date",
                    scale=0.8,
                    reason="large position with earnings inside the holding period",
                )
            )
        elif pos["lenses_disagree"] and pos["weight"] > 0.12:
            flags.append(
                RiskReviewFlag(
                    ticker=pos["ticker"],
                    risk_type="other",
                    scale=0.9,
                    reason="analysts disagree sharply on a large position",
                )
            )
    return RiskReview(flags=flags, summary=f"Offline review flagged {len(flags)} position(s).")


class OfflineLLM:
    def __init__(self, max_calls: int = 10_000) -> None:
        self.usage = UsageLog(max_calls)

    def structured(self, spec: AgentSpec, payload: dict[str, Any], schema: type[T], sample: int = 0) -> T:
        self.usage.reserve(spec.name)
        handlers: dict[type[BaseModel], Any] = {
            AnalystDraft: analyst_draft,
            ValidationVerdict: lambda p: ValidationVerdict(passed=bool(p["report"]["key_metrics"]), issues=[]),
            ManagerDecision: manager_decision,
            DebateArguments: debate_arguments,
            DevilsAdvocateNote: lambda p: DevilsAdvocateNote(
                bull_weakness="Bull arguments lean on recent momentum, which can reverse.",
                bear_weakness="Bear arguments may overweight single-lens disagreements.",
            ),
            JudgeRuling: judge_ruling,
            RiskReview: risk_review,
            MonitorSummary: lambda p: MonitorSummary(summary="Offline monitor summary: see the numbers above."),
        }
        if schema not in handlers:
            raise NotImplementedError(f"offline LLM has no rule for {schema.__name__}")
        return handlers[schema](payload)
