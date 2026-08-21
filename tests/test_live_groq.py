from __future__ import annotations

import os

import pytest

from nfh_instantiation_agent.llm import GroqLLM
from nfh_instantiation_agent import InstantiationAgent, InstantiationChatService


pytestmark = pytest.mark.skipif(
    not os.environ.get("GROQ_API_KEY") or os.environ.get("INSTANTIATION_AGENT_E2E_LIVE") != "true",
    reason="live Groq suite requires GROQ_API_KEY and INSTANTIATION_AGENT_E2E_LIVE=true",
)


def test_live_participant_intent_and_secret_logs():
    llm = GroqLLM()
    schema = {
        "type": "object",
        "required": ["intent", "skills", "arguments"],
        "properties": {
            "intent": {"enum": ["onboard_participant"]},
            "skills": {"type": "array"},
            "arguments": {"type": "object"},
        },
    }
    parsed = llm.parse_intent(
        "Onboard seller.example as a BPP in staging with key id key-1 and callback https://seller.example/bpp",
        schema,
    )
    assert parsed["intent"] == "onboard_participant"
    assert isinstance(parsed["skills"], list)
    assert isinstance(parsed["arguments"], dict)
    log_text = str(llm.debug_log)
    assert os.environ["GROQ_API_KEY"] not in log_text
    print("LIVE SUMMARY: participant intent JSON parsed; Groq debug logs did not contain the credential value.")


def test_live_phrasing_preserves_cost():
    llm = GroqLLM()
    source = {"monthly_cost": 254.0, "currency": "USD", "guardrail": "cost_estimation_skill"}
    text = llm.phrase(source)
    assert "254.0" in text
    assert "USD" in text
    print("LIVE SUMMARY: deterministic cost survived LLM phrasing without numeric drift.")


def test_live_onboarding_conversation_end_to_end(tmp_path):
    service = InstantiationChatService(
        agent=InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work"),
        llm=GroqLLM(),
    )
    first = service.chat("live-seller", "onboard seller.example as a BPP in staging")
    assert first.stage == "collecting"
    assert "key" in first.text.lower()
    second = service.chat(
        "live-seller",
        "key id key-1 callback https://seller.example/bpp key ref secret://seller/key",
    )
    assert second.stage == "done"
    assert "Registered seller.example as BPP" in second.text
    assert os.environ["GROQ_API_KEY"] not in str(service.llm.debug_log)
    print("LIVE SUMMARY: natural-language onboarding conversation clarified missing fields and executed the deterministic participant skills.")
