from __future__ import annotations

import pytest

from nfh_instantiation_agent import AgentError, InstantiationAgent, Stage


def intent(env: str = "dev") -> dict:
    return {
        "network": {"name": "sandbox", "environment": env, "domain": "retail:1.1.0", "country": "IND"},
        "cloud": {"provider": "gcp", "topology": "gke", "project_id": "sandbox", "state_backend": "tfstate", "credential_ref": "gcp-adc"},
        "deployment": {"node_count": 2, "machine_type": "e2-standard-4", "adapter_version": "1.0.0"},
    }


def local_intent(env: str = "dev") -> dict:
    data = intent(env)
    data["cloud"] = {"provider": "local", "topology": "local"}
    return data


def participant() -> dict:
    return {
        "subscriber_id": "seller.example",
        "role": "BPP",
        "unique_key_id": "key-1",
        "public_url": "https://seller.example/bpp",
        "key_ref": "secret://seller/key",
    }


def test_participant_onboarding_creates_did_credential_and_registry(tmp_path):
    agent = InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work")
    state = agent.onboard_participant(intent(), participant())
    assert state.stage == Stage.DONE
    assert state.participant["did"].startswith("did:nfh:participant:")
    assert state.participant["capability_credential"]["role"] == "BPP"
    stored = agent.store.get_json("role:seller.example:BPP")
    assert stored["subscriber_id"] == "seller.example"
    assert stored["signing_public_key"] == "RESOLVE_FROM:secret://seller/key"


def test_catalog_publish_does_not_render_terraform(tmp_path):
    agent = InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work")
    entry = {"catalog_id": "cat-1", "subscriber_id": "seller.example", "descriptor": {"name": "Seeds"}}
    state = agent.publish_catalog(intent(), entry)
    assert state.stage == Stage.DONE
    assert not (tmp_path / "work").exists()


def test_budget_cap_blocks_bootstrap_before_apply(tmp_path):
    agent = InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work", budget_cap_usd=10)
    state = agent.bootstrap(intent(), approvals={"config": "approve bootstrap config", "apply": "spend money now"})
    assert state.stage == Stage.BLOCKED
    assert "Budget cap blocks" in "\n".join(state.messages)


def test_local_bootstrap_skips_cost_estimation_and_writes_compose(tmp_path, monkeypatch):
    class Completed:
        returncode = 0
        stdout = "started"
        stderr = ""

    monkeypatch.setattr("nfh_instantiation_agent.skills.subprocess.run", lambda *args, **kwargs: Completed())
    monkeypatch.setattr("nfh_instantiation_agent.skills._poll_local_health", lambda ctx: {"healthy": True, "urls": {}})
    agent = InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work", budget_cap_usd=0)

    state = agent.bootstrap(local_intent(), approvals={"config": "approve bootstrap config", "apply": "spend money now"})

    assert state.stage == Stage.DONE
    assert state.cost_estimate == {}
    compose_files = list((tmp_path / "work").glob("*/local/docker-compose.yml"))
    assert compose_files
    assert "Local network 'sandbox' is up" in "\n".join(state.messages)


def test_bootstrap_requires_two_distinct_approval_phrases(tmp_path):
    agent = InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work")
    state = agent.bootstrap(intent(), approvals={"config": "yes", "apply": "yes"})
    assert state.stage == Stage.CONFIG_APPROVAL
    assert "approve bootstrap config" in state.messages[-1]


def test_bootstrap_is_idempotent_noop(tmp_path):
    agent = InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work")
    first = agent.bootstrap(intent(), approvals={"config": "approve bootstrap config", "apply": "spend money now"})
    assert first.stage == Stage.DONE
    rendered_sessions = list((tmp_path / "work").iterdir())
    manifest_count = agent.store.manifest_count()

    second = agent.bootstrap(intent(), approvals={"config": "approve bootstrap config", "apply": "spend money now"})

    assert second.stage == Stage.NOOP
    assert second.diff["changed"] is False
    assert "No intent changes detected" in second.messages[-1]
    assert list((tmp_path / "work").iterdir()) == rendered_sessions
    assert agent.store.manifest_count() == manifest_count


def test_incremental_upgrade_allows_scale_and_version_only(tmp_path):
    old = intent()
    new = intent()
    new["deployment"]["node_count"] = 3
    new["deployment"]["adapter_version"] = "1.1.0"
    agent = InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work")
    state = agent.upgrade(old, new, approvals={"apply": "approve incremental upgrade"})
    assert state.stage == Stage.DONE
    assert set(state.diff["changed_paths"]) == {"deployment.adapter_version", "deployment.node_count"}


def test_incremental_upgrade_rejects_full_recreate_change(tmp_path):
    old = intent()
    new = intent()
    new["cloud"]["region"] = "us-central1"
    agent = InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work")
    state = agent.upgrade(old, new, approvals={"apply": "approve incremental upgrade"})
    assert state.stage == Stage.HUMAN_REVIEW
    assert state.messages[-1].startswith("Upgrade contains non-incremental")


def test_production_participant_needs_exact_skill_approval(tmp_path):
    agent = InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work")
    with pytest.raises(AgentError, match="approve did_provision_skill"):
        agent.onboard_participant(intent("production"), participant(), approvals={"did_provision_skill": "yes"})
