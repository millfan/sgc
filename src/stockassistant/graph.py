"""LangGraph wiring for stages 1 to 9.

The graph only connects the stages; each node calls one stage's plain ``run`` function, so the
stages stay independent of LangGraph (docs/ARCHITECTURE.md, section 2). Every node's output is
written to the journal by the runner, not by the agent.

    data -> regime -> alpha -> analysts -> portfolio_manager -> (bull || bear) -> devils_advocate
         -> judge -> adjustment -> risk --approved--> execution -> END
                                         --rejected, retry left--> adjustment
                                         --rejected again--> END (no plan)
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from stockassistant.config import Settings
from stockassistant.data.provider import MarketDataProvider
from stockassistant.llm import LLMClient
from stockassistant.memory.journal import Journal
from stockassistant.schemas import (
    AdjustedPortfolio,
    AlphaScores,
    AnalystReports,
    ApprovedPortfolio,
    DebateRecord,
    DebateSide,
    DevilsAdvocateNote,
    Holdings,
    MarketDataSnapshot,
    PortfolioProposal,
    RebalancePlan,
    RegimeAssessment,
    RiskRejection,
    RunResult,
    ShortList,
    Universe,
)
from stockassistant.stages import (
    adjustment,
    alpha,
    analysts,
    data_universe,
    debate,
    execution,
    portfolio_manager,
    regime,
    risk,
)

log = logging.getLogger(__name__)


class PipelineState(TypedDict, total=False):
    run_id: str
    as_of: date
    snapshot: MarketDataSnapshot
    universe: Universe
    regime: RegimeAssessment
    alpha: AlphaScores
    short_list: ShortList
    reports: AnalystReports
    proposal: PortfolioProposal
    bull: DebateSide
    bear: DebateSide
    devils_note: DevilsAdvocateNote
    debate: DebateRecord
    adjusted: AdjustedPortfolio
    approved: ApprovedPortfolio
    rejection: RiskRejection
    plan: RebalancePlan
    status: str


@dataclass
class Deps:
    settings: Settings
    provider: MarketDataProvider
    llm: LLMClient
    journal: Journal
    holdings: Holdings


def _journal_payload(value: Any) -> Any:
    if isinstance(value, MarketDataSnapshot):
        return value.journal_view()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def build_graph(deps: Deps):
    s = deps.settings

    def journaled(name: str, fn: Callable[[PipelineState], dict[str, Any]]):
        def node(state: PipelineState) -> dict[str, Any]:
            log.info("stage %s", name)
            update = fn(state)
            for key, value in update.items():
                if isinstance(value, BaseModel):
                    deps.journal.record(state["run_id"], state["as_of"], key, _journal_payload(value))
            return update

        return node

    def data_node(st: PipelineState) -> dict[str, Any]:
        snap, uni = data_universe.run(st["as_of"], s, deps.provider, always_include=list(deps.holdings.shares))
        return {"snapshot": snap, "universe": uni}

    def regime_node(st: PipelineState) -> dict[str, Any]:
        past = [
            RegimeAssessment.model_validate(e.payload)
            for e in deps.journal.history("regime", known_before=st["as_of"])
            if e.known_at < st["as_of"]
        ]
        return {"regime": regime.run(st["snapshot"], st["universe"], past)}

    def alpha_node(st: PipelineState) -> dict[str, Any]:
        scores, short = alpha.run(st["snapshot"], st["universe"], st["regime"], s, list(deps.holdings.shares))
        return {"alpha": scores, "short_list": short}

    def analysts_node(st: PipelineState) -> dict[str, Any]:
        return {"reports": analysts.run(st["snapshot"], st["short_list"], st["alpha"], st["regime"], deps.llm, s)}

    def pm_node(st: PipelineState) -> dict[str, Any]:
        return {
            "proposal": portfolio_manager.run(
                st["snapshot"], st["short_list"], st["alpha"], st["regime"], st["reports"], s, deps.llm
            )
        }

    def bull_node(st: PipelineState) -> dict[str, Any]:
        return {"bull": debate.argue("bull", st["proposal"], st["reports"], st["snapshot"], deps.llm, s)}

    def bear_node(st: PipelineState) -> dict[str, Any]:
        return {"bear": debate.argue("bear", st["proposal"], st["reports"], st["snapshot"], deps.llm, s)}

    def devils_node(st: PipelineState) -> dict[str, Any]:
        return {"devils_note": debate.devils_advocate(st["bull"], st["bear"], st["proposal"], deps.llm, s)}

    def judge_node(st: PipelineState) -> dict[str, Any]:
        return {
            "debate": debate.judge(
                st["bull"], st["bear"], st["devils_note"], st["proposal"], st["reports"], st["snapshot"], deps.llm, s
            )
        }

    def adjust_node(st: PipelineState) -> dict[str, Any]:
        return {"adjusted": adjustment.run(st["proposal"], st["debate"], s, st.get("rejection"))}

    def risk_node(st: PipelineState) -> dict[str, Any]:
        result = risk.run(
            st["adjusted"],
            st["reports"],
            st["snapshot"],
            st["regime"],
            s.risk,
            _value(deps.holdings, st["snapshot"]),
            deps.llm,
            s,
        )
        if isinstance(result, RiskRejection):
            return {"rejection": result, "status": "rejected"}
        return {"approved": result, "status": "approved"}

    def execution_node(st: PipelineState) -> dict[str, Any]:
        return {"plan": execution.run(st["approved"], deps.holdings, st["snapshot"], s.execution), "status": "planned"}

    def after_risk(st: PipelineState) -> str:
        if st.get("status") == "approved":
            return "execution"
        rej = st["rejection"]
        return "adjustment" if rej.attempt < s.risk.max_retries else END

    g = StateGraph(PipelineState)
    nodes: dict[str, Callable[[PipelineState], dict[str, Any]]] = {
        "data": data_node,
        "regime": regime_node,
        "alpha": alpha_node,
        "analysts": analysts_node,
        "portfolio_manager": pm_node,
        "bull": bull_node,
        "bear": bear_node,
        "devils_advocate": devils_node,
        "judge": judge_node,
        "adjustment": adjust_node,
        "risk": risk_node,
        "execution": execution_node,
    }
    for name, fn in nodes.items():
        g.add_node(name, journaled(name, fn))
    g.add_edge(START, "data")
    g.add_edge("data", "regime")
    g.add_edge("regime", "alpha")
    g.add_edge("alpha", "analysts")
    g.add_edge("analysts", "portfolio_manager")
    g.add_edge("portfolio_manager", "bull")  # bull and bear run in parallel
    g.add_edge("portfolio_manager", "bear")
    g.add_edge(["bull", "bear"], "devils_advocate")
    g.add_edge("devils_advocate", "judge")
    g.add_edge("judge", "adjustment")
    g.add_edge("adjustment", "risk")
    g.add_conditional_edges("risk", after_risk, ["execution", "adjustment", END])
    g.add_edge("execution", END)
    return g.compile()


def _value(holdings: Holdings, snapshot: MarketDataSnapshot) -> float:
    close = snapshot.close_frame().ffill()
    return holdings.cash + sum(n * float(close[t].iloc[-1]) for t, n in holdings.shares.items() if t in close.columns)


def run_pipeline(as_of: date, deps: Deps, run_id: str | None = None) -> RunResult:
    run_id = run_id or f"{as_of.isoformat()}-{uuid.uuid4().hex[:8]}"
    graph = build_graph(deps)
    final = cast(PipelineState, graph.invoke({"run_id": run_id, "as_of": as_of}, {"recursion_limit": 50}))
    deps.journal.record(run_id, as_of, "llm_usage", deps.llm.usage.summary())
    plan = final.get("plan")
    if plan is not None:
        result = RunResult(
            run_id=run_id,
            as_of=as_of,
            status="planned",
            plan=plan,
            message=f"{len(plan.orders)} orders for review; turnover {plan.turnover:.1%}",
        )
    else:
        rej = final.get("rejection")
        result = RunResult(
            run_id=run_id, as_of=as_of, status="rejected", message=rej.message if rej else "run ended without a plan"
        )
    deps.journal.record(run_id, as_of, "run_result", result.model_dump(mode="json", exclude={"plan"}))
    return result
