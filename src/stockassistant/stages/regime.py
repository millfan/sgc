"""Stage 2, RegimeAgent: classify the market state with observable rules.

Trend comes from the benchmark's moving averages, volatility from the percentile of its
21-day realized volatility, and breadth from the share of the universe above its 50-day
average. A stability check reruns the rules one day earlier and lowers confidence when a
label flips. References: ml4t ch. 9 (9.5 regime features), ch. 1.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from stockassistant.schemas import MarketDataSnapshot, RegimeAssessment, Universe


def _labels(close: pd.DataFrame, benchmark: str, members: list[str]) -> tuple[str, str, str, dict[str, float]]:
    b = close[benchmark].dropna()
    price, ma50, ma200 = float(b.iloc[-1]), float(b.tail(50).mean()), float(b.tail(200).mean())
    if price > ma200 and ma50 > ma200:
        trend = "up"
    elif price < ma200 and ma50 < ma200:
        trend = "down"
    else:
        trend = "sideways"

    rv = b.pct_change().rolling(21).std().dropna() * np.sqrt(252)
    current_vol = float(rv.iloc[-1]) if len(rv) else float("nan")
    pct = float((rv.tail(252) <= current_vol).mean()) if len(rv) else 0.5
    volatility = "low" if pct < 0.3 else ("high" if pct > 0.7 else "normal")

    breadth = float((close[members].iloc[-1] > close[members].tail(50).mean()).mean()) if members else 0.5
    risk_off = trend == "down" or volatility == "high" or breadth < 0.35
    evidence = {
        "benchmark_vs_ma200": price / ma200 - 1,
        "ma50_vs_ma200": ma50 / ma200 - 1,
        "realized_vol_21d": current_vol,
        "vol_percentile_1y": pct,
        "breadth_above_ma50": breadth,
    }
    return trend, volatility, ("risk_off" if risk_off else "risk_on"), evidence


def run(
    snapshot: MarketDataSnapshot, universe: Universe, history: list[RegimeAssessment] | None = None
) -> RegimeAssessment:
    close = snapshot.close_frame().ffill()
    trend, volatility, mode, evidence = _labels(close, snapshot.benchmark, universe.tickers)

    # Confidence: distance from the trend threshold and the volatility band edges.
    trend_margin = min(abs(evidence["benchmark_vs_ma200"]) / 0.05, 1.0)
    vol_margin = min(abs(evidence["vol_percentile_1y"] - 0.5) / 0.5 + 0.3, 1.0)
    confidence = 0.5 * trend_margin + 0.5 * vol_margin

    # Stability guardrail: labels should not flip on one day of data.
    if len(close) > 220:
        prev = _labels(close.iloc[:-1], snapshot.benchmark, universe.tickers)
        if prev[:3] != (trend, volatility, mode):
            confidence *= 0.6
            evidence["label_flipped_vs_yesterday"] = 1.0

    days = 0
    for past in sorted(history or [], key=lambda r: r.as_of, reverse=True):
        if (past.trend, past.risk_mode) != (trend, mode):
            break
        days = (snapshot.as_of - past.as_of).days
    return RegimeAssessment(
        as_of=snapshot.as_of,
        trend=trend,  # type: ignore[arg-type]
        volatility=volatility,  # type: ignore[arg-type]
        risk_mode=mode,  # type: ignore[arg-type]
        confidence=round(float(np.clip(confidence, 0, 1)), 3),
        evidence={k: round(float(v), 5) for k, v in evidence.items()},
        days_in_regime=days,
    )
