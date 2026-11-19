"""The Anthropic client (with a fake SDK, so no network) and the SKILL.md roles."""

from types import SimpleNamespace

import pytest

from stockassistant.config import RoleOverride
from stockassistant.llm import LLMError, load_skill
from stockassistant.llm.client import FALLBACK_BETA, AnthropicLLM
from stockassistant.llm.skills import AGENTS_DIR
from stockassistant.schemas import ValidationVerdict


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def fake_sdk(stop_reason="end_turn", parsed=None):
    response = SimpleNamespace(
        stop_reason=stop_reason,
        parsed_output=parsed,
        stop_details=None,
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )
    return SimpleNamespace(messages=FakeMessages(response), beta=SimpleNamespace(messages=FakeMessages(response)))


OK = ValidationVerdict(passed=True, issues=[])


def test_opus_request_shape(settings):
    sdk = fake_sdk(parsed=OK)
    llm = AnthropicLLM(settings, client=sdk)
    out = llm.structured(load_skill("judge"), {"x": 1}, ValidationVerdict)
    kw = sdk.beta.messages.kwargs
    assert out == OK
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_config"] == {"effort": "high"}
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_format"] is ValidationVerdict
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert "temperature" not in kw and "tool_choice" not in kw
    assert "<evidence>" in kw["messages"][0]["content"]
    assert llm.usage.summary()["input_tokens"] == {"judge": 100}


def test_haiku_uses_the_plain_endpoint(settings):
    sdk = fake_sdk(parsed=OK)
    AnthropicLLM(settings, client=sdk).structured(load_skill("sentiment_analyst"), {}, ValidationVerdict)
    assert sdk.beta.messages.kwargs is None
    assert sdk.messages.kwargs["model"] == "claude-haiku-5-5"
    assert sdk.messages.kwargs["output_config"] == {"effort": "low"}


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_bad_stops_raise(settings, stop):
    llm = AnthropicLLM(settings, client=fake_sdk(stop_reason=stop, parsed=OK))
    with pytest.raises(LLMError):
        llm.structured(load_skill("judge"), {}, ValidationVerdict)


def test_missing_parse_raises(settings):
    llm = AnthropicLLM(settings, client=fake_sdk(parsed=None))
    with pytest.raises(LLMError):
        llm.structured(load_skill("judge"), {}, ValidationVerdict)


def test_every_role_loads_with_the_planned_model_tier():
    expected = {
        "portfolio_manager": "claude-opus-5-5",
        "judge": "claude-opus-5-5",
        "risk_officer": "claude-opus-5-5",
        "fundamental_analyst": "claude-sonnet-5-5",
        "technical_analyst": "claude-sonnet-5-5",
        "macro_strategist": "claude-sonnet-5-5",
        "report_validator": "claude-sonnet-5-5",
        "bull_agent": "claude-sonnet-5-5",
        "bear_agent": "claude-sonnet-5-5",
        "devils_advocate": "claude-sonnet-5-5",
        "sentiment_analyst": "claude-haiku-5-5",
        "monitor_agent": "claude-sonnet-5-5",
    }
    roles = {p.name for p in AGENTS_DIR.iterdir() if (p / "SKILL.md").exists()}
    assert roles == set(expected)
    for role, model in expected.items():
        spec = load_skill(role)
        assert spec.model == model and spec.name == role and spec.system and len(spec.version) == 12


def test_config_overrides_a_role(settings):
    roles = {"judge": RoleOverride(model="claude-sonnet-5-5", effort="medium")}
    s = settings.model_copy(update={"llm": settings.llm.model_copy(update={"roles": roles})})
    spec = load_skill("judge", s)
    assert (spec.model, spec.effort) == ("claude-sonnet-5-5", "medium")
