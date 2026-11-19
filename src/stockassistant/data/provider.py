"""The provider interface. Stage 1 only talks to this protocol, so a paid point-in-time
provider can replace yfinance without touching any other stage."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Protocol

import pandas as pd

from stockassistant.schemas import Fundamentals, NewsItem

if TYPE_CHECKING:
    from stockassistant.config import Settings


class MarketDataProvider(Protocol):
    name: str

    def history(self, tickers: list[str], start: date, end: date) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Adjusted close and volume, indexed by trading date, one column per ticker. Rows are <= end."""
        ...

    def sectors(self, tickers: list[str]) -> dict[str, str]: ...

    def fundamentals(self, tickers: list[str], as_of: date) -> dict[str, Fundamentals]:
        """Fundamentals known on ``as_of``. Return nothing rather than data that was not public yet."""
        ...

    def news(self, tickers: list[str], as_of: date) -> dict[str, list[NewsItem]]:
        """Headlines published on or before ``as_of``."""
        ...


def make_provider(settings: Settings) -> MarketDataProvider:
    if settings.data.provider == "synthetic":
        from stockassistant.data.synthetic import SyntheticProvider

        return SyntheticProvider()
    from stockassistant.data.yfinance_provider import YFinanceProvider

    return YFinanceProvider()
