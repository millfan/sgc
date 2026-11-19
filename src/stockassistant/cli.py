"""Command line entry point: ``stockassistant run | backtest | monitor | approve``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from stockassistant.config import Settings, load_settings
from stockassistant.data import make_provider
from stockassistant.llm import make_llm
from stockassistant.memory import make_journal
from stockassistant.memory.journal import Journal
from stockassistant.schemas import Holdings, RebalancePlan
from stockassistant.stages.data_universe import DataQualityError


def _settings(args: argparse.Namespace) -> Settings:
    s = load_settings(args.config)
    data = s.data.model_copy(update={"provider": args.provider}) if args.provider else s.data
    llm = s.llm.model_copy(update={"provider": args.llm}) if getattr(args, "llm", None) else s.llm
    return s.model_copy(update={"data": data, "llm": llm})


def _journal(s: Settings) -> Journal:
    return make_journal(s.database_url, s.run.journal_dir)


def current_holdings(journal: Journal, s: Settings, path: str | None) -> Holdings:
    if path:
        return Holdings.model_validate_json(Path(path).read_text())
    entries = journal.history("holdings", limit=1)
    if entries:
        return Holdings.model_validate(entries[0].payload)
    return Holdings(cash=s.run.portfolio_value)


def _print_plan(plan: RebalancePlan) -> None:
    print(f"\nRebalance plan for {plan.as_of}  (portfolio value ${plan.portfolio_value:,.0f})")
    print(f"{'side':<5} {'ticker':<7} {'shares':>8} {'price':>10} {'notional':>12} {'cost':>8} {'target':>8}")
    for o in plan.orders:
        print(
            f"{o.side:<5} {o.ticker:<7} {o.shares:>8} {o.est_price:>10.2f} {o.notional:>12,.0f} "
            f"{o.est_cost:>8.2f} {o.target_weight:>8.2%}"
        )
    for sk in plan.skipped:
        print(f"skip  {sk.ticker:<7} {sk.reason}")
    print(f"turnover {plan.turnover:.1%}, estimated cost ${plan.est_total_cost:,.2f}")
    print(plan.note)


def cmd_run(args: argparse.Namespace) -> int:
    from stockassistant.graph import Deps, run_pipeline

    s = _settings(args)
    journal = _journal(s)
    holdings = current_holdings(journal, s, args.holdings)
    deps = Deps(settings=s, provider=make_provider(s), llm=make_llm(s), journal=journal, holdings=holdings)
    try:
        result = run_pipeline(args.date, deps)
    except DataQualityError as exc:
        print(f"Run stopped by the data-quality guardrail: {exc}", file=sys.stderr)
        return 3
    print(f"run {result.run_id}: {result.status}. {result.message}")
    if result.plan:
        _print_plan(result.plan)
        print(f"\nReview it, place the trades yourself, then record them with: stockassistant approve {result.run_id}")
    return 0 if result.status == "planned" else 2


def cmd_approve(args: argparse.Namespace) -> int:
    s = _settings(args)
    journal = _journal(s)
    plans = [e for e in journal.history("plan", limit=500) if e.run_id == args.run_id]
    if not plans:
        print(f"no plan found for run {args.run_id}", file=sys.stderr)
        return 1
    if any(e.run_id == args.run_id for e in journal.history("approval", limit=500)):
        print(f"run {args.run_id} is already approved", file=sys.stderr)
        return 1
    plan = RebalancePlan.model_validate(plans[0].payload).model_copy(update={"status": "approved"})
    holdings = current_holdings(journal, s, None)
    shares, cash = dict(holdings.shares), holdings.cash
    for o in plan.orders:
        signed = o.shares if o.side == "buy" else -o.shares
        shares[o.ticker] = shares.get(o.ticker, 0.0) + signed
        cash -= signed * o.est_price + o.est_cost
    new = Holdings(cash=round(cash, 2), shares={t: n for t, n in shares.items() if n})
    journal.record(args.run_id, plan.as_of, "approval", {"plan": plan.model_dump(mode="json")})
    journal.record(args.run_id, plan.as_of, "holdings", new.model_dump(mode="json"))
    print(f"Recorded run {args.run_id} as executed. Holdings: {new.model_dump_json()}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from stockassistant.stages.backtest import run_backtest

    s = _settings(args)
    llm = make_llm(s) if args.mode == "full" else None
    report, agent_eval = run_backtest(args.start, args.end, s, make_provider(s), args.mode, llm)
    out = {
        "backtest": report.model_dump(mode="json"),
        "agent_eval": agent_eval.model_dump(mode="json") if agent_eval else None,
    }
    if args.output:
        Path(args.output).write_text(json.dumps(out, indent=2))
    summary = {k: v for k, v in out["backtest"].items() if k != "equity_curve"}
    print(json.dumps({"backtest": summary, "agent_eval": out["agent_eval"]}, indent=2))
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    from stockassistant.stages import monitor

    s = _settings(args)
    journal = _journal(s)
    llm = make_llm(s) if args.summarize else None
    report = monitor.run(args.date, journal, make_provider(s), s, llm)
    if report is None:
        print("No plan in the journal yet. Run the pipeline first.")
        return 1
    journal.record(f"monitor-{args.date}", args.date, "monitoring", report.model_dump(mode="json"))
    print(report.model_dump_json(indent=2))
    return 0


def cmd_update_universe(args: argparse.Namespace) -> int:
    from stockassistant.data.universe_list import fetch_sp500, write_members

    s = _settings(args)
    if not s.data.universe_file:
        print("data.universe_file is empty in the config; nothing to update", file=sys.stderr)
        return 1
    old = {m.ticker for m in s.universe_members()} if s.data.universe_file else set()
    members = fetch_sp500()
    path = write_members(members, s.data.universe_file)
    new = {m.ticker for m in members}
    print(f"Wrote {len(members)} S&P 500 constituents to {path}")
    print(f"added: {sorted(new - old) or 'none'}; removed: {sorted(old - new) or 'none'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stockassistant", description=__doc__)
    p.add_argument("--config", help="TOML config file (default: config/default.toml)")
    p.add_argument("--provider", choices=["yfinance", "synthetic"], help="override the data provider")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run stages 1 to 9 for one date and print the trade plan")
    r.add_argument("--date", type=date.fromisoformat, default=date.today())
    r.add_argument("--holdings", help='JSON file with current holdings: {"cash": ..., "shares": {...}}')
    r.add_argument("--llm", choices=["anthropic", "offline"], help="override the LLM provider")
    r.set_defaults(func=cmd_run)

    a = sub.add_parser("approve", help="record that you placed a plan's trades (paper trading)")
    a.add_argument("run_id")
    a.set_defaults(func=cmd_approve)

    b = sub.add_parser("backtest", help="walk-forward backtest")
    b.add_argument("--start", type=date.fromisoformat, required=True)
    b.add_argument("--end", type=date.fromisoformat, required=True)
    b.add_argument("--mode", choices=["quant", "full"], default="quant")
    b.add_argument("--llm", choices=["anthropic", "offline"], help="override the LLM provider (full mode)")
    b.add_argument("--output", help="write the full report, with the equity curve, to this JSON file")
    b.set_defaults(func=cmd_backtest)

    m = sub.add_parser("monitor", help="mark the latest plan to market and raise alerts")
    m.add_argument("--date", type=date.fromisoformat, default=date.today())
    m.add_argument("--summarize", action="store_true", help="add a plain-language LLM summary")
    m.add_argument("--llm", choices=["anthropic", "offline"], help="override the LLM provider")
    m.set_defaults(func=cmd_monitor)

    u = sub.add_parser("update-universe", help="refresh the S&P 500 constituent list in the universe file")
    u.set_defaults(func=cmd_update_universe)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
