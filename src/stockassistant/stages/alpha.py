"""Stage 3, AlphaAgent: score the universe and pick the short list.

Features are cross-sectionally z-scored price factors (momentum, short-term reversal, low
volatility, trend), with a regime interaction that leans on low volatility in risk-off
markets. A ridge model is trained walk-forward on earlier dates of the snapshot only:
labels must end ``embargo_days`` before the run date, and the out-of-sample IC is measured
on a purged, later slice. If the model has too little history or its IC is below the
configured floor, the stage falls back to an equal-weighted factor composite.

References: ml4t ch. 6 (walk-forward CV, purging, embargo), ch. 7, 8, 11, 12.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from stockassistant.config import Settings
from stockassistant.quant import FEATURES, price_features, zscore
from stockassistant.schemas import AlphaScore, AlphaScores, MarketDataSnapshot, RegimeAssessment, ShortList, Universe

SAMPLE_STEP = 5  # trading days between training cross-sections


def _composite_weights(regime: RegimeAssessment) -> pd.Series:
    w = pd.Series({"mom_12_1": 1.0, "mom_6_1": 0.5, "rev_1m": 0.25, "low_vol": 0.5, "trend_200": 0.5})
    if regime.risk_mode == "risk_off":
        w["low_vol"] = 1.25
        w["mom_6_1"] = 0.25
    return w / w.abs().sum()


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    ra, rb = pd.Series(a).rank().to_numpy(), pd.Series(b).rank().to_numpy()
    if np.std(ra) == 0 or np.std(rb) == 0:
        return 0.0
    return float(np.corrcoef(ra, rb)[0, 1])


def _training_panel(
    close: pd.DataFrame, tickers: list[str], horizon: int, embargo: int
) -> list[tuple[int, pd.DataFrame, pd.Series]]:
    """(row, z-scored features, forward-return ranks) for every eligible past date."""
    last = len(close) - 1
    latest_label_end = last - embargo
    panel = []
    for pos in range(253, latest_label_end - horizon + 1, SAMPLE_STEP):
        feats = zscore(price_features(close[tickers], pos))
        fwd = close[tickers].iloc[pos + horizon] / close[tickers].iloc[pos] - 1
        ok = fwd.notna()
        if ok.sum() < 5:
            continue
        panel.append((pos, feats[ok], fwd[ok].rank(pct=True) - 0.5))
    return panel


def run(
    snapshot: MarketDataSnapshot,
    universe: Universe,
    regime: RegimeAssessment,
    settings: Settings,
    holdings: list[str] | None = None,
) -> tuple[AlphaScores, ShortList]:
    cfg = settings.alpha
    close = snapshot.close_frame().ffill()
    tickers = universe.tickers
    current = zscore(price_features(close[tickers], len(close) - 1))[FEATURES]
    composite_w = _composite_weights(regime)

    panel = _training_panel(close, tickers, cfg.forecast_horizon_days, cfg.embargo_days)
    model_version = "composite-v1"
    oos_ic: float | None = None
    coefs = composite_w.reindex(FEATURES).fillna(0.0)

    if len(panel) >= cfg.min_train_dates:
        split = int(len(panel) * 0.7)
        purge = int(np.ceil(cfg.forecast_horizon_days / SAMPLE_STEP))  # drop overlapping labels
        train, test = panel[: max(split - purge, 1)], panel[split:]
        model = Ridge(alpha=1.0, fit_intercept=False)
        model.fit(pd.concat([p[1] for p in train])[FEATURES], pd.concat([p[2] for p in train]))
        ics = [_spearman(model.predict(f[FEATURES]), y.to_numpy()) for _, f, y in test]
        oos_ic = float(np.mean(ics)) if ics else None
        if oos_ic is not None and oos_ic > cfg.min_oos_ic:
            full = Ridge(alpha=1.0, fit_intercept=False)
            full.fit(pd.concat([p[1] for p in panel])[FEATURES], pd.concat([p[2] for p in panel]))
            # Blend the fitted coefficients with the regime-aware composite to keep scores stable.
            fitted = pd.Series(full.coef_, index=FEATURES)
            scale = composite_w.abs().sum() / max(fitted.abs().sum(), 1e-9)
            coefs = 0.5 * fitted * scale + 0.5 * coefs
            model_version = "ridge-walkforward-v1"

    contributions = current.mul(coefs, axis=1)
    raw = contributions.sum(axis=1).sort_values(ascending=False)
    scores = [
        AlphaScore(
            ticker=t,
            score=round(float(raw[t]), 5),
            rank=i + 1,
            features={f: round(float(current.loc[t, f]), 4) for f in FEATURES},
            contributions={f: round(float(contributions.loc[t, f]), 5) for f in FEATURES},
        )
        for i, t in enumerate(raw.index)
    ]
    alpha = AlphaScores(
        as_of=snapshot.as_of,
        model_version=model_version,
        forecast_horizon_days=cfg.forecast_horizon_days,
        oos_ic=None if oos_ic is None else round(oos_ic, 4),
        scores=scores,
    )
    top = [s.ticker for s in scores[: cfg.short_list_size]]
    held = [t for t in (holdings or []) if t in tickers and t not in top]
    return alpha, ShortList(as_of=snapshot.as_of, tickers=[*top, *held], from_holdings=held)
