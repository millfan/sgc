"""A provider wrapper that fetches price history once for a whole backtest and slices it per date."""

from __future__ import annotations

from datetime import date

import pandas as pd

from stockassistant.data.provider import MarketDataProvider
from stockassistant.schemas import Fundamentals, NewsItem


class CachedProvider:
    def __init__(self, inner: MarketDataProvider, tickers: list[str], start: date, end: date) -> None:
        self.inner = inner
        self.name = f"cached:{inner.name}"
        self._close, self._volume = inner.history(tickers, start, end)
        self._sectors: dict[str, str] = {}

    def history(self, tickers: list[str], start: date, end: date) -> tuple[pd.DataFrame, pd.DataFrame]:
        missing = [t for t in tickers if t not in self._close.columns]
        if missing:
            c, v = self.inner.history(missing, self._close.index[0].date(), self._close.index[-1].date())
            self._close, self._volume = self._close.join(c, how="left"), self._volume.join(v, how="left")
        mask = (self._close.index >= pd.Timestamp(start)) & (self._close.index <= pd.Timestamp(end))
        return self._close.loc[mask, tickers], self._volume.loc[mask, tickers]

    def full_close(self) -> pd.DataFrame:
        return self._close

    def sectors(self, tickers: list[str]) -> dict[str, str]:
        need = [t for t in tickers if t not in self._sectors]
        if need:
            self._sectors.update(self.inner.sectors(need))
        return {t: self._sectors[t] for t in tickers}

    def fundamentals(self, tickers: list[str], as_of: date) -> dict[str, Fundamentals]:
        return self.inner.fundamentals(tickers, as_of)

    def news(self, tickers: list[str], as_of: date) -> dict[str, list[NewsItem]]:
        return self.inner.news(tickers, as_of)
