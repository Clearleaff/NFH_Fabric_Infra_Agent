from __future__ import annotations

from pathlib import Path

from tools.instantiation_agent import InstantiationAgent, InstantiationChatService


class SequenceLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.debug_log = []

    def parse_intent(self, text, schema):
        del text, schema
        return self.responses.pop(0)

    def phrase(self, source):
        return str(source)


def test_chat_clarifies_and_persists_onboarding_session(tmp_path):
    llm = SequenceLLM(
        [
            {
                "intent": "onboard_participant",
                "arguments": {"subscriber_id": "seller.example", "role": "BPP", "network": {"environment": "staging"}},
            },
            {
                "intent": "onboard_participant",
                "arguments": {
                    "unique_key_id": "key-1",
                    "public_url": "https://seller.example/bpp",
                    "key_ref": "secret://seller/key",
                },
            },
        ]
    )
    agent = InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work")
    service = InstantiationChatService(agent=agent, llm=llm)

    first = service.chat("s1", "onboard seller.example as a BPP in staging")
    assert first.stage == "collecting"
    assert "unique_key_id" in first.text

    second = service.chat("s1", "key id key-1 callback https://seller.example/bpp key ref secret://seller/key")
    assert second.stage == "done"
    assert "Registered seller.example as BPP" in second.text
    stored = agent.store.get_json("role:seller.example:BPP")
    assert stored["subscriber_id"] == "seller.example"


def test_chat_does_not_paraphrase_approval_phrase(tmp_path):
    llm = SequenceLLM(
        [
            {
                "intent": "bootstrap",
                "arguments": {
                    "network": {"name": "chat", "environment": "staging", "domain": "retail:1.1.0"},
                    "cloud": {"project_id": "sandbox", "state_backend": "tfstate", "credential_ref": "gcp-adc"},
                },
            }
        ]
    )
    agent = InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work")
    service = InstantiationChatService(agent=agent, llm=llm)
    first = service.chat("s2", "bootstrap chat staging network")
    assert first.stage == "config_approval"

    second = service.chat("s2", "yes please approve it")
    assert second.stage == "config_approval"
    assert "approve bootstrap config" in second.text


def test_status_intent_does_not_create_circular_session_data(tmp_path):
    llm = SequenceLLM([{"intent": "status", "arguments": {}}])
    agent = InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work")
    service = InstantiationChatService(agent=agent, llm=llm)

    response = service.chat("s3", "status")

    assert response.intent == "status"
    stored = agent.store.get_json("chat_session:s3")
    assert stored["data"]["intent"] == "status"
    assert stored["data"] is not stored


def test_collecting_session_keeps_intent_for_detail_lines(tmp_path):
    llm = SequenceLLM(
        [
            {"intent": "publish_catalog", "arguments": {"subscriber_id": "bpp2.local"}},
            {"intent": "status", "arguments": {"network": {"environment": "production"}}},
        ]
    )
    agent = InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work")
    service = InstantiationChatService(agent=agent, llm=llm)

    first = service.chat("s4", "add this catalog to bpp2.local")
    second = service.chat("s4", "status: active")

    assert first.intent == "publish_catalog"
    assert second.intent == "publish_catalog"
    assert "publish_catalog" in second.text


def test_help_question_does_not_fall_through_to_status(tmp_path):
    llm = SequenceLLM([])
    service = InstantiationChatService(agent=InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work"), llm=llm)

    response = service.chat("s5", "what all can you do")

    assert response.intent == "help"
    assert "local Docker" in response.text
    assert "GCP/Terraform" in response.text


def test_free_form_create_network_collects_bootstrap_fields_not_status(tmp_path):
    llm = SequenceLLM([{"intent": "status", "arguments": {}}])
    service = InstantiationChatService(agent=InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work"), llm=llm)

    response = service.chat("s6", "create a nfh network for me")

    assert response.intent == "bootstrap"
    assert response.stage == "collecting"
    assert "network.name" in response.text


def test_local_join_network_chat_flow(tmp_path, monkeypatch):
    class Completed:
        returncode = 0
        stdout = "started"
        stderr = ""

    monkeypatch.setattr("tools.instantiation_agent.skills.subprocess.run", lambda *args, **kwargs: Completed())
    monkeypatch.setattr("tools.instantiation_agent.skills._poll_local_health", lambda ctx: {"healthy": True, "urls": {}})
    source = tmp_path / "onix"
    (source / "config").mkdir(parents=True)
    (source / "schemas").mkdir()
    (source / "config" / "local-simple.yaml").write_text("http:\n  port: 8081\n")
    llm = SequenceLLM(
        [
            {
                "intent": "join_network",
                "arguments": {
                    "network": {"name": "local-fabric", "environment": "dev", "domain": "retail:1.1.0"},
                    "cloud": {"provider": "local", "topology": "local"},
                    "source": {"path": str(source)},
                    "subscriber_id": "bap3.local",
                    "role": "BAP",
                    "unique_key_id": "bap-key",
                    "public_url": "http://bap3.local",
                    "key_ref": "secret://bap3/key",
                },
            }
        ]
    )
    service = InstantiationChatService(agent=InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work"), llm=llm)

    response = service.chat("s7", "join bap3.local as BAP to local network key id bap-key callback http://bap3.local key ref secret://bap3/key")

    assert response.stage == "done"
    assert "bap3.local joined local-fabric as BAP" in response.text
    assert "onix-bap-bap3-local" in response.text


def test_local_deregister_chat_flow(tmp_path, monkeypatch):
    class Completed:
        returncode = 0
        stdout = "stopped"
        stderr = ""

    monkeypatch.setattr("tools.instantiation_agent.skills.subprocess.run", lambda *args, **kwargs: Completed())
    agent = InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work")
    service = InstantiationChatService(agent=agent, llm=SequenceLLM([{"intent": "offboard_participant", "arguments": {"network": {"name": "local-fabric"}, "cloud": {"topology": "local"}, "subscriber_id": "bap3.local", "role": "BAP"}}]))

    response = service.chat("s8", "remove participant bap3.local as BAP from local network")

    assert response.stage == "done"
    assert "bap3.local removed from local-fabric" in response.text
