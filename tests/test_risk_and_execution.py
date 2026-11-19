from stockassistant.config import ExecutionConfig
from stockassistant.schemas import (
    AdjustedPortfolio,
    ApprovedPortfolio,
    Holdings,
    RiskRejection,
    RiskReview,
    RiskReviewFlag,
)
from stockassistant.stages import execution, risk
from tests.conftest import AS_OF, ScriptedLLM


def _adjusted(weights):
    return AdjustedPortfolio(as_of=AS_OF, weights=weights, changes=[])


def test_hard_limits_clip_position_and_sector(stage1, stage2, settings):
    snap, uni = stage1
    names = uni.tickers[:6]
    weights = {names[0]: 0.5, **{t: 0.1 for t in names[1:]}}
    out = risk.run(_adjusted(weights), None, snap, stage2, settings.risk, 100_000, None)
    assert isinstance(out, ApprovedPortfolio)
    assert max(out.weights.values()) <= settings.risk.max_weight + 1e-9
    by_sector: dict[str, float] = {}
    for t, w in out.weights.items():
        by_sector[snap.sectors[t]] = by_sector.get(snap.sectors[t], 0) + w
    assert max(by_sector.values()) <= settings.risk.max_sector + 1e-9
    assert out.cash_weight >= 0 and all(w <= weights[t] + 1e-9 for t, w in out.weights.items())
    assert next(c for c in out.checks if c.name == "max_weight").action == "clipped to the cap"


def test_review_can_only_cut(stage1, stage2, reports, settings):
    snap, uni = stage1
    names = uni.tickers[:6]
    weights = {t: 0.1 for t in names}
    llm = ScriptedLLM(
        {
            "risk_officer": lambda p: RiskReview(
                summary="s",
                flags=[
                    RiskReviewFlag(ticker=names[0], risk_type="litigation", scale=0.0, reason="veto"),
                    RiskReviewFlag(ticker=names[1], risk_type="other", scale=1.0, reason="no change"),
                    RiskReviewFlag(ticker="NOT_HELD", risk_type="other", scale=0.5, reason="ignored"),
                ],
            )
        }
    )
    out = risk.run(_adjusted(weights), reports, snap, stage2, settings.risk, 100_000, llm)
    assert isinstance(out, ApprovedPortfolio)
    assert names[0] not in out.weights
    assert all(out.weights[t] <= weights[t] + 1e-9 for t in out.weights)
    assert [f.ticker for f in out.review_flags] == [names[0]]


def test_too_few_holdings_is_a_rejection(stage1, stage2, settings):
    snap, uni = stage1
    out = risk.run(_adjusted({uni.tickers[0]: 0.1, uni.tickers[1]: 0.1}), None, snap, stage2, settings.risk, 1e5, None)
    assert isinstance(out, RiskRejection) and out.breaches[0].name == "min_holdings"


def test_risk_limits_are_frozen(settings):
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        settings.risk.max_weight = 1.0  # type: ignore[misc]


def _approved(weights):
    return ApprovedPortfolio(
        as_of=AS_OF, weights=weights, cash_weight=1 - sum(weights.values()), checks=[], review_flags=[], metrics={}
    )


def test_execution_plan_from_cash(stage1):
    snap, uni = stage1
    weights = {t: 0.1 for t in uni.tickers[:8]}
    plan = execution.run(_approved(weights), Holdings(cash=100_000), snap, ExecutionConfig(max_turnover=1.0))
    assert {o.ticker for o in plan.orders} == set(weights)
    assert all(o.side == "buy" and o.est_cost > 0 for o in plan.orders)
    assert abs(sum(o.notional for o in plan.orders) - 80_000) < 2_000
    assert plan.status == "pending_approval" and "Paper trading" in plan.note


def test_turnover_cap_band_and_sells_first(stage1):
    snap, uni = stage1
    a, b, c = uni.tickers[:3]
    price = {t: snap.close[t][-1] for t in (a, b, c)}
    holdings = Holdings(cash=20_000, shares={a: 40_000 / price[a], b: 40_000 / price[b]})
    value = 100_000
    target = {b: holdings.shares[b] * price[b] / value + 0.001, c: 0.4}  # b inside the band, a sold, c bought
    plan = execution.run(_approved(target), holdings, snap, ExecutionConfig(max_turnover=0.2))
    assert plan.orders[0].side == "sell"
    assert any(s.ticker == b and "band" in s.reason for s in plan.skipped)
    assert plan.turnover <= 0.2 + 0.01


def test_costly_trades_are_skipped(stage1):
    snap, uni = stage1
    plan = execution.run(
        _approved({uni.tickers[0]: 0.1}), Holdings(cash=100_000), snap, ExecutionConfig(max_cost_bps=0.1)
    )
    assert not plan.orders and "cost" in plan.skipped[0].reason
