from datetime import date

import numpy as np
import pandas as pd
import pytest

from stockassistant.data.synthetic import SyntheticProvider
from stockassistant.stages import alpha, data_universe, regime
from tests.conftest import AS_OF


def test_snapshot_is_point_in_time(stage1):
    snap, uni = stage1
    assert max(snap.dates) <= AS_OF
    assert snap.benchmark in snap.close
    assert all(n.published <= AS_OF for items in snap.news.values() for n in items)
    assert set(uni.tickers).isdisjoint(uni.dropped)


def test_universe_rules_drop_cheap_stocks(stage1, settings):
    snap, uni = stage1
    for t in uni.tickers:
        assert snap.close[t][-1] >= settings.universe.min_price
    assert any("below minimum" in r for r in uni.dropped.values())


class GappyProvider(SyntheticProvider):
    def history(self, tickers, start, end):
        close, volume = super().history(tickers, start, end)
        close.loc[close.index[-30:], "AAPL"] = np.nan  # stale
        close.loc[close.index[100], "MSFT"] = close["MSFT"].iloc[99] * 3  # bad split
        return close, volume


def test_quality_guardrail_drops_bad_series(settings):
    snap, uni = data_universe.run(AS_OF, settings, GappyProvider())
    assert "AAPL" not in uni.tickers and "MSFT" not in uni.tickers
    assert "stale" in snap.quality.failed["AAPL"] or "missing" in snap.quality.failed["AAPL"]
    assert "outlier" in snap.quality.failed["MSFT"]


class NoBenchmark(SyntheticProvider):
    def history(self, tickers, start, end):
        close, volume = super().history(tickers, start, end)
        return close.drop(columns=["SPY"]), volume.drop(columns=["SPY"])


def test_missing_benchmark_stops_the_run(settings):
    with pytest.raises(data_universe.DataQualityError):
        data_universe.run(AS_OF, settings, NoBenchmark())


def test_regime_labels_and_memory(stage1, stage2):
    snap, uni = stage1
    assert stage2.trend in {"up", "down", "sideways"}
    assert 0 <= stage2.confidence <= 1
    earlier = stage2.model_copy(update={"as_of": date(2024, 5, 31)})
    oldest = stage2.model_copy(update={"as_of": date(2024, 4, 30)})
    again = regime.run(snap, uni, [earlier, oldest])
    assert again.days_in_regime == (AS_OF - date(2024, 4, 30)).days


def test_alpha_ignores_data_after_the_run_date(stage1, stage2, stage3, settings):
    snap, uni = stage1
    scores, short = stage3

    # Rebuild the snapshot with extra rows after the run date; stage 1 must cut them off.
    class Leaky(SyntheticProvider):
        def history(self, tickers, start, end):
            return super().history(tickers, start, date(2024, 12, 31))

    snap2, uni2 = data_universe.run(AS_OF, settings, Leaky())
    scores2, _ = alpha.run(snap2, uni2, stage2, settings)
    assert [s.ticker for s in scores.scores] == [s.ticker for s in scores2.scores]
    assert len(short.tickers) == settings.alpha.short_list_size


def test_alpha_training_labels_respect_embargo(stage1, settings):
    snap, uni = stage1
    close = snap.close_frame().ffill()
    panel = alpha._training_panel(close, uni.tickers, 21, 21)
    assert panel and max(pos for pos, _, _ in panel) + 21 <= len(close) - 1 - 21


def test_short_list_always_includes_holdings(stage1, stage2, settings):
    snap, uni = stage1
    scores, _ = alpha.run(snap, uni, stage2, settings)
    worst = scores.scores[-1].ticker
    _, short = alpha.run(snap, uni, stage2, settings, holdings=[worst])
    assert worst in short.tickers and short.from_holdings == [worst]


def test_frames_round_trip(stage1):
    snap, _ = stage1
    frame = snap.close_frame()
    assert isinstance(frame.index, pd.DatetimeIndex) and frame.shape[0] == len(snap.dates)
