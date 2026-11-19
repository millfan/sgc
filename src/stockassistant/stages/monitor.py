"""Stage 11, MonitorAgent: track the paper portfolio between runs and raise alerts.

Reads the latest plan from the journal (the approved one when the person has approved it),
marks it to market, and reports performance against the benchmark, drift from target weights
and alerts. The numbers come from code; an optional LLM summary only puts them into words.
References: ml4t ch. 26, ch. 25; baa4f ch. 12.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from stockassistant.config import Settings
from stockassistant.data.provider import MarketDataProvider
from stockassistant.llm import LLMClient, load_skill
from stockassistant.memory.journal import Journal
from stockassistant.schemas import MonitoringReport, MonitorSummary, RebalancePlan

DRIFT_ALERT = 0.03
UNDERPERFORMANCE_ALERT = 0.05


def latest_plan(journal: Journal, as_of: date) -> RebalancePlan | None:
    approvals = journal.history("approval", known_before=None, limit=1)
    if approvals:
        return RebalancePlan.model_validate(approvals[0].payload["plan"])
    plans = journal.history("plan", known_before=as_of, limit=1)
    return RebalancePlan.model_validate(plans[0].payload) if plans else None


def run(
    as_of: date,
    journal: Journal,
    provider: MarketDataProvider,
    settings: Settings,
    llm: LLMClient | None = None,
) -> MonitoringReport | None:
    plan = latest_plan(journal, as_of)
    if plan is None:
        return None
    target = {t: w for t, w in plan.target_weights.items() if w > 0}
    bench = settings.data.benchmark
    close, _ = provider.history([*target, bench], plan.as_of - timedelta(days=7), as_of)
    close = close.ffill()
    start = close.loc[: pd.Timestamp(plan.as_of)].iloc[-1]
    now = close.iloc[-1]
    rets = {t: float(now[t] / start[t] - 1) for t in target}
    port_ret = sum(w * rets[t] for t, w in target.items())
    bench_ret = float(now[bench] / start[bench] - 1)
    current = {t: w * (1 + rets[t]) / (1 + port_ret) for t, w in target.items()}
    drift = {t: round(current[t] - target[t], 5) for t in target}

    alerts: list[str] = []
    if plan.status != "approved":
        alerts.append("The latest plan has not been approved; returns assume it was executed as planned.")
    for t, d in drift.items():
        if abs(d) > DRIFT_ALERT:
            alerts.append(f"{t} has drifted {d:+.1%} from its target weight.")
    if port_ret - bench_ret < -UNDERPERFORMANCE_ALERT:
        alerts.append(f"Portfolio trails the benchmark by {bench_ret - port_ret:.1%} since the last rebalance.")
    if port_ret < -settings.risk.max_drawdown / 2:
        alerts.append(f"Portfolio is down {port_ret:.1%} since the last rebalance, past half the drawdown limit.")
    due = 31 if settings.run.rebalance_frequency == "monthly" else 7
    if (as_of - plan.as_of).days >= due:
        alerts.append("A rebalance is due: run the pipeline again.")

    report = MonitoringReport(
        as_of=as_of,
        plan_date=plan.as_of,
        portfolio_return=round(port_ret, 5),
        benchmark_return=round(bench_ret, 5),
        drift=drift,
        max_drift=max((abs(d) for d in drift.values()), default=0.0),
        alerts=alerts,
    )
    if llm is not None:
        spec = load_skill("monitor_agent", settings)
        summary = llm.structured(spec, report.model_dump(mode="json"), MonitorSummary).summary
        report = report.model_copy(update={"summary": summary})
    return report
