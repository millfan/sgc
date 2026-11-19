"""Stage 9, ExecutionAgent: turn approved weights into a cost-aware list of share orders.

Costs are commission per share, half the bid-ask spread, and square-root market impact
(``k * daily volatility * sqrt(shares / average daily shares)``). Trades inside the no-trade
band, or whose estimated cost exceeds ``max_cost_bps``, are skipped and listed with the reason.
If one-way turnover exceeds the cap, every trade is scaled down proportionally.

The plan is for the person to review. Nothing is sent to a broker (paper trading only).
References: ml4t ch. 18, ch. 25.
"""

from __future__ import annotations

import math

import numpy as np

from stockassistant.config import ExecutionConfig
from stockassistant.schemas import ApprovedPortfolio, Holdings, MarketDataSnapshot, Order, RebalancePlan, SkippedTrade


def estimate_cost(shares: float, price: float, daily_vol: float, adv_shares: float, cfg: ExecutionConfig) -> float:
    shares = abs(shares)
    if shares == 0:
        return 0.0
    notional = shares * price
    impact = cfg.impact_coefficient * daily_vol * math.sqrt(shares / adv_shares) if adv_shares > 0 else 0.0
    return cfg.commission_per_share * shares + notional * (cfg.half_spread_bps / 1e4 + impact)


def run(
    approved: ApprovedPortfolio, holdings: Holdings, snapshot: MarketDataSnapshot, cfg: ExecutionConfig
) -> RebalancePlan:
    close = snapshot.close_frame().ffill()
    volume = snapshot.volume_frame()
    names = sorted(set(approved.weights) | {t for t, s in holdings.shares.items() if s})
    missing = [t for t in names if t not in close.columns]
    prices = {t: float(close[t].iloc[-1]) for t in names if t not in missing}

    value = holdings.cash + sum(holdings.shares.get(t, 0.0) * prices.get(t, 0.0) for t in names)
    current_w = {t: holdings.shares.get(t, 0.0) * prices[t] / value if value else 0.0 for t in prices}
    target_w = {t: approved.weights.get(t, 0.0) for t in prices}

    skipped: list[SkippedTrade] = [
        SkippedTrade(
            ticker=t,
            reason="no price in the snapshot; cannot trade",
            target_weight=approved.weights.get(t, 0.0),
            current_weight=0.0,
        )
        for t in missing
    ]
    deltas: dict[str, float] = {}
    for t in prices:
        d = target_w[t] - current_w[t]
        if abs(d) < cfg.no_trade_band:
            if abs(d) > 1e-9:
                skipped.append(
                    SkippedTrade(
                        ticker=t,
                        reason="inside the no-trade band",
                        target_weight=target_w[t],
                        current_weight=current_w[t],
                    )
                )
            continue
        deltas[t] = d

    turnover = 0.5 * sum(abs(d) for d in deltas.values())
    scale = min(1.0, cfg.max_turnover / turnover) if turnover > 0 else 1.0

    orders: list[Order] = []
    total_cost = 0.0
    for t, d in deltas.items():
        price = prices[t]
        shares = int(np.trunc(d * scale * value / price))
        if t in holdings.shares and target_w[t] == 0 and scale == 1.0:
            shares = -int(holdings.shares[t])  # full exit sells every share
        if shares == 0:
            continue
        rets = close[t].pct_change().tail(63)
        daily_vol = float(rets.std()) if rets.notna().sum() > 5 else 0.02
        adv_shares = float(volume[t].tail(20).mean())
        cost = estimate_cost(shares, price, daily_vol, adv_shares, cfg)
        notional = abs(shares) * price
        if cost / notional * 1e4 > cfg.max_cost_bps:
            skipped.append(
                SkippedTrade(
                    ticker=t,
                    reason=f"estimated cost {cost / notional * 1e4:.0f} bps above cap",
                    target_weight=target_w[t],
                    current_weight=current_w[t],
                )
            )
            continue
        total_cost += cost
        orders.append(
            Order(
                ticker=t,
                side="buy" if shares > 0 else "sell",
                shares=abs(shares),
                est_price=round(price, 4),
                notional=round(notional, 2),
                est_cost=round(cost, 2),
                current_weight=round(current_w[t], 6),
                target_weight=round(target_w[t], 6),
            )
        )
    orders.sort(key=lambda o: (o.side != "sell", o.ticker))  # sells first, to fund the buys
    executed_turnover = 0.5 * sum(o.notional for o in orders) / value if value else 0.0
    return RebalancePlan(
        as_of=approved.as_of,
        portfolio_value=round(value, 2),
        orders=orders,
        skipped=skipped,
        turnover=round(executed_turnover, 5),
        est_total_cost=round(total_cost, 2),
        target_weights=approved.weights,
    )
