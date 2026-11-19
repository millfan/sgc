"""Free data from Yahoo Finance through yfinance.

Limits to keep in mind (also listed in the README):
- The free feed is not point-in-time for fundamentals. ``fundamentals`` therefore only
  returns data for run dates within ``FRESH_DAYS`` of today; backtests run without them.
- The ticker list is today's, so backtests carry survivorship bias.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pandas as pd

from stockassistant.schemas import Fundamentals, NewsItem

log = logging.getLogger(__name__)
FRESH_DAYS = 7


def _f(info: dict, key: str) -> float | None:
    val = info.get(key)
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


class YFinanceProvider:
    name = "yfinance"

    def __init__(self) -> None:
        import yfinance  # imported lazily so tests never need network access

        self._yf = yfinance
        self._info: dict[str, dict] = {}

    def _ticker_info(self, ticker: str) -> dict:
        if ticker not in self._info:
            try:
                self._info[ticker] = self._yf.Ticker(ticker).info or {}
            except Exception as exc:  # network or parse errors become missing data, flagged by stage 1
                log.warning("yfinance info failed for %s: %s", ticker, exc)
                self._info[ticker] = {}
        return self._info[ticker]

    def history(self, tickers: list[str], start: date, end: date) -> tuple[pd.DataFrame, pd.DataFrame]:
        raw = self._yf.download(
            tickers,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=True,
            progress=False,
            group_by="column",
            threads=True,
        )
        if raw is None or raw.empty:
            empty = pd.DataFrame(columns=tickers, index=pd.DatetimeIndex([]), dtype=float)
            return empty, empty
        close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]].set_axis(tickers, axis=1)
        volume = raw["Volume"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Volume"]].set_axis(tickers, axis=1)
        close = close.reindex(columns=tickers)
        volume = volume.reindex(columns=tickers)
        close.index = pd.DatetimeIndex(close.index).tz_localize(None)
        volume.index = close.index
        mask = close.index <= pd.Timestamp(end)
        return close[mask], volume[mask]

    def sectors(self, tickers: list[str]) -> dict[str, str]:
        return {t: str(self._ticker_info(t).get("sector") or "Unknown") for t in tickers}

    def fundamentals(self, tickers: list[str], as_of: date) -> dict[str, Fundamentals]:
        if (date.today() - as_of).days > FRESH_DAYS:
            return {}  # not point-in-time: never hand today's fundamentals to a past run date
        out: dict[str, Fundamentals] = {}
        for t in tickers:
            info = self._ticker_info(t)
            if not info:
                continue
            earnings = None
            ts = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
            if ts:
                try:
                    earnings = datetime.fromtimestamp(int(ts)).date()
                except (TypeError, ValueError, OSError):
                    earnings = None
            out[t] = Fundamentals(
                market_cap=_f(info, "marketCap"),
                pe_ratio=_f(info, "trailingPE"),
                price_to_book=_f(info, "priceToBook"),
                return_on_equity=_f(info, "returnOnEquity"),
                debt_to_equity=(d / 100.0) if (d := _f(info, "debtToEquity")) is not None else None,
                profit_margin=_f(info, "profitMargins"),
                revenue_growth=_f(info, "revenueGrowth"),
                dividend_yield=_f(info, "dividendYield"),
                next_earnings_date=earnings if earnings and earnings >= as_of else None,
            )
        return out

    def news(self, tickers: list[str], as_of: date) -> dict[str, list[NewsItem]]:
        if (date.today() - as_of).days > FRESH_DAYS:
            return {}
        out: dict[str, list[NewsItem]] = {}
        for t in tickers:
            try:
                raw_items = self._yf.Ticker(t).news or []
            except Exception as exc:
                log.warning("yfinance news failed for %s: %s", t, exc)
                continue
            items: list[NewsItem] = []
            for i, item in enumerate(raw_items[:10]):
                content = item.get("content", item)
                title = content.get("title")
                when = content.get("pubDate") or content.get("providerPublishTime")
                if not title or not when:
                    continue
                try:
                    published = (
                        datetime.fromtimestamp(int(when)).date()
                        if isinstance(when, int | float)
                        else datetime.fromisoformat(str(when).replace("Z", "+00:00")).date()
                    )
                except (TypeError, ValueError, OSError):
                    continue
                if published > as_of:
                    continue
                provider = content.get("provider") or {}
                url = (content.get("canonicalUrl") or {}).get("url") or content.get("link")
                items.append(
                    NewsItem(
                        id=f"news:{t}:{published.isoformat()}:{i}",
                        ticker=t,
                        published=published,
                        title=str(title),
                        source=str(provider.get("displayName") or content.get("publisher") or "Yahoo Finance"),
                        url=url,
                    )
                )
            out[t] = items
        return out
