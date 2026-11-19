"""Load an agent role from its skill-style ``SKILL.md`` file (docs/ARCHITECTURE.md, "Where skills fit").

Each file has YAML front matter (name, description, model, effort, max_tokens) and a Markdown
body used as the system prompt. The content hash is the prompt version recorded in the journal.
"""

from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel

from stockassistant.config import Settings

AGENTS_DIR = Path(__file__).resolve().parents[1] / "agents"


class AgentSpec(BaseModel):
    model_config = {"frozen": True}

    name: str
    description: str
    model: str
    effort: str
    max_tokens: int = 8000
    system: str
    version: str


@cache
def _read(role: str) -> AgentSpec:
    path = AGENTS_DIR / role / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"{path} has no front matter")
    _, front, body = text.split("---", 2)
    meta = yaml.safe_load(front)
    return AgentSpec(
        name=meta["name"],
        description=meta["description"],
        model=meta["model"],
        effort=meta.get("effort", "medium"),
        max_tokens=int(meta.get("max_tokens", 8000)),
        system=body.strip(),
        version=hashlib.sha256(text.encode()).hexdigest()[:12],
    )


def load_skill(role: str, settings: Settings | None = None) -> AgentSpec:
    spec = _read(role)
    override = settings.llm.roles.get(role) if settings else None
    if override:
        spec = spec.model_copy(
            update={k: v for k, v in {"model": override.model, "effort": override.effort}.items() if v}
        )
    return spec
