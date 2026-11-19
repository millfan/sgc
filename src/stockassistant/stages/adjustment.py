"""Stage 7, AdjustmentAgent: apply the judge's verdicts to the weights with bounded rules.

The rulebook (procedural memory) lives in config: a "trim" cuts ``trim_fraction`` of the
weight scaled by the judge's confidence, an "add" raises it by ``add_fraction`` scaled the
same way, and a "drop" removes the position. Every change except a drop is capped at
``max_change``. No ticker is added here, and gross exposure never rises above the proposal's.

On a retry after a risk rejection the rules turn conservative: drops become trims (so the
portfolio keeps enough holdings) and the risk officer's scale-downs are applied first.
References: baa4f ch. 7 (evaluator-optimizer), ch. 8; ml4t ch. 17.
"""

from __future__ import annotations

from stockassistant.config import Settings
from stockassistant.guardrails import check_weights
from stockassistant.schemas import AdjustedPortfolio, DebateRecord, PortfolioProposal, RiskRejection, WeightChange


def run(
    proposal: PortfolioProposal,
    debate: DebateRecord | None,
    settings: Settings,
    rejection: RiskRejection | None = None,
) -> AdjustedPortfolio:
    cfg = settings.debate
    weights = proposal.weights()
    gross = sum(weights.values())
    changes: list[WeightChange] = []

    if rejection is not None:
        for flag in rejection.review_flags:
            if flag.ticker in weights and flag.scale < 1:
                old = weights[flag.ticker]
                weights[flag.ticker] = old * flag.scale
                changes.append(
                    WeightChange(
                        ticker=flag.ticker,
                        old_weight=old,
                        new_weight=weights[flag.ticker],
                        reason=f"risk review ({flag.risk_type}): {flag.reason}",
                    )
                )

    for v in debate.verdicts if debate else []:
        if v.ticker not in weights:
            continue
        old = weights[v.ticker]
        action = "trim" if (v.action == "drop" and rejection is not None) else v.action
        if action == "drop":
            new = 0.0
        elif action == "trim":
            new = old - min(old * cfg.trim_fraction * v.confidence, cfg.max_change)
        elif action == "add":
            new = old + min(old * cfg.add_fraction * v.confidence, cfg.max_change)
        else:
            continue
        if abs(new - old) < 1e-9:
            continue
        weights[v.ticker] = new
        reason = f"judge: {v.action} (confidence {v.confidence:.2f}) - {v.rationale}"
        if action != v.action:
            reason += " [softened to trim on the retry after a risk rejection]"
        changes.append(WeightChange(ticker=v.ticker, old_weight=round(old, 6), new_weight=round(new, 6), reason=reason))

    total = sum(weights.values())
    if total > gross > 0:  # adds may not raise gross exposure; scale everything back down
        weights = {t: w * gross / total for t, w in weights.items()}
    weights = {t: round(w, 6) for t, w in weights.items() if w > 1e-6}
    check_weights(weights)
    return AdjustedPortfolio(
        as_of=proposal.as_of,
        weights=weights,
        changes=changes,
        attempt=0 if rejection is None else rejection.attempt + 1,
    )
