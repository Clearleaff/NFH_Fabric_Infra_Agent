from __future__ import annotations

from nfh_instantiation_agent import InstantiationAgent, InstantiationChatService
from nfh_instantiation_agent.skills import _discover_runtime_contract
from nfh_instantiation_agent.skills import _validate_participant_identity
from nfh_instantiation_agent.local_fabric import local_compose_files
from nfh_instantiation_agent.models import AgentError
from nfh_instantiation_agent.service import _extract_from_text


class FixedLLM:
    debug_log: list[dict] = []

    def parse_intent(self, text, schema):
        del text, schema
        return {"intent": "bootstrap", "arguments": {}}

    def phrase(self, source):
        return str(source)


def test_local_request_never_requires_cloud_fields_and_is_interruptible(tmp_path):
    service = InstantiationChatService(agent=InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work"), llm=FixedLLM())
    first = service.chat("sre", "create me a local beckn retail network with local registry")
    assert first.intent == "bootstrap"
    assert "cloud.project_id" not in first.text
    greeting = service.chat("sre", "hi")
    assert greeting.intent == "help"
    assert service.agent.store.get_json("chat_session:sre")["intent"] == "bootstrap"


def test_plan_is_stable_and_changes_with_desired_state(tmp_path):
    agent = InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work")
    local = {"network": {"name": "demo", "environment": "staging", "domain": "retail:1.1.0"}, "cloud": {"provider": "local", "topology": "local"}}
    first = agent.plan("bootstrap", local)
    assert first["plan_id"].startswith("OP-")
    assert agent.plan("bootstrap", local)["plan_hash"] == first["plan_hash"]
    assert agent.plan("bootstrap", {**local, "gateway": {"enabled": False}})["plan_hash"] != first["plan_hash"]


def test_observation_does_not_claim_sqlite_roles_are_runtime_health(tmp_path):
    agent = InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work")
    agent.store.put_json("role:seller.local:BPP", {"subscriber_id": "seller.local", "type": "BPP"})
    observed = agent.observe({"network": {"name": "demo"}, "cloud": {"topology": "local"}})
    assert observed["runtime_available"] is False
    assert observed["containers"] == []


def test_requirements_question_for_local_network_does_not_request_gcp_fields(tmp_path):
    service = InstantiationChatService(agent=InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work"), llm=FixedLLM())
    message = "what all values you want from to create my own nfh network locally with registry and 1 bap and 2 bpp"
    response = service.chat("requirements", message)
    assert response.intent == "requirements"
    assert "local" in response.text.lower()
    assert "registry" in response.text.lower()
    assert "1 BAP" in response.text
    assert "2 BPP" in response.text
    assert "cloud.project_id" not in response.text
    assert "cloud.state_backend" not in response.text
    assert "cloud.credential_ref" not in response.text
    pending = service.agent.store.get_json("chat_session:requirements")
    assert pending["arguments"]["cloud"]["topology"] == "local"
    assert pending["arguments"]["requested_composition"] == {"bap_count": 1, "bpp_count": 2}


def test_question_and_command_have_distinct_semantics_and_keep_composition(tmp_path):
    service = InstantiationChatService(agent=InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work"), llm=FixedLLM())
    question = service.chat("pair", "what do you need to create a local network?")
    assert question.intent == "requirements"
    command = service.chat("pair", "create me a local nfh network with registry and 1 bap and 2 bpp")
    assert command.intent == "bootstrap"
    state = service.agent.store.get_json("chat_session:pair")
    assert state["arguments"]["cloud"]["topology"] == "local"
    assert state["arguments"]["requested_composition"] == {"bap_count": 1, "bpp_count": 2}
    why = service.chat("pair", "why do you need the domain?")
    assert why.intent == "explanation"
    assert service.agent.store.get_json("chat_session:pair")["arguments"] == state["arguments"]


def test_progressive_local_topology_and_gcp_change_recompute_requirements(tmp_path):
    service = InstantiationChatService(agent=InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work"), llm=FixedLLM())
    service.chat("progress", "I want my own NFH network locally")
    service.chat("progress", "call it demo and use staging")
    service.chat("progress", "retail:1.1.0")
    service.chat("progress", "also I want one BAP and two BPPs")
    state = service.agent.store.get_json("chat_session:progress")
    assert state["arguments"]["requested_composition"] == {"bap_count": 1, "bpp_count": 2}
    assert state["arguments"]["cloud"]["topology"] == "local"
    service.chat("progress", "actually deploy it on GCP")
    state = service.agent.store.get_json("chat_session:progress")
    assert state["arguments"]["cloud"]["topology"] == "gke"


def test_requirements_preview_keeps_local_subject_for_missing_domain_followup(tmp_path):
    service = InstantiationChatService(agent=InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work"), llm=FixedLLM())
    service.chat("context", "what all values you need to create a nfh fabric with local registry and 1bap and 2 bpp")
    response = service.chat("context", "i don't have domain")
    assert "cloud.project_id" not in response.text
    state = service.agent.store.get_json("chat_session:context")
    assert state["arguments"]["cloud"]["topology"] == "local"
    assert state["arguments"]["registry"] == {"enabled": True}
    assert state["arguments"]["requested_composition"] == {"bap_count": 1, "bpp_count": 2}


def test_typo_and_natural_field_variants_are_normalized_without_dns_domain_confusion(tmp_path):
    service = InstantiationChatService(agent=InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work"), llm=FixedLLM())
    service.chat("natural", "create me alocal beckn network with local regiastry")
    service.chat("natural", "network.name:- cleaff-money-exchange")
    service.chat("natural", "network.enviroment:-dev")
    service.chat("natural", "use www.cleaff.com")
    state = service.agent.store.get_json("chat_session:natural")["arguments"]
    assert state["cloud"]["topology"] == "local"
    assert state["network"]["name"] == "cleaff-money-exchange"
    assert state["network"]["environment"] == "dev"
    assert state["network"].get("domain") is None
    assert state["network"]["hostname"] == "www.cleaff.com"


def test_plain_name_detail_is_collected_during_bootstrap(tmp_path):
    service = InstantiationChatService(agent=InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work"), llm=FixedLLM())
    service.chat("plain-name", "create a local nfh fabric")
    response = service.chat("plain-name", "name money")
    assert response.intent == "bootstrap"
    assert "network.environment" in response.text
    assert service.agent.store.get_json("chat_session:plain-name")["arguments"]["network"]["name"] == "money"
    assert _extract_from_text("name money")["network"]["name"] == "money"


def test_run_is_never_reclassified_as_status_or_execution(tmp_path):
    service = InstantiationChatService(agent=InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work"), llm=FixedLLM())
    response = service.chat("run", "run")
    assert response.intent == "clarification"
    assert "no executable `run`" in response.text
    assert service.agent.store.get_json("chat_session:run") is None


def test_one_line_join_network_name_does_not_consume_participant_fields():
    extracted = _extract_from_text(
        "join seller.demo as BPP to local network network.name: demo-money "
        "key id seller-key callback http://seller.demo/bpp/receiver key ref secret://seller/key"
    )
    assert extracted["network"]["name"] == "demo-money"
    assert extracted["subscriber_id"] == "seller.demo"
    assert extracted["unique_key_id"] == "seller-key"


def test_approval_after_done_does_not_repeat_a_completed_action(tmp_path):
    service = InstantiationChatService(agent=InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work"), llm=FixedLLM())
    service.agent.store.put_json("chat_session:done", {"session_id": "done", "intent": "publish_catalog", "stage": "done", "arguments": {}})
    response = service.chat("done", "approve catalog_publish_skill")
    assert response.intent is None
    assert "do not have a pending action" in response.text


def test_runtime_contract_is_component_specific_and_does_not_assume_health_endpoint(tmp_path):
    source = tmp_path / "onix"
    (source / "install").mkdir(parents=True)
    (source / "config").mkdir()
    (source / "install" / "docker-compose.yml").write_text("services:\n  registry:\n    image: fidedocker/registry\n")
    (source / "config" / "local-simple.yaml").write_text("modules:\n  - path: /bap/caller/\n  - path: /bpp/receiver/\n")
    contract = _discover_runtime_contract(source)
    assert contract["registry"]["internal_ports"] == [3000, 3030]
    assert contract["gateway"]["internal_ports"] == [4000, 4030]
    assert "/bap/caller/" in contract["adapter"]["routes"]
    assert "health" not in str(contract).lower()


def test_compose_contains_only_explicit_fabric_participants_and_exact_roles():
    intent = {"network": {"name": "fabric-a", "domain": "retail:1.1.0"}, "deployment": {"adapter_image": "example/adapter@sha256:test"}}
    participants = [{"subscriber_id": "buyer.local", "type": "BAP"}, {"subscriber_id": "seller1.local", "type": "BPP"}, {"subscriber_id": "seller2.local", "type": "BPP"}]
    compose = local_compose_files(intent, participants=participants)["docker-compose.yml"]
    assert "name: nfh-fabric-a" in compose
    assert compose.count("onix-bap-") == 1
    assert compose.count("onix-bpp-") == 2
    assert "sqlite3-connect" not in compose


def test_malformed_participant_identity_is_rejected_before_persistence():
    with __import__("pytest").raises(AgentError, match="invalid subscriber_id"):
        _validate_participant_identity("seller.local callback https://example.invalid key secret://x")
