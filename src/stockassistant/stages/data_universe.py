"""Stage 1, DataAgent: fetch point-in-time data, check its quality, and filter to a tradable universe.

Deterministic code. References: ml4t ch. 2, 4 and 6.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from stockassistant.config import Settings
from stockassistant.data.provider import MarketDataProvider
from stockassistant.schemas import DataQualityReport, MarketDataSnapshot, Universe


class DataQualityError(RuntimeError):
    """Raised when the data cannot support a run (the stage 1 guardrail stops the pipeline)."""


def _clean(values: pd.Series) -> list[float | None]:
    return [None if pd.isna(v) else float(v) for v in values.to_numpy()]


def quality_check(close: pd.DataFrame, as_of: date, settings: Settings) -> DataQualityReport:
    cfg = settings.universe
    failed: dict[str, str] = {}
    warnings: list[str] = []
    stale_cutoff = pd.Timestamp(as_of) - pd.tseries.offsets.BDay(5)
    for t in close.columns:
        s = close[t]
        missing = float(s.isna().mean()) if len(s) else 1.0
        if missing > cfg.max_missing_fraction:
            failed[t] = f"missing {missing:.0%} of bars"
            continue
        last_valid = s.last_valid_index()
        if last_valid is None or last_valid < stale_cutoff:
            failed[t] = "stale price"
            continue
        rets = s.pct_change(fill_method=None).abs()
        if (rets > cfg.max_abs_daily_return).any():
            failed[t] = "outlier daily return (possible bad split adjustment)"
            continue
        if missing > 0:
            warnings.append(f"{t}: {missing:.1%} missing bars forward-filled")
    return DataQualityReport(checked_tickers=len(close.columns), failed=failed, warnings=warnings)


def run(
    as_of: date,
    settings: Settings,
    provider: MarketDataProvider,
    always_include: list[str] | None = None,
) -> tuple[MarketDataSnapshot, Universe]:
    listed = settings.universe_members()
    # A ticker that joined the index after the run date was not in the universe yet (backtests).
    not_yet = {m.ticker for m in listed if m.date_added and m.date_added > as_of} - set(always_include or [])
    file_sectors = {m.ticker: m.sector for m in listed if m.sector and m.sector != "Unknown"}
    tickers: list[str] = list(
        dict.fromkeys([*(m.ticker for m in listed if m.ticker not in not_yet), *(always_include or [])])
    )
    benchmark = settings.data.benchmark
    start = as_of - timedelta(days=settings.data.lookback_days)
    close, volume = provider.history([*tickers, benchmark], start, as_of)
    if close.empty or benchmark not in close or close[benchmark].dropna().empty:
        raise DataQualityError(f"no data for the benchmark {benchmark} up to {as_of} from {provider.name}")
    close = close[pd.DatetimeIndex(close.index) <= pd.Timestamp(as_of)]
    volume = volume.reindex(close.index)

    quality = quality_check(close, as_of, settings)
    if benchmark in quality.failed:
        raise DataQualityError(f"benchmark {benchmark} failed quality checks: {quality.failed[benchmark]}")

    good = [t for t in tickers if t in close.columns and t not in quality.failed]
    close = close.ffill()
    need_sector = [t for t in good if t not in file_sectors]
    sectors = {t: file_sectors[t] for t in good if t in file_sectors} | (
        provider.sectors(need_sector) if need_sector else {}
    )
    fundamentals = provider.fundamentals(good, as_of)

    cfg = settings.universe
    dropped: dict[str, str] = {t: f"data quality: {r}" for t, r in quality.failed.items() if t != benchmark}
    dropped.update({t: "no data" for t in tickers if t not in close.columns})
    dropped.update({t: "joined the index after the run date" for t in not_yet})
    members: list[str] = []
    for t in good:
        s = close[t].dropna()
        if len(s) < cfg.min_history_days:
            dropped[t] = f"listing age: {len(s)} days of history"
            continue
        last = float(s.iloc[-1])
        if last < cfg.min_price:
            dropped[t] = f"price {last:.2f} below minimum"
            continue
        adv = float((close[t] * volume[t]).tail(20).mean())
        if not np.isfinite(adv) or adv < cfg.min_avg_dollar_volume:
            dropped[t] = f"average dollar volume {adv:,.0f} below minimum"
            continue
        f = fundamentals.get(t)
        mcap = f.market_cap if f is not None else None
        if mcap is not None and mcap < cfg.min_market_cap:
            dropped[t] = f"market cap {mcap:,.0f} below minimum"
            continue
        members.append(t)

    keep = [*good, benchmark]
    snapshot = MarketDataSnapshot(
        as_of=as_of,
        benchmark=benchmark,
        dates=[d.date() for d in close.index],
        close={t: _clean(close[t]) for t in keep},
        volume={t: _clean(volume[t]) for t in keep},
        sectors=sectors,
        fundamentals=fundamentals,
        news=provider.news(members, as_of),
        quality=quality,
    )
    return snapshot, Universe(as_of=as_of, tickers=members, dropped=dropped)
