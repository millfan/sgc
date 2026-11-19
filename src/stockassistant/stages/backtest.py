"""Stage 10, EvaluationAgent: walk-forward backtest of the pipeline and evaluation of the agents.

At each rebalance date the stages run on data up to that date only (the cached provider slices
history; the yfinance provider refuses fundamentals and news for past dates). Trades are
filled at that day's close with the stage 9 cost model, and the portfolio is marked to market
daily until the next rebalance.

Two modes:
- ``quant``: stages 1, 2, 3, 5 (optimizer baseline), 8 (hard limits) and 9. No LLM calls.
  This is the Milestone 2 baseline the agent layer has to beat.
- ``full``: the whole LangGraph pipeline, including every agent. Costs real API money with
  the Anthropic client, so run it on a short window or with ``--llm offline``.

Agent evaluation in ``full`` mode scores each lens's non-neutral views, and the judge's
verdicts, against the stock's return relative to the benchmark until the next rebalance.
References: ml4t ch. 16, ch. 20; baa4f ch. 8, ch. 11.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from typing import Literal

import numpy as np
import pandas as pd

from stockassistant.config import Settings
from stockassistant.data.cache import CachedProvider
from stockassistant.data.provider import MarketDataProvider
from stockassistant.llm import LLMClient
from stockassistant.memory.journal import MemoryJournal
from stockassistant.schemas import (
    AgentEvalReport,
    AnalystReports,
    BacktestReport,
    DebateRecord,
    Holdings,
    RebalancePlan,
    RegimeAssessment,
    RiskRejection,
)
from stockassistant.stages import adjustment, alpha, data_universe, execution, portfolio_manager, regime, risk

log = logging.getLogger(__name__)


def rebalance_dates(index: pd.DatetimeIndex, start: date, end: date, frequency: str) -> list[date]:
    idx = index[(index >= pd.Timestamp(start)) & (index <= pd.Timestamp(end))]
    if len(idx) == 0:
        return []
    s = pd.Series(idx, index=idx)
    key = s.dt.to_period("M") if frequency == "monthly" else s.dt.to_period("W")
    return [d.date() for d in s.groupby(key).max()]


def _apply(plan: RebalancePlan, holdings: Holdings) -> tuple[Holdings, float]:
    shares = dict(holdings.shares)
    cash = holdings.cash
    cost = 0.0
    for o in plan.orders:
        signed = o.shares if o.side == "buy" else -o.shares
        shares[o.ticker] = shares.get(o.ticker, 0.0) + signed
        cash -= signed * o.est_price + o.est_cost
        cost += o.est_cost
    return Holdings(cash=cash, shares={t: n for t, n in shares.items() if n}), cost


def _quant_plan(
    as_of: date,
    settings: Settings,
    provider: MarketDataProvider,
    holdings: Holdings,
    past_regimes: list[RegimeAssessment],
) -> tuple[RebalancePlan | None, RegimeAssessment]:
    snap, uni = data_universe.run(as_of, settings, provider, always_include=list(holdings.shares))
    reg = regime.run(snap, uni, past_regimes)
    scores, short = alpha.run(snap, uni, reg, settings, list(holdings.shares))
    proposal = portfolio_manager.run(snap, short, scores, reg, None, settings, None, use_llm=False)
    adjusted = adjustment.run(proposal, None, settings)
    close = snap.close_frame().ffill()
    value = holdings.cash + sum(n * float(close[t].iloc[-1]) for t, n in holdings.shares.items() if t in close)
    approved = risk.run(adjusted, None, snap, reg, settings.risk, value, None)
    if isinstance(approved, RiskRejection):
        return None, reg
    return execution.run(approved, holdings, snap, settings.execution), reg


def _stats(equity: pd.Series) -> tuple[float, float, float, float]:
    rets = equity.pct_change().dropna()
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)
    vol = float(rets.std() * np.sqrt(252)) if len(rets) > 1 else 0.0
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if len(rets) > 1 and rets.std() > 0 else 0.0
    dd = float((1 - equity / equity.cummax()).max())
    return cagr, vol, sharpe, dd


def run_backtest(
    start: date,
    end: date,
    settings: Settings,
    provider: MarketDataProvider,
    mode: Literal["quant", "full"] = "quant",
    llm: LLMClient | None = None,
) -> tuple[BacktestReport, AgentEvalReport | None]:
    if mode == "full" and llm is None:
        raise ValueError("full mode needs an LLM client")
    tickers = [*settings.data.tickers, settings.data.benchmark]
    cached = CachedProvider(provider, tickers, start - timedelta(days=settings.data.lookback_days), end)
    close = cached.full_close().ffill()
    dates = rebalance_dates(close.index, start, end, settings.run.rebalance_frequency)
    if len(dates) < 2:
        raise ValueError("the window needs at least two rebalance dates")

    holdings = Holdings(cash=settings.run.portfolio_value)
    journal = MemoryJournal()
    past_regimes: list[RegimeAssessment] = []
    equity: dict[pd.Timestamp, float] = {}
    turnovers, total_cost = [], 0.0
    regime_returns: dict[str, list[float]] = defaultdict(list)
    hits: dict[str, list[int]] = defaultdict(list)
    verdict_hits: list[int] = []

    for i, d in enumerate(dates[:-1]):
        nxt = dates[i + 1]
        if mode == "quant":
            plan, reg = _quant_plan(d, settings, cached, holdings, past_regimes)
            reports, debate_rec = None, None
        else:
            from stockassistant.graph import Deps, run_pipeline

            assert llm is not None
            result = run_pipeline(d, Deps(settings, cached, llm, journal, holdings), run_id=f"bt-{d}")
            plan = result.plan
            reg = RegimeAssessment.model_validate(journal.history("regime", d, 1)[0].payload)
            rep = journal.history("reports", d, 1)
            reports = AnalystReports.model_validate(rep[0].payload) if rep else None
            deb = journal.history("debate", d, 1)
            debate_rec = DebateRecord.model_validate(deb[0].payload) if deb else None
        past_regimes.append(reg)
        start_value = holdings.cash + sum(n * float(close.at[pd.Timestamp(d), t]) for t, n in holdings.shares.items())
        if plan is not None:
            holdings, cost = _apply(plan, holdings)
            total_cost += cost
            turnovers.append(plan.turnover)

        window = close.loc[(close.index > pd.Timestamp(d)) & (close.index <= pd.Timestamp(nxt))]
        if not equity:
            equity[pd.Timestamp(d)] = start_value
        for ts, row in window.iterrows():
            equity[pd.Timestamp(str(ts))] = holdings.cash + sum(n * float(row[t]) for t, n in holdings.shares.items())
        end_value = equity[max(equity)]
        regime_returns[reg.risk_mode].append(end_value / start_value - 1)

        # Agent evaluation against what happened next (relative to the benchmark).
        if reports is not None:
            b = settings.data.benchmark
            bench_ret = close.at[pd.Timestamp(nxt), b] / close.at[pd.Timestamp(d), b] - 1
            excess = {
                t: close.at[pd.Timestamp(nxt), t] / close.at[pd.Timestamp(d), t] - 1 - bench_ret
                for t in close.columns
                if t != b
            }
            for r in reports.reports:
                if r.view == "neutral":
                    continue
                if r.lens == "macro":
                    hits["macro"].append(int((bench_ret > 0) == (r.view == "bullish")))
                elif r.ticker in excess:
                    hits[r.lens].append(int((excess[r.ticker] > 0) == (r.view == "bullish")))
            for v in debate_rec.verdicts if debate_rec else []:
                if v.action != "keep" and v.ticker in excess:
                    verdict_hits.append(int((excess[v.ticker] > 0) == (v.action == "add")))

    eq = pd.Series(equity).sort_index()
    bench = close[settings.data.benchmark].loc[eq.index[0] : eq.index[-1]]
    cagr, vol, sharpe, dd = _stats(eq)
    b_cagr, _, b_sharpe, _ = _stats(bench)
    caveats = [
        "The universe is today's ticker list, so history has survivorship bias.",
        "Fills are at the rebalance day's close with modeled costs; real fills differ.",
    ]
    if provider.name == "yfinance":
        caveats.append("yfinance fundamentals and news are not point-in-time, so past runs see neither.")
    if provider.name == "synthetic":
        caveats.append("Synthetic data: results test the machinery, not the strategy.")
    if mode == "full":
        caveats.append(
            "LLM look-ahead: the models may know what happened after these dates. "
            "Treat any gain over the quant baseline with suspicion (PLAN.md, stage 10)."
        )
    monthly = eq.resample("ME").last()
    report = BacktestReport(
        start=dates[0],
        end=dates[-1],
        mode=mode,
        rebalances=len(dates) - 1,
        cagr=round(cagr, 5),
        volatility=round(vol, 5),
        sharpe=round(sharpe, 3),
        max_drawdown=round(dd, 5),
        avg_turnover=round(float(np.mean(turnovers)) if turnovers else 0.0, 5),
        total_costs=round(total_cost, 2),
        benchmark_cagr=round(b_cagr, 5),
        benchmark_sharpe=round(b_sharpe, 3),
        per_regime_return={k: round(float(np.mean(v)), 5) for k, v in regime_returns.items()},
        equity_curve={str(k.date()): round(float(v), 2) for k, v in monthly.items()},
        caveats=caveats,
    )
    agent_eval = None
    if mode == "full":
        agent_eval = AgentEvalReport(
            rebalances=len(dates) - 1,
            analyst_hit_rate={k: round(float(np.mean(v)), 4) for k, v in hits.items()},
            analyst_calls={k: len(v) for k, v in hits.items()},
            verdict_hit_rate=round(float(np.mean(verdict_hits)), 4) if verdict_hits else None,
            note="Hit rate: share of non-neutral views whose sign matched the return relative to the benchmark "
            "until the next rebalance. About 0.5 means the lens adds nothing.",
        )
    return report, agent_eval
