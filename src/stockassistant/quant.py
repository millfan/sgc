"""Shared numeric helpers: features and portfolio statistics.

These are the "tools" behind the agents. LLM agents only ever see numbers produced here.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252
FEATURES = ["mom_12_1", "mom_6_1", "rev_1m", "low_vol", "trend_200"]


def returns(close: pd.DataFrame) -> pd.DataFrame:
    return close.pct_change(fill_method=None)


def price_features(close: pd.DataFrame, pos: int) -> pd.DataFrame:
    """Price-based features for every ticker at row ``pos``, using rows <= pos only."""
    window = close.iloc[: pos + 1]
    last = window.iloc[-1]

    def ret(a: int, b: int) -> pd.Series:
        if len(window) <= a:
            return pd.Series(np.nan, index=close.columns)
        return window.iloc[-1 - b] / window.iloc[-1 - a] - 1

    daily = returns(window).tail(63)
    ma200 = window.tail(200).mean()
    return pd.DataFrame(
        {
            "mom_12_1": ret(252, 21),
            "mom_6_1": ret(126, 21),
            "rev_1m": -ret(21, 0),
            "low_vol": -daily.std() * np.sqrt(TRADING_DAYS),
            "trend_200": last / ma200 - 1,
        }
    )


def zscore(df: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score per column; missing values become 0 (the cross-sectional mean)."""
    z = (df - df.mean()) / df.std(ddof=0).replace(0, np.nan)
    return z.fillna(0.0).clip(-3, 3)


def technical_metrics(close: pd.Series, benchmark: pd.Series) -> dict[str, float]:
    s = close.dropna()
    r = s.pct_change().dropna()
    b = benchmark.reindex(s.index).pct_change().dropna()
    joined = pd.concat([r, b], axis=1, keys=["s", "b"]).dropna().tail(TRADING_DAYS)
    beta = float(joined.cov().iloc[0, 1] / joined["b"].var()) if len(joined) > 20 else float("nan")
    delta = s.diff().tail(15)
    gain, loss = delta.clip(lower=0).mean(), -delta.clip(upper=0).mean()
    rsi = 100.0 if loss == 0 else float(100 - 100 / (1 + gain / loss))
    peak = s.tail(TRADING_DAYS).cummax()
    return {
        "return_1m": float(s.iloc[-1] / s.iloc[-22] - 1) if len(s) > 22 else float("nan"),
        "return_3m": float(s.iloc[-1] / s.iloc[-64] - 1) if len(s) > 64 else float("nan"),
        "return_12m": float(s.iloc[-1] / s.iloc[-253] - 1) if len(s) > 253 else float("nan"),
        "price_vs_ma50": float(s.iloc[-1] / s.tail(50).mean() - 1),
        "price_vs_ma200": float(s.iloc[-1] / s.tail(200).mean() - 1),
        "volatility_3m": float(r.tail(63).std() * np.sqrt(TRADING_DAYS)),
        "rsi_14": rsi,
        "drawdown_from_1y_high": float(s.iloc[-1] / peak.iloc[-1] - 1),
        "beta_1y": beta,
    }


def portfolio_stats(
    weights: dict[str, float], close: pd.DataFrame, benchmark: str, lookback: int = TRADING_DAYS
) -> dict[str, float]:
    """Ex-ante volatility, beta and trailing simulated drawdown of fixed target weights."""
    names = [t for t, w in weights.items() if w > 0]
    if not names:
        return {"volatility": 0.0, "beta": 0.0, "max_drawdown": 0.0, "gross": 0.0}
    rets = returns(close[[*names, benchmark]]).tail(lookback).fillna(0.0)
    w = np.array([weights[t] for t in names])
    port = rets[names].to_numpy() @ w
    cov = np.cov(rets[names].to_numpy(), rowvar=False) * TRADING_DAYS
    vol = float(np.sqrt(max(float(w @ np.atleast_2d(cov) @ w), 0.0)))
    bench = rets[benchmark].to_numpy()
    beta = float(np.cov(port, bench)[0, 1] / np.var(bench, ddof=1)) if np.var(bench) > 0 else 0.0
    equity = np.cumprod(1 + port)
    dd = float(np.max(1 - equity / np.maximum.accumulate(equity))) if len(equity) else 0.0
    return {"volatility": vol, "beta": beta, "max_drawdown": dd, "gross": float(w.sum())}


def inverse_vol_weights(close: pd.DataFrame, tickers: list[str], lookback: int = 63) -> dict[str, float]:
    vol = returns(close[tickers]).tail(lookback).std().replace(0, np.nan)
    inv = (1 / vol).fillna(0.0)
    total = float(inv.sum())
    if total == 0:
        return {t: 1 / len(tickers) for t in tickers}
    return {t: float(inv[t] / total) for t in tickers}


def avg_dollar_volume(close: pd.DataFrame, volume: pd.DataFrame, ticker: str, days: int = 20) -> float:
    return float((close[ticker] * volume[ticker]).tail(days).mean())
