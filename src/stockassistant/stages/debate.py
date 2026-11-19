"""Stage 6, the adversarial debate: bull and bear in parallel, then the devil's advocate, then the judge.

The bull and the bear never see each other's arguments (separate calls, separate contexts).
Every argument must cite an analyst report; uncited arguments, or arguments about tickers
outside the proposal, are dropped by the citation guardrail before anyone else reads them.
The bear is asked to look for disagreement between lenses, concentration and regime mismatch,
and code hands it the lens disagreements and sector weights so it does not have to compute them.
References: baa4f ch. 8 (Part B), ch. 7; ml4t ch. 24.
"""

from __future__ import annotations

from typing import Any, Literal

from stockassistant.guardrails import valid_citations
from stockassistant.llm import LLMClient, load_skill
from stockassistant.schemas import (
    AnalystReports,
    DebateArguments,
    DebateRecord,
    DebateSide,
    DevilsAdvocateNote,
    JudgeRuling,
    MarketDataSnapshot,
    PortfolioProposal,
    Verdict,
)

PORTFOLIO = "PORTFOLIO"


def lens_disagreements(proposal: PortfolioProposal, reports: AnalystReports) -> list[dict[str, Any]]:
    out = []
    for p in proposal.positions:
        views = {r.lens: r.view for r in reports.for_ticker(p.ticker)}
        if "bullish" in views.values() and "bearish" in views.values():
            out.append({"ticker": p.ticker, "weight": p.weight, "views": views})
    return out


def sector_weights(proposal: PortfolioProposal, snapshot: MarketDataSnapshot) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in proposal.positions:
        sector = snapshot.sectors.get(p.ticker, "Unknown")
        out[sector] = round(out.get(sector, 0.0) + p.weight, 4)
    return out


def _payload(proposal: PortfolioProposal, reports: AnalystReports, snapshot: MarketDataSnapshot) -> dict[str, Any]:
    return {
        "proposal": proposal.model_dump(mode="json", exclude={"schema_version"}),
        "reports": [r.model_dump(mode="json", exclude={"schema_version", "agent_version"}) for r in reports.reports],
        "lens_disagreements": lens_disagreements(proposal, reports),
        "sector_weights": sector_weights(proposal, snapshot),
    }


def argue(
    side: Literal["bull", "bear"],
    proposal: PortfolioProposal,
    reports: AnalystReports,
    snapshot: MarketDataSnapshot,
    llm: LLMClient,
    settings: Any = None,
) -> DebateSide:
    spec = load_skill(f"{side}_agent", settings)
    raw = llm.structured(spec, _payload(proposal, reports, snapshot) | {"side": side}, DebateArguments)
    targets = {p.ticker for p in proposal.positions} | {PORTFOLIO}
    known = reports.ids()
    kept, rejected = [], 0
    for arg in raw.arguments:
        cited = valid_citations(arg.cited_reports, known)
        if arg.target not in targets or not cited:
            rejected += 1
            continue
        kept.append(arg.model_copy(update={"cited_reports": cited}))
    return DebateSide(side=side, arguments=kept, rejected_arguments=rejected)


def devils_advocate(
    bull: DebateSide, bear: DebateSide, proposal: PortfolioProposal, llm: LLMClient, settings: Any = None
) -> DevilsAdvocateNote:
    spec = load_skill("devils_advocate", settings)
    payload = {
        "proposal": proposal.model_dump(mode="json", exclude={"schema_version"}),
        "bull": bull.model_dump(mode="json"),
        "bear": bear.model_dump(mode="json"),
    }
    return llm.structured(spec, payload, DevilsAdvocateNote)


def judge(
    bull: DebateSide,
    bear: DebateSide,
    note: DevilsAdvocateNote,
    proposal: PortfolioProposal,
    reports: AnalystReports,
    snapshot: MarketDataSnapshot,
    llm: LLMClient,
    settings: Any = None,
) -> DebateRecord:
    spec = load_skill("judge", settings)
    payload = _payload(proposal, reports, snapshot) | {
        "bull": bull.model_dump(mode="json"),
        "bear": bear.model_dump(mode="json"),
        "devils_advocate": note.model_dump(mode="json"),
    }
    ruling = llm.structured(spec, payload, JudgeRuling)
    held = {p.ticker for p in proposal.positions}
    verdicts = {v.ticker: v for v in ruling.verdicts if v.ticker in held}
    for t in held - set(verdicts):  # the judge must rule on every position; default to keep
        verdicts[t] = Verdict(ticker=t, action="keep", confidence=0.0, rationale="No ruling given; kept unchanged.")
    return DebateRecord(
        as_of=proposal.as_of,
        bull=bull,
        bear=bear,
        bull_weakness=note.bull_weakness,
        bear_weakness=note.bear_weakness,
        verdicts=[verdicts[p.ticker] for p in proposal.positions],
        portfolio_comment=ruling.portfolio_comment,
    )
