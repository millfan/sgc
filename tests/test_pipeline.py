"""End-to-end runs of the LangGraph pipeline with a mocked LLM."""

from stockassistant.graph import Deps, run_pipeline
from stockassistant.memory.journal import MemoryJournal
from stockassistant.schemas import Holdings, JudgeRuling, RiskReview, RiskReviewFlag, Verdict
from tests.conftest import AS_OF, ScriptedLLM

ROLES = {
    "fundamental_analyst",
    "technical_analyst",
    "sentiment_analyst",
    "macro_strategist",
    "report_validator",
    "portfolio_manager",
    "bull_agent",
    "bear_agent",
    "devils_advocate",
    "judge",
    "risk_officer",
}


def _deps(settings, provider, llm, holdings=None):
    return Deps(
        settings=settings,
        provider=provider,
        llm=llm,
        journal=MemoryJournal(),
        holdings=holdings or Holdings(cash=settings.run.portfolio_value),
    )


def test_full_pipeline_produces_a_plan_and_journals_every_stage(settings, provider):
    llm = ScriptedLLM()
    deps = _deps(settings, provider, llm)
    result = run_pipeline(AS_OF, deps, run_id="t1")
    assert result.status == "planned" and result.plan is not None and result.plan.orders
    assert set(llm.roles()) == ROLES
    stages = {e.stage for e in deps.journal.entries}
    assert {
        "snapshot",
        "universe",
        "regime",
        "alpha",
        "short_list",
        "reports",
        "proposal",
        "bull",
        "bear",
        "devils_note",
        "debate",
        "adjusted",
        "approved",
        "plan",
        "llm_usage",
        "run_result",
    } <= stages
    assert all(e.run_id == "t1" and e.known_at == AS_OF for e in deps.journal.entries)
    weights = result.plan.target_weights
    assert sum(weights.values()) <= 1 + 1e-9
    assert max(weights.values()) <= settings.risk.max_weight + 1e-9


def test_risk_rejection_loops_back_once_then_stops(settings, provider):
    veto_all = ScriptedLLM(
        {
            "risk_officer": lambda p: RiskReview(
                summary="veto",
                flags=[
                    RiskReviewFlag(ticker=pos["ticker"], risk_type="other", scale=0.0, reason="veto")
                    for pos in p["positions"]
                ],
            )
        }
    )
    deps = _deps(settings, provider, veto_all)
    result = run_pipeline(AS_OF, deps, run_id="t2")
    assert result.status == "rejected" and result.plan is None
    attempts = [e.payload["attempt"] for e in deps.journal.entries if e.stage == "adjusted"]
    assert attempts == [0, 1]  # one retry through stage 7, then the run stops
    assert [e.payload["attempt"] for e in deps.journal.entries if e.stage == "rejection"] == [0, 1]


def test_rejection_then_recovery_when_the_retry_softens_drops(settings, provider):
    """Judge drops plus risk vetoes leave too few names; the retry turns drops into trims and passes."""
    dropped: list[str] = []
    calls = {"n": 0}

    def judge(p):
        names = [x["ticker"] for x in p["proposal"]["positions"]]
        dropped.extend(names[:4])
        return JudgeRuling(
            portfolio_comment="",
            verdicts=[
                Verdict(ticker=t, action="drop" if t in dropped else "keep", confidence=0.9, rationale="r")
                for t in names
            ],
        )

    def risk_officer(p):
        calls["n"] += 1
        held = [x["ticker"] for x in p["positions"] if x["ticker"] not in dropped]
        if calls["n"] > 1:
            return RiskReview(summary="fine", flags=[])
        return RiskReview(
            summary="", flags=[RiskReviewFlag(ticker=t, risk_type="other", scale=0.0, reason="veto") for t in held[:3]]
        )

    deps = _deps(settings, provider, ScriptedLLM({"judge": judge, "risk_officer": risk_officer}))
    result = run_pipeline(AS_OF, deps)
    assert calls["n"] == 2
    assert result.status == "planned"
    assert all(t in result.plan.target_weights for t in dropped)  # softened to trims on the retry


def test_existing_holdings_are_reviewed_and_can_be_sold(settings, provider, stage1):
    snap, uni = stage1
    held = uni.tickers[-1]
    holdings = Holdings(cash=50_000, shares={held: 100})
    deps = _deps(settings, provider, ScriptedLLM(), holdings)
    result = run_pipeline(AS_OF, deps)
    short = next(e for e in deps.journal.entries if e.stage == "short_list").payload
    assert held in short["tickers"]
    if held not in result.plan.target_weights:
        assert any(o.ticker == held and o.side == "sell" for o in result.plan.orders)


def test_llm_call_budget_is_enforced(settings, provider):
    import pytest

    from stockassistant.llm import BudgetExceeded

    llm = ScriptedLLM()
    llm.usage.max_calls = 5
    with pytest.raises(BudgetExceeded):
        run_pipeline(AS_OF, _deps(settings, provider, llm))
