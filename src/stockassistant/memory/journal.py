"""The decision journal (stage 11): an append-only record of every stage's output per run.

It is written by the pipeline runner, never by an agent, and is the system's episodic memory:
the regime stage reads past regimes from it and the monitor reads the latest plan. Two
backends share one interface: JSON files for local use and Postgres (``DATABASE_URL``) for
the Docker Compose setup. ``known_at`` is the run date, so a backtest replay of date D only
reads entries with ``known_at <= D``.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel


class JournalEntry(BaseModel):
    run_id: str
    known_at: date
    stage: str
    recorded_at: datetime
    payload: dict[str, Any]


class Journal(Protocol):
    def record(self, run_id: str, known_at: date, stage: str, payload: dict[str, Any]) -> None: ...

    def history(self, stage: str, known_before: date | None = None, limit: int = 50) -> list[JournalEntry]:
        """Entries for ``stage`` with ``known_at <= known_before``, newest first."""
        ...


def _now() -> datetime:
    return datetime.now(UTC)


class FileJournal:
    """One JSON Lines file, opened in append mode only."""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "journal.jsonl"
        self._lock = threading.Lock()

    def record(self, run_id: str, known_at: date, stage: str, payload: dict[str, Any]) -> None:
        entry = JournalEntry(run_id=run_id, known_at=known_at, stage=stage, recorded_at=_now(), payload=payload)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(entry.model_dump_json() + "\n")

    def history(self, stage: str, known_before: date | None = None, limit: int = 50) -> list[JournalEntry]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                entry = JournalEntry.model_validate_json(line)
                if entry.stage == stage and (known_before is None or entry.known_at <= known_before):
                    out.append(entry)
        out.sort(key=lambda e: (e.known_at, e.recorded_at), reverse=True)
        return out[:limit]


class MemoryJournal:
    """In-process journal for backtests and tests."""

    def __init__(self) -> None:
        self.entries: list[JournalEntry] = []
        self._lock = threading.Lock()

    def record(self, run_id: str, known_at: date, stage: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self.entries.append(
                JournalEntry(
                    run_id=run_id,
                    known_at=known_at,
                    stage=stage,
                    recorded_at=_now(),
                    payload=json.loads(json.dumps(payload, default=str)),
                )
            )

    def history(self, stage: str, known_before: date | None = None, limit: int = 50) -> list[JournalEntry]:
        out = [e for e in self.entries if e.stage == stage and (known_before is None or e.known_at <= known_before)]
        out.sort(key=lambda e: (e.known_at, e.recorded_at), reverse=True)
        return out[:limit]


class PostgresJournal:
    """Postgres backend. The table is created by db/init.sql; the app role only has INSERT and SELECT."""

    def __init__(self, url: str) -> None:
        import psycopg  # optional dependency: pip install stockassistant[postgres]

        self._conn = psycopg.connect(url, autocommit=True)

    def record(self, run_id: str, known_at: date, stage: str, payload: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO journal_entries (run_id, known_at, stage, payload) VALUES (%s, %s, %s, %s)",
                (run_id, known_at, stage, Jsonb(json.loads(json.dumps(payload, default=str)))),
            )

    def history(self, stage: str, known_before: date | None = None, limit: int = 50) -> list[JournalEntry]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT run_id, known_at, stage, recorded_at, payload FROM journal_entries "
                "WHERE stage = %s AND (%s::date IS NULL OR known_at <= %s::date) "
                "ORDER BY known_at DESC, recorded_at DESC LIMIT %s",
                (stage, known_before, known_before, limit),
            )
            return [
                JournalEntry(run_id=r[0], known_at=r[1], stage=r[2], recorded_at=r[3], payload=r[4])
                for r in cur.fetchall()
            ]


def make_journal(database_url: str | None, journal_dir: str | Path) -> Journal:
    if database_url:
        return PostgresJournal(database_url)
    return FileJournal(journal_dir)
