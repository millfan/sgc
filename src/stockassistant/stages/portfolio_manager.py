"""Stage 5, PortfolioManager: an optimizer baseline, tilted by the manager's reading of the reports.

1. Baseline (code): inverse-volatility weights over the short list, scaled by alpha rank.
2. Tilts (LLM, Opus): for each name, an integer tilt from -2 to +2 that must cite reports.
   Several independent samples are drawn and the median tilt per name is kept
   (self-consistency, baa4f ch. 6). Uncited tilts are set to zero (guardrail).
3. Weights (code): baseline times (1 + tilt * step), the top ``max_positions`` kept, scaled
   to the regime's gross exposure. The rest is cash.

``use_llm=False`` gives the optimizer-only baseline used by the quantitative backtest.
References: ml4t ch. 17; baa4f ch. 8, ch. 6.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np

from stockassistant.config import Settings
from stockassistant.guardrails import check_weights, valid_citations
from stockassistant.llm import LLMClient, load_skill
from stockassistant.quant import inverse_vol_weights
from stockassistant.schemas import (
    AlphaScores,
    AnalystReports,
    ManagerDecision,
    MarketDataSnapshot,
    PortfolioProposal,
    ProposedPosition,
    RegimeAssessment,
    ShortList,
)


def baseline_weights(snapshot: MarketDataSnapshot, short_list: ShortList, alpha: AlphaScores) -> dict[str, float]:
    close = snapshot.close_frame().ffill()
    inv = inverse_vol_weights(close, short_list.tickers)
    scores = alpha.by_ticker()
    n = max(len(alpha.scores), 1)
    # Rank multiplier from 1.5 (best) to 0.5 (worst) keeps the alpha ordering in the baseline.
    raw = {t: inv[t] * (1.5 - (scores[t].rank - 1) / max(n - 1, 1)) for t in short_list.tickers}
    total = sum(raw.values())
    return {t: w / total for t, w in raw.items()}


def _manager_payload(
    baseline: dict[str, float], reports: AnalystReports, alpha: AlphaScores, regime: RegimeAssessment
) -> dict[str, Any]:
    scores = alpha.by_ticker()
    macro = reports.macro()
    return {
        "regime": regime.model_dump(mode="json", exclude={"schema_version"}),
        "macro_report": macro.model_dump(mode="json", exclude={"schema_version"}) if macro else None,
        "candidates": [
            {
                "ticker": t,
                "baseline_weight": round(w, 4),
                "alpha_score": scores[t].score,
                "alpha_rank": scores[t].rank,
                "reports": [
                    {
                        "report_id": r.report_id,
                        "lens": r.lens,
                        "view": r.view,
                        "confidence": r.confidence,
                        "summary": r.summary,
                        "validated": r.validated,
                        "risks": [x.model_dump(exclude={"schema_version"}) for x in r.risks],
                    }
                    for r in reports.for_ticker(t)
                ],
            }
            for t, w in baseline.items()
        ],
    }


def run(
    snapshot: MarketDataSnapshot,
    short_list: ShortList,
    alpha: AlphaScores,
    regime: RegimeAssessment,
    reports: AnalystReports | None,
    settings: Settings,
    llm: LLMClient | None,
    use_llm: bool = True,
) -> PortfolioProposal:
    cfg = settings.portfolio
    baseline = baseline_weights(snapshot, short_list, alpha)
    tilts: dict[str, float] = dict.fromkeys(baseline, 0.0)
    theses: dict[str, str] = {t: "Optimizer baseline (no analyst tilt)." for t in baseline}
    citations: dict[str, list[str]] = {t: [] for t in baseline}
    method = "inverse-vol x alpha-rank baseline"

    if use_llm and llm is not None and reports is not None:
        spec = load_skill("portfolio_manager", settings)
        payload = _manager_payload(baseline, reports, alpha, regime)
        samples: dict[str, list[int]] = defaultdict(list)
        notes: dict[str, list[tuple[int, str, list[str]]]] = defaultdict(list)
        for i in range(max(cfg.self_consistency_samples, 1)):
            decision = llm.structured(spec, payload, ManagerDecision, sample=i)
            for tilt in decision.tilts:
                if tilt.ticker not in baseline:
                    continue
                cited = valid_citations(
                    tilt.cited_reports,
                    {r.report_id for r in reports.for_ticker(tilt.ticker)}
                    | ({m.report_id} if (m := reports.macro()) else set()),
                )
                units = int(np.clip(tilt.tilt_units, -cfg.max_tilt_units, cfg.max_tilt_units)) if cited else 0
                samples[tilt.ticker].append(units)
                notes[tilt.ticker].append((units, tilt.thesis, cited))
        for t, values in samples.items():
            med = int(np.round(np.median(values)))
            tilts[t] = med
            match = next((n for n in notes[t] if n[0] == med), notes[t][0])
            theses[t], citations[t] = match[1], match[2]
        method = f"{method} + manager tilts (median of {cfg.self_consistency_samples} samples)"

    tilted = {t: baseline[t] * max(1 + tilts[t] * cfg.tilt_step, 0.0) for t in baseline}
    chosen = sorted((t for t in tilted if tilted[t] > 0), key=lambda t: tilted[t], reverse=True)[: cfg.max_positions]
    gross = cfg.risk_off_gross if regime.risk_mode == "risk_off" else cfg.risk_on_gross
    total = sum(tilted[t] for t in chosen) or 1.0
    weights = {t: tilted[t] / total * gross for t in chosen}
    check_weights(weights)
    positions = [
        ProposedPosition(
            ticker=t,
            weight=round(weights[t], 6),
            baseline_weight=round(baseline[t], 6),
            tilt_units=tilts[t],
            thesis=theses[t],
            cited_reports=citations[t],
        )
        for t in chosen
    ]
    return PortfolioProposal(
        as_of=snapshot.as_of,
        method=method,
        positions=positions,
        cash_weight=round(1 - sum(p.weight for p in positions), 6),
    )
