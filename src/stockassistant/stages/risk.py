"""Stage 8, RiskOfficer: hard limits in code, then an LLM risk review that can only cut.

Hard limits come from config and are fixed for the run (``RiskLimits`` is frozen). Each
breach is fixed by clipping or scaling weights down; the excess goes to cash. Then the
review reads the analysts' ``risks`` fields, the macro report and code-computed facts
(earnings dates inside the holding period, lens disagreements) and may scale positions down
or veto them. Scale factors above 1 are ignored, so the review can never raise a weight.

If the result cannot satisfy the limits (for example too few holdings remain), the stage
returns a ``RiskRejection``; the graph sends it back to stage 7 once and stops after that.
References: ml4t ch. 19; baa4f ch. 8, ch. 12.
"""

from __future__ import annotations

from typing import Any

from stockassistant.config import RiskLimits
from stockassistant.guardrails import check_weights
from stockassistant.llm import LLMClient, load_skill
from stockassistant.quant import avg_dollar_volume, portfolio_stats
from stockassistant.schemas import (
    AdjustedPortfolio,
    AnalystReports,
    ApprovedPortfolio,
    LimitCheck,
    MarketDataSnapshot,
    RegimeAssessment,
    RiskRejection,
    RiskReview,
    RiskReviewFlag,
)

HOLDING_PERIOD_DAYS = 31
MIN_WEIGHT = 1e-4


def _scale(weights: dict[str, float], factor: float, names: list[str] | None = None) -> dict[str, float]:
    names = list(weights) if names is None else names
    return {t: (w * factor if t in names else w) for t, w in weights.items()}


def apply_hard_limits(
    weights: dict[str, float],
    snapshot: MarketDataSnapshot,
    regime: RegimeAssessment,
    limits: RiskLimits,
    portfolio_value: float,
) -> tuple[dict[str, float], list[LimitCheck]]:
    close = snapshot.close_frame().ffill()
    volume = snapshot.volume_frame()
    w = dict(weights)
    checks: list[LimitCheck] = []

    # Position size.
    worst = max(w.values(), default=0.0)
    if worst > limits.max_weight:
        w = {t: min(x, limits.max_weight) for t, x in w.items()}
    checks.append(
        LimitCheck(
            name="max_weight",
            value=worst,
            limit=limits.max_weight,
            passed=worst <= limits.max_weight,
            action="" if worst <= limits.max_weight else "clipped to the cap",
        )
    )

    # Sector exposure.
    sectors: dict[str, list[str]] = {}
    for t in w:
        sectors.setdefault(snapshot.sectors.get(t, "Unknown"), []).append(t)
    worst_sector = 0.0
    for names in sectors.values():
        exposure = sum(w[t] for t in names)
        worst_sector = max(worst_sector, exposure)
        if exposure > limits.max_sector:
            w = _scale(w, limits.max_sector / exposure, names)
    checks.append(
        LimitCheck(
            name="max_sector",
            value=worst_sector,
            limit=limits.max_sector,
            passed=worst_sector <= limits.max_sector,
            action="" if worst_sector <= limits.max_sector else "scaled the sector down",
        )
    )

    # Liquidity: position notional as a share of average daily dollar volume.
    worst_liq = 0.0
    for t in list(w):
        adv = avg_dollar_volume(close, volume, t)
        if adv > 0:
            share = w[t] * portfolio_value / adv
            worst_liq = max(worst_liq, share)
            if share > limits.max_adv_fraction:
                w[t] = limits.max_adv_fraction * adv / portfolio_value
    checks.append(
        LimitCheck(
            name="max_adv_fraction",
            value=worst_liq,
            limit=limits.max_adv_fraction,
            passed=worst_liq <= limits.max_adv_fraction,
            action="" if worst_liq <= limits.max_adv_fraction else "clipped to liquidity",
        )
    )

    # Regime-triggered exposure cap.
    gross = sum(w.values())
    if regime.risk_mode == "risk_off":
        ok = gross <= limits.risk_off_max_gross + 1e-9
        if not ok:
            w = _scale(w, limits.risk_off_max_gross / gross)
        checks.append(
            LimitCheck(
                name="risk_off_max_gross",
                value=gross,
                limit=limits.risk_off_max_gross,
                passed=ok,
                action="" if ok else "scaled gross exposure down",
            )
        )

    # Portfolio volatility, beta and trailing drawdown: scale gross exposure down when breached.
    for name, limit in (
        ("volatility", limits.max_volatility),
        ("beta", limits.max_beta),
        ("max_drawdown", limits.max_drawdown),
    ):
        value = portfolio_stats(w, close, snapshot.benchmark)[name]
        ok = value <= limit
        if not ok and value > 0:
            w = _scale(w, limit / value)
        checks.append(
            LimitCheck(
                name=f"max_{name}" if name != "max_drawdown" else name,
                value=value,
                limit=limit,
                passed=ok,
                action="" if ok else "scaled gross exposure down",
            )
        )
    return {t: x for t, x in w.items() if x > MIN_WEIGHT}, checks


