from datetime import date

from stockassistant.cli import main
from stockassistant.llm.offline import OfflineLLM
from stockassistant.memory.journal import FileJournal
from stockassistant.stages.backtest import rebalance_dates, run_backtest


def test_quant_backtest(settings, provider):
    report, agent_eval = run_backtest(date(2023, 1, 1), date(2023, 6, 30), settings, provider)
    assert report.mode == "quant" and report.rebalances == 5 and agent_eval is None
    assert report.total_costs > 0 and 0 <= report.max_drawdown < 1
    assert any("Synthetic" in c for c in report.caveats)


def test_full_backtest_scores_the_agents(settings, provider):
    report, agent_eval = run_backtest(date(2023, 1, 1), date(2023, 3, 31), settings, provider, "full", OfflineLLM())
    assert report.mode == "full" and agent_eval is not None
    assert set(agent_eval.analyst_hit_rate) <= {"fundamental", "technical", "sentiment", "macro"}
    assert all(0 <= v <= 1 for v in agent_eval.analyst_hit_rate.values())
    assert any("look-ahead" in c for c in report.caveats)


def test_rebalance_dates_are_last_trading_day_of_month(provider):
    close, _ = provider.history(["SPY"], date(2024, 1, 1), date(2024, 3, 31))
    assert rebalance_dates(close.index, date(2024, 1, 1), date(2024, 3, 31), "monthly") == [
        date(2024, 1, 31),
        date(2024, 2, 29),
        date(2024, 3, 29),
    ]


def test_file_journal_is_point_in_time(tmp_path):
    j = FileJournal(tmp_path)
    j.record("a", date(2024, 1, 31), "regime", {"v": 1})
    j.record("b", date(2024, 2, 29), "regime", {"v": 2})
    assert [e.payload["v"] for e in j.history("regime")] == [2, 1]
    assert [e.payload["v"] for e in j.history("regime", known_before=date(2024, 2, 1))] == [1]


def test_cli_run_approve_monitor(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["--provider", "synthetic", "run", "--date", "2024-06-28", "--llm", "offline"]) == 0
    out = capsys.readouterr().out
    run_id = out.split("run ")[1].split(":")[0]
    assert "Paper trading only" in out
    assert main(["--provider", "synthetic", "approve", run_id]) == 0
    assert main(["--provider", "synthetic", "approve", run_id]) == 1  # no double booking
    assert main(["--provider", "synthetic", "monitor", "--date", "2024-07-31"]) == 0
    holdings = FileJournal(tmp_path / "runs").history("holdings")
    assert holdings and holdings[0].payload["shares"]
