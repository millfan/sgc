"""The starting universe: a constituent list kept as a CSV file in the repository.

The default is the S&P 500 (``config/universe/sp500.csv``), with each company's GICS sector and
the date it joined the index. Stage 1 uses the sector column instead of asking the data provider
for every ticker, and drops any ticker that joined the index after the run date, so a backtest
does not hold names before they were members. Names that left the index before today are not in
the file, so backtests still carry some survivorship bias.

Refresh the file with ``stockassistant update-universe``.
"""

from __future__ import annotations

import csv
import io
import urllib.request
from datetime import date
from functools import cache
from pathlib import Path

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[3]
# Public-domain dataset that mirrors the Wikipedia list of S&P 500 companies.
SP500_SOURCE_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
FIELDS = ["ticker", "name", "sector", "sub_industry", "date_added"]


class Member(BaseModel):
    model_config = {"frozen": True}

    ticker: str
    name: str = ""
    sector: str = "Unknown"
    sub_industry: str = ""
    date_added: date | None = None


def resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p


@cache
def load_members(path: str) -> tuple[Member, ...]:
    with resolve(path).open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for r in rows:
        added = (r.get("date_added") or "").strip()
        try:
            added_date = date.fromisoformat(added[:10]) if added else None
        except ValueError:
            added_date = None
        out.append(
            Member(
                ticker=r["ticker"].strip(),
                name=r.get("name", ""),
                sector=r.get("sector") or "Unknown",
                sub_industry=r.get("sub_industry", ""),
                date_added=added_date,
            )
        )
    return tuple(out)


def to_provider_symbol(symbol: str) -> str:
    """Class shares are written with a dot in index lists (BRK.B) and a dash in Yahoo symbols (BRK-B)."""
    return symbol.strip().replace(".", "-")


def fetch_sp500(url: str = SP500_SOURCE_URL, timeout: float = 30) -> list[Member]:
    req = urllib.request.Request(url, headers={"User-Agent": "stockassistant-universe-refresh"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    if len(rows) < 450 or "Symbol" not in rows[0]:
        raise ValueError(f"unexpected constituent list from {url}: {len(rows)} rows")
    members = []
    for r in rows:
        added = (r.get("Date added") or "")[:10]
        members.append(
            Member(
                ticker=to_provider_symbol(r["Symbol"]),
                name=r.get("Security", ""),
                sector=r.get("GICS Sector") or "Unknown",
                sub_industry=r.get("GICS Sub-Industry", ""),
                date_added=date.fromisoformat(added) if len(added) == 10 else None,
            )
        )
    return sorted(members, key=lambda m: m.ticker)


def write_members(members: list[Member], path: str | Path) -> Path:
    target = resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        for m in members:
            w.writerow([m.ticker, m.name, m.sector, m.sub_industry, m.date_added.isoformat() if m.date_added else ""])
    load_members.cache_clear()
    return target