def review_payload(
    weights: dict[str, float], reports: AnalystReports, snapshot: MarketDataSnapshot, regime: RegimeAssessment
) -> dict[str, Any]:
    positions = []
    for t, w in sorted(weights.items(), key=lambda kv: -kv[1]):
        f = snapshot.fundamentals.get(t)
        days_to_earnings = (f.next_earnings_date - snapshot.as_of).days if f and f.next_earnings_date else None
        views = {r.lens: r.view for r in reports.for_ticker(t)}
        positions.append(
            {
                "ticker": t,
                "weight": round(w, 4),
                "sector": snapshot.sectors.get(t, "Unknown"),
                "earnings_inside_holding_period": days_to_earnings is not None
                and 0 <= days_to_earnings <= HOLDING_PERIOD_DAYS,
                "views": views,
                "lenses_disagree": "bullish" in views.values() and "bearish" in views.values(),
                "flagged_risks": [
                    {"report_id": r.report_id, **x.model_dump(exclude={"schema_version"})}
                    for r in reports.for_ticker(t)
                    for x in r.risks
                ],
            }
        )
    macro = reports.macro()
    return {
        "regime": regime.model_dump(mode="json", exclude={"schema_version"}),
        "macro_report": macro.model_dump(mode="json", exclude={"schema_version"}) if macro else None,
        "positions": positions,
    }


def run(
    adjusted: AdjustedPortfolio,
    reports: AnalystReports | None,
    snapshot: MarketDataSnapshot,
    regime: RegimeAssessment,
    limits: RiskLimits,
    portfolio_value: float,
    llm: LLMClient | None,
    settings: Any = None,
) -> ApprovedPortfolio | RiskRejection:
    weights, checks = apply_hard_limits(adjusted.weights, snapshot, regime, limits, portfolio_value)

    flags: list[RiskReviewFlag] = []
    if llm is not None and reports is not None and weights:
        spec = load_skill("risk_officer", settings)
        review = llm.structured(spec, review_payload(weights, reports, snapshot, regime), RiskReview)
        for flag in review.flags:
            if flag.ticker in weights and flag.scale < 1:  # one-directional: can only cut
                flags.append(flag)
                weights[flag.ticker] *= flag.scale
        weights = {t: x for t, x in weights.items() if x > MIN_WEIGHT}

    holdings = len(weights)
    checks.append(
        LimitCheck(
            name="min_holdings", value=holdings, limit=limits.min_holdings, passed=holdings >= limits.min_holdings
        )
    )
    if holdings < limits.min_holdings:
        return RiskRejection(
            as_of=adjusted.as_of,
            breaches=[c for c in checks if c.name == "min_holdings"],
            review_flags=flags,
            message=f"only {holdings} holdings after limits and review; at least {limits.min_holdings} required",
            attempt=adjusted.attempt,
        )

    check_weights(weights)
    stats = portfolio_stats(weights, snapshot.close_frame().ffill(), snapshot.benchmark)
    return ApprovedPortfolio(
        as_of=adjusted.as_of,
        weights={t: round(x, 6) for t, x in weights.items()},
        cash_weight=round(1 - sum(weights.values()), 6),
        checks=checks,
        review_flags=flags,
        metrics={k: round(v, 5) for k, v in stats.items()},
    )
