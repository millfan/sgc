"""Guardrails at the three places PLAN.md names: inputs, tool outputs and final outputs (baa4f ch. 12)."""

from __future__ import annotations

import re

_INJECTION = re.compile(
    r"(ignore|disregard|forget)\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|prompts?)"
    r"|system\s+prompt|you\s+are\s+now|new\s+instructions|</?\s*evidence\s*>|\bassistant\s*:|\bhuman\s*:"
    r"|(buy|sell)\s+all\b|set\s+(the\s+)?weight",
    re.IGNORECASE,
)


def looks_like_injection(text: str) -> bool:
    """Tool-output guardrail: news and filings can carry text written to steer an agent."""
    return bool(_INJECTION.search(text))


def valid_citations(cited: list[str], known: set[str]) -> list[str]:
    """Citation guardrail: keep only citations that point at something that exists."""
    return [c for c in dict.fromkeys(cited) if c in known]


def check_weights(weights: dict[str, float], tol: float = 1e-6) -> None:
    """Output guardrail on any weight vector: long only, no NaN, sums to at most 1."""
    for t, w in weights.items():
        if not (w == w) or w < -tol or w > 1 + tol:  # w != w catches NaN
            raise ValueError(f"invalid weight {w!r} for {t}")
    if sum(weights.values()) > 1 + tol:
        raise ValueError(f"weights sum to {sum(weights.values()):.4f} > 1")
