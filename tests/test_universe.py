from datetime import date

from stockassistant.data.universe_list import fetch_sp500, load_members, write_members
from stockassistant.stages import data_universe
from tests.conftest import AS_OF


def test_default_universe_is_the_sp500(settings):
    members = settings.universe_members()
    tickers = {m.ticker for m in members}
    assert 490 <= len(members) <= 510
    assert {"AAPL", "MSFT", "JPM", "XOM", "BRK-B"} <= tickers  # class shares use Yahoo's dash form
    assert all(m.sector != "Unknown" for m in members)


def test_sectors_come_from_the_universe_file(stage1, settings):
    snap, uni = stage1
    listed = {m.ticker: m.sector for m in settings.universe_members()}
    assert all(snap.sectors[t] == listed[t] for t in uni.tickers)


def test_names_added_to_the_index_later_are_excluded(settings, provider):
    later = [m.ticker for m in settings.universe_members() if m.date_added and m.date_added > AS_OF]
    assert later, "the list should contain some recent additions"
    _, uni = data_universe.run(AS_OF, settings, provider)
    assert not set(later) & set(uni.tickers)
    assert uni.dropped[later[0]] == "joined the index after the run date"


def test_extra_tickers_are_added(settings):
    s = settings.model_copy(update={"data": settings.data.model_copy(update={"tickers": ["ZZZZ"]})})
    assert "ZZZZ" in {m.ticker for m in s.universe_members()}


def test_refresh_parses_the_source_format(tmp_path):
    rows = ["Symbol,Security,GICS Sector,GICS Sub-Industry,Headquarters Location,Date added,CIK,Founded"]
    rows += [f"T{i},Co {i},Energy,Oil,Somewhere,2001-02-03,{i},1900" for i in range(500)]
    rows += ['BRK.B,Berkshire Hathaway,Financials,Insurance,"Omaha, Nebraska",2010-02-16,1067983,1839']
    src = tmp_path / "constituents.csv"
    src.write_text("\n".join(rows))
    members = fetch_sp500(src.as_uri())
    assert len(members) == 501 and members[0].ticker == "BRK-B"
    out = write_members(members, tmp_path / "sp500.csv")
    loaded = load_members(str(out))
    assert loaded[0].date_added == date(2010, 2, 16) and loaded[0].sector == "Financials"
