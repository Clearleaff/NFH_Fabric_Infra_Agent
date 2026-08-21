from __future__ import annotations

import pytest

from nfh_instantiation_agent.llm import GroqLLM, deterministic_phrase


def test_llm_rejects_secret_in_context():
    llm = GroqLLM(api_key="real-secret-value")
    with pytest.raises(ValueError):
        llm.phrase({"credential": "real-secret-value"})


def test_deterministic_phrase_preserves_values():
    source = {"monthly_cost": 219.0, "subscriber_id": "seller.example"}
    text = deterministic_phrase(source)
    assert "219.0" in text
    assert "seller.example" in text
