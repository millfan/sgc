"""Deterministic synthetic market data for tests, demos and offline development.

Prices follow a market factor, a sector factor and an idiosyncratic part, with a slowly
changing per-stock drift so that momentum carries some real signal. The same ticker and
date always give the same numbers.
"""

from __future__ import annotations

import zlib
from datetime import date, timedelta
from functools import cache

import numpy as np
import pandas as pd

from stockassistant.schemas import Fundamentals, NewsItem

SECTORS = [
    "Technology",
    "Financials",
    "Health Care",
    "Energy",
    "Consumer Staples",
    "Industrials",
    "Utilities",
    "Materials",
]
_START = date(2012, 1, 2)
_END = date(2027, 12, 31)


def _seed(text: str) -> int:
    return zlib.crc32(text.encode())


@cache
def _calendar() -> pd.DatetimeIndex:
    return pd.bdate_range(_START, _END)


@cache
def _market() -> np.ndarray:
    rng = np.random.default_rng(7)
    n = len(_calendar())
    # Two volatility regimes switching every ~250 days, to give the regime model something to find.
    regime = (np.arange(n) // 250) % 3 == 2
    vol = np.where(regime, 0.018, 0.009)
    drift = np.where(regime, -0.0004, 0.0005)
    return drift + vol * rng.standard_normal(n)


@cache
def _sector_factor(sector: str) -> np.ndarray:
    rng = np.random.default_rng(_seed("sector:" + sector))
    return 0.006 * rng.standard_normal(len(_calendar()))


def sector_of(ticker: str) -> str:
    if ticker == "SPY":
        return "Index"
    return SECTORS[_seed(ticker) % len(SECTORS)]


@cache
def _series(ticker: str) -> tuple[np.ndarray, np.ndarray]:
    n = len(_calendar())
    if ticker == "SPY":
        rets = _market()
        base_vol = 80e6
    else:
        rng = np.random.default_rng(_seed(ticker))
        beta = 0.7 + 0.6 * rng.random()
        idio_vol = 0.008 + 0.012 * rng.random()
        # Drift changes every ~126 days (persistent alpha that momentum can pick up).
        blocks = n // 126 + 1
        drift = np.repeat(0.0006 * rng.standard_normal(blocks), 126)[:n]
        rets = beta * _market() + _sector_factor(sector_of(ticker)) + drift + idio_vol * rng.standard_normal(n)
        base_vol = float(rng.uniform(2e6, 30e6))
    price = 50.0 * np.exp(np.cumsum(rets))
    vol_rng = np.random.default_rng(_seed("vol:" + ticker))
    volume = base_vol * np.exp(0.3 * vol_rng.standard_normal(n))
    return price, volume


class SyntheticProvider:
    name = "synthetic"

    def history(self, tickers: list[str], start: date, end: date) -> tuple[pd.DataFrame, pd.DataFrame]:
        cal = _calendar()
        mask = (cal >= pd.Timestamp(start)) & (cal <= pd.Timestamp(end))
        close = {t: _series(t)[0][mask] for t in tickers}
        volume = {t: _series(t)[1][mask] for t in tickers}
        idx = cal[mask]
        return pd.DataFrame(close, index=idx), pd.DataFrame(volume, index=idx)

    def sectors(self, tickers: list[str]) -> dict[str, str]:
        return {t: sector_of(t) for t in tickers}

    def fundamentals(self, tickers: list[str], as_of: date) -> dict[str, Fundamentals]:
        out: dict[str, Fundamentals] = {}
        quarter = as_of.year * 4 + (as_of.month - 1) // 3
        for t in tickers:
            rng = np.random.default_rng(_seed(f"{t}:{quarter}"))
            price, _ = _series(t)
            next_q_month = ((as_of.month - 1) // 3 + 1) * 3 + 1
            year = as_of.year + (1 if next_q_month > 12 else 0)
            month = next_q_month - 12 if next_q_month > 12 else next_q_month
            out[t] = Fundamentals(
                market_cap=float(rng.uniform(2e10, 2e12)),
                pe_ratio=float(rng.uniform(8, 45)),
                price_to_book=float(rng.uniform(1, 12)),
                return_on_equity=float(rng.uniform(-0.05, 0.4)),
                debt_to_equity=float(rng.uniform(0.1, 2.5)),
                profit_margin=float(rng.uniform(-0.02, 0.35)),
                revenue_growth=float(rng.normal(0.06, 0.08)),
                dividend_yield=float(rng.uniform(0, 0.04)),
                next_earnings_date=date(year, month, 1) + timedelta(days=int(rng.integers(5, 30))),
            )
        return out

    def news(self, tickers: list[str], as_of: date) -> dict[str, list[NewsItem]]:
        out: dict[str, list[NewsItem]] = {}
        cal = _calendar()
        pos = int(cal.searchsorted(pd.Timestamp(as_of), side="right")) - 1
        for t in tickers:
            price, _ = _series(t)
            items = []
            for k, lag in enumerate((3, 10, 20)):
                i = max(pos - lag, 21)
                ret = price[i] / price[i - 21] - 1
                tone = (
                    "beats expectations as demand grows"
                    if ret > 0.02
                    else ("faces pressure as analysts cut targets" if ret < -0.02 else "holds steady in a quiet month")
                )
                published = cal[i].date()
                items.append(
                    NewsItem(
                        id=f"news:{t}:{published.isoformat()}:{k}",
                        ticker=t,
                        published=published,
                        title=f"{t} {tone}",
                        source="synthetic-wire",
                    )
                )
            out[t] = items
        return out
