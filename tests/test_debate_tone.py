"""The five debaters keep their lens but write factual, sourced statements instead of advocacy.

Bull and Bear still look for upside and downside, and the three risk analysts for reward, loss and
balance; every prompt now carries the shared factual-tone rules and none asks the model to persuade.
"""
from __future__ import annotations

import pytest

from tradingagents.agents.context import get_factual_tone_instruction
from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
from tradingagents.agents.risk_mgmt.aggressive_debator import create_aggressive_debator
from tradingagents.agents.risk_mgmt.conservative_debator import create_conservative_debator
from tradingagents.agents.risk_mgmt.neutral_debator import create_neutral_debator

from tests.test_debate_opening import _capturing_llm, _investment_state, _risk_state

ADVOCACY = ("persuasive", "persuading", "champion", "refute", "compelling", "convincing",
            "best path", "safest path", "best of both worlds", "dynamic debate")

DEBATERS = [
    (create_bull_researcher, _investment_state, "Bull Analyst"),
    (create_bear_researcher, _investment_state, "Bear Analyst"),
    (create_aggressive_debator, _risk_state, "Aggressive Risk Analyst"),
    (create_conservative_debator, _risk_state, "Conservative Risk Analyst"),
    (create_neutral_debator, _risk_state, "Neutral Risk Analyst"),
]


def _prompt(factory, state_fn) -> str:
    captured = {}
    state = state_fn("") if state_fn is _investment_state else state_fn()
    factory(_capturing_llm(captured))(state)
    return captured["prompt"]


@pytest.mark.unit
@pytest.mark.parametrize("factory,state_fn,role", DEBATERS)
def test_each_debater_keeps_its_role_and_writes_factually(factory, state_fn, role):
    prompt = _prompt(factory, state_fn)
    assert role in prompt
    assert get_factual_tone_instruction() in prompt
    # The reports still reach the prompt.
    for report in ("Market research report: m", "Social media sentiment report: s"):
        assert report.lower() in prompt.lower()


@pytest.mark.unit
@pytest.mark.parametrize("factory,state_fn,role", DEBATERS)
def test_no_debater_is_asked_to_persuade(factory, state_fn, role):
    prompt = _prompt(factory, state_fn).lower()
    assert [w for w in ADVOCACY if w in prompt] == []


@pytest.mark.unit
def test_the_tone_rules_ask_for_sources_and_hedged_claims():
    rules = get_factual_tone_instruction()
    assert "say which report it comes from" in rules
    assert "likelihoods rather than certainties" in rules
    assert "do not try to win the argument" in rules
