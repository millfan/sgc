"""Stages 5 to 7: proposal, debate and adjustment."""

from stockassistant.schemas import (
    Argument,
    DebateArguments,
    DebateRecord,
    DebateSide,
    JudgeRuling,
    ManagerDecision,
    RiskRejection,
    RiskReviewFlag,
    TiltDecision,
    Verdict,
)
from stockassistant.stages import adjustment, debate, portfolio_manager
from tests.conftest import AS_OF, ScriptedLLM


def _proposal(stage1, stage2, stage3, reports, settings, llm=None, use_llm=True):
    snap, _ = stage1
    scores, short = stage3
    return portfolio_manager.run(snap, short, scores, stage2, reports, settings, llm or ScriptedLLM(), use_llm)


def test_baseline_proposal_respects_gross_and_position_count(stage1, stage2, stage3, settings):
    p = _proposal(stage1, stage2, stage3, None, settings, use_llm=False)
    gross = settings.portfolio.risk_off_gross if stage2.risk_mode == "risk_off" else settings.portfolio.risk_on_gross
    assert abs(sum(p.weights().values()) - gross) < 1e-4
    assert len(p.positions) <= settings.portfolio.max_positions
    assert all(pos.tilt_units == 0 for pos in p.positions)


def test_uncited_tilts_are_zeroed(stage1, stage2, stage3, reports, settings):
    _, short = stage3
    target = short.tickers[0]
    llm = ScriptedLLM(
        {
            "portfolio_manager": lambda p: ManagerDecision(
                tilts=[TiltDecision(ticker=target, tilt_units=2, thesis="trust me", cited_reports=["nonexistent:ID"])]
            )
        }
    )
    p = _proposal(stage1, stage2, stage3, reports, settings, llm)
    pos = {x.ticker: x for x in p.positions}
    assert target not in pos or pos[target].tilt_units == 0


def test_self_consistency_keeps_the_median_tilt(stage1, stage2, stage3, reports, settings):
    _, short = stage3
    target = short.tickers[-1]
    samples = iter([2, 2, -2])
    llm = ScriptedLLM(
        {
            "portfolio_manager": lambda p: ManagerDecision(
                tilts=[
                    TiltDecision(
                        ticker=target, tilt_units=next(samples), thesis="t", cited_reports=[f"technical:{target}"]
                    )
                ]
            )
        }
    )
    p = _proposal(stage1, stage2, stage3, reports, settings, llm)
    assert sum(1 for role in llm.roles() if role == "portfolio_manager") == settings.portfolio.self_consistency_samples
    pos = {x.ticker: x for x in p.positions}
    assert pos[target].tilt_units == 2 and pos[target].cited_reports == [f"technical:{target}"]


def test_debate_drops_uncited_and_off_portfolio_arguments(stage1, stage2, stage3, reports, settings):
    snap, _ = stage1
    p = _proposal(stage1, stage2, stage3, reports, settings)
    held = p.positions[0].ticker
    llm = ScriptedLLM(
        {
            "bull_agent": lambda _: DebateArguments(
                arguments=[
                    Argument(target=held, claim="good", cited_reports=[f"technical:{held}"]),
                    Argument(target=held, claim="no source", cited_reports=[]),
                    Argument(target="ZZZZ", claim="not in the proposal", cited_reports=[f"technical:{held}"]),
                ]
            )
        }
    )
    side = debate.argue("bull", p, reports, snap, llm, settings)
    assert len(side.arguments) == 1 and side.rejected_arguments == 2


def test_bull_and_bear_do_not_see_each_other(stage1, stage2, stage3, reports, settings):
    snap, _ = stage1
    p = _proposal(stage1, stage2, stage3, reports, settings)
    llm = ScriptedLLM()
    debate.argue("bull", p, reports, snap, llm, settings)
    debate.argue("bear", p, reports, snap, llm, settings)
    for _, payload in llm.calls:
        assert "bull" not in payload and "bear" not in payload


def test_judge_rules_on_every_position(stage1, stage2, stage3, reports, settings):
    snap, _ = stage1
    p = _proposal(stage1, stage2, stage3, reports, settings)
    first = p.positions[0].ticker
    llm = ScriptedLLM(
        {
            "judge": lambda _: JudgeRuling(
                portfolio_comment="ok",
                verdicts=[
                    Verdict(ticker=first, action="trim", confidence=0.8, rationale="r"),
                    Verdict(ticker="ZZZZ", action="add", confidence=1.0, rationale="not held"),
                ],
            )
        }
    )
    empty = DebateSide(side="bull", arguments=[])
    rec = debate.judge(
        empty,
        empty.model_copy(update={"side": "bear"}),
        debate.DevilsAdvocateNote(bull_weakness="a", bear_weakness="b"),
        p,
        reports,
        snap,
        llm,
        settings,
    )
    assert [v.ticker for v in rec.verdicts] == [x.ticker for x in p.positions]
    assert rec.verdicts[0].action == "trim" and all(v.action == "keep" for v in rec.verdicts[1:])


def _record(p, verdicts):
    empty = DebateSide(side="bull", arguments=[])
    return DebateRecord(
        as_of=AS_OF,
        bull=empty,
        bear=empty.model_copy(update={"side": "bear"}),
        bull_weakness="",
        bear_weakness="",
        verdicts=verdicts,
        portfolio_comment="",
    )


def test_adjustment_rules_are_bounded(stage1, stage2, stage3, settings):
    p = _proposal(stage1, stage2, stage3, None, settings, use_llm=False)
    a, b, c = (x.ticker for x in p.positions[:3])
    rec = _record(
        p,
        [
            Verdict(ticker=a, action="drop", confidence=1, rationale=""),
            Verdict(ticker=b, action="trim", confidence=1, rationale=""),
            Verdict(ticker=c, action="add", confidence=1, rationale=""),
        ],
    )
    adj = adjustment.run(p, rec, settings)
    w = p.weights()
    assert a not in adj.weights
    assert w[b] - adj.weights[b] <= settings.debate.max_change + 1e-9 and adj.weights[b] < w[b]
    assert sum(adj.weights.values()) <= sum(w.values()) + 1e-9
    assert len(adj.changes) == 3 and set(adj.weights) <= set(w)


def test_retry_softens_drops_and_applies_risk_cuts(stage1, stage2, stage3, settings):
    p = _proposal(stage1, stage2, stage3, None, settings, use_llm=False)
    a, b = p.positions[0].ticker, p.positions[1].ticker
    rec = _record(p, [Verdict(ticker=a, action="drop", confidence=1, rationale="")])
    rej = RiskRejection(
        as_of=AS_OF,
        breaches=[],
        message="too few",
        attempt=0,
        review_flags=[RiskReviewFlag(ticker=b, risk_type="earnings_date", scale=0.5, reason="earnings")],
    )
    adj = adjustment.run(p, rec, settings, rejection=rej)
    assert a in adj.weights and adj.attempt == 1
    assert adj.weights[b] <= p.weights()[b] * 0.5 + 1e-6
