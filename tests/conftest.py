from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import pytest

from stockassistant.config import Settings, load_settings
from stockassistant.data.synthetic import SyntheticProvider
from stockassistant.llm.client import UsageLog
from stockassistant.llm.offline import OfflineLLM
from stockassistant.llm.skills import AgentSpec
from stockassistant.stages import alpha, analysts, data_universe, regime

AS_OF = date(2024, 6, 28)


class ScriptedLLM:
    """The offline rules by default, with per-role overrides for tests. Records every call."""

    def __init__(self, overrides: dict[str, Callable[[dict[str, Any]], Any]] | None = None) -> None:
        self.base = OfflineLLM()
        self.usage = UsageLog(10_000)
        self.overrides = overrides or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def structured(self, spec: AgentSpec, payload: dict[str, Any], schema: type, sample: int = 0) -> Any:
        self.usage.reserve(spec.name)
        self.calls.append((spec.name, payload))
        if spec.name in self.overrides:
            return self.overrides[spec.name](payload)
        return self.base.structured(spec, payload, schema, sample)

    def roles(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture(scope="session")
def settings() -> Settings:
    s = load_settings()
    return s.model_copy(
        update={
            "data": s.data.model_copy(update={"provider": "synthetic"}),
            "llm": s.llm.model_copy(update={"provider": "offline"}),
        }
    )


@pytest.fixture(scope="session")
def provider() -> SyntheticProvider:
    return SyntheticProvider()


@pytest.fixture(scope="session")
def stage1(settings, provider):
    return data_universe.run(AS_OF, settings, provider)


@pytest.fixture(scope="session")
def stage2(stage1):
    snap, uni = stage1
    return regime.run(snap, uni)


@pytest.fixture(scope="session")
def stage3(stage1, stage2, settings):
    snap, uni = stage1
    return alpha.run(snap, uni, stage2, settings)


@pytest.fixture(scope="session")
def reports(stage1, stage2, stage3, settings):
    snap, _ = stage1
    scores, short = stage3
    return analysts.run(snap, short, scores, stage2, OfflineLLM(), settings)
