from __future__ import annotations

from nfh_instantiation_agent import InstantiationAgent, InstantiationChatService


class EchoLLM:
    def parse_intent(self, text, schema):
        del schema
        return {"intent": "unknown", "arguments": {}}

    def phrase(self, source):
        return str(source)


class FailingLLM:
    def parse_intent(self, text, schema):
        del text, schema
        raise TimeoutError("LLM should not be called")

    def phrase(self, source):
        return str(source)


def service(tmp_path):
    agent = InstantiationAgent(state_db=tmp_path / "state.db", workspace=tmp_path / "work")
    return InstantiationChatService(agent=agent, llm=EchoLLM()), agent


def test_chat_provider_and_catalog_journey(tmp_path):
    chat, agent = service(tmp_path)
    sid = "provider-journey"

    first = chat.chat(sid, "add provider bpp2.local as BPP in staging")
    assert first.stage == "collecting"

    second = chat.chat(
        sid,
        '\n'.join(
            [
                "subscriber_id: bpp2.local",
                "url: http://onix-bpp2-local:8093/bpp/receiver",
                "type: provider",
                'signing_public_key: "BVk+pRYl232qPt0PWrAqR+ecNMmEeyNoMGUcnEpH4Q4="',
                "network_id: local.fabricsandbox/production",
                "status: active",
            ]
        ),
    )
    assert second.stage == "collecting"
    assert "approve did_provision_skill" in second.text or "approval" in second.text
    assert chat.chat(sid, "approve did_provision_skill").stage == "collecting"
    assert chat.chat(sid, "approve capability_credential_skill").stage == "collecting"
    second = chat.chat(sid, "approve role_registration_skill")
    assert second.stage == "done"
    assert second.text == "Registered bpp2.local as BPP."
    assert agent.store.get_json("role:bpp2.local:BPP")["subscriber_id"] == "bpp2.local"

    publish = chat.chat(sid, "publish catalog catalog id bpp2-seeds-v1 descriptor name Local Seeds for bpp2.local")
    assert publish.stage == "collecting"
    assert "approve catalog_publish_skill" in publish.text
    publish = chat.chat(sid, "approve catalog_publish_skill")
    assert publish.stage == "done"
    assert publish.text == "Published catalog bpp2-seeds-v1."
    assert agent.store.get_json("catalog:bpp2-seeds-v1")["descriptor"]["name"] == "Local Seeds for bpp2.local"

    update = chat.chat(sid, "update catalog catalog id bpp2-seeds-v1 descriptor name Local Seeds Updated for bpp2.local")
    assert update.stage == "collecting"
    assert "approve catalog_update_skill" in update.text
    update = chat.chat(sid, "approve catalog_update_skill")
    assert update.stage == "done"
    assert update.text == "Updated catalog bpp2-seeds-v1."
    assert agent.store.get_json("catalog:bpp2-seeds-v1")["descriptor"]["name"] == "Local Seeds Updated for bpp2.local"

    retire = chat.chat(sid, "retire catalog catalog id bpp2-seeds-v1")
    assert retire.stage == "collecting"
    assert "approve catalog_retire_skill" in retire.text
    retire = chat.chat(sid, "approve catalog_retire_skill")
    assert retire.stage == "done"
    assert retire.text == "Retired catalog bpp2-seeds-v1."


def test_chat_provider_accepts_glued_key_value_paste(tmp_path):
    chat, agent = service(tmp_path)
    sid = "glued-provider"

    first = chat.chat(sid, "add provider bpp2.local as BPP in staging")
    assert first.stage == "collecting"

    second = chat.chat(
        sid,
        'subscriber_id: bpp2.localurl: http://onix-bpp2-local:8093/bpp/receivertype: providersigning_public_key: "BVk+pRYl232qPt0PWrAqR+ecNMmEeyNoMGUcnEpH4Q4="network_id: local.fabricsandbox/productionstatus: active',
    )

    assert second.stage == "collecting"
    assert "approve did_provision_skill" in second.text
    assert chat.chat(sid, "approve did_provision_skill").stage == "collecting"
    assert chat.chat(sid, "approve capability_credential_skill").stage == "collecting"
    response = chat.chat(sid, "approve role_registration_skill")
    assert response.stage == "done"
    assert response.text == "Registered bpp2.local as BPP."
    stored = agent.store.get_json("role:bpp2.local:BPP")
    assert stored["subscriber_url"] == "http://onix-bpp2-local:8093/bpp/receiver"


def test_chat_loose_approve_does_not_call_llm_while_approval_is_pending(tmp_path):
    chat, _agent = service(tmp_path)
    sid = "approval-retry"

    chat.chat(sid, "add provider bpp2.local as BPP in staging")
    first_prompt = chat.chat(
        sid,
        'subscriber_id: bpp2.localurl: http://onix-bpp2-local:8093/bpp/receivertype: providersigning_public_key: "BVk+pRYl232qPt0PWrAqR+ecNMmEeyNoMGUcnEpH4Q4="network_id: local.fabricsandbox/productionstatus: active',
    )
    assert "approve did_provision_skill" in first_prompt.text

    chat.llm = FailingLLM()
    retry = chat.chat(sid, "approve")

    assert retry.stage == "collecting"
    assert retry.text == "did_provision_skill requires exact production approval phrase: approve did_provision_skill"


def test_chat_bap_registration_journey(tmp_path):
    chat, agent = service(tmp_path)
    sid = "bap-journey"

    response = chat.chat(
        sid,
        "add baap bap1.local key id bap-key-1 callback http://onix-bap-local:8081/bap/receiver key ref secret://bap1/signing in staging",
    )

    assert response.stage == "done"
    assert response.text == "Registered bap1.local as BAP."
    assert agent.store.get_json("role:bap1.local:BAP")["subscriber_url"] == "http://onix-bap-local:8081/bap/receiver"


def test_chat_bap_role_is_not_overwritten_by_bpp_url_path(tmp_path):
    chat, agent = service(tmp_path)
    sid = "bap-url-path"

    first = chat.chat(sid, "add provider bap3.local as BAP in staging")
    assert first.stage == "collecting"

    second = chat.chat(
        sid,
        '\n'.join(
            [
                "subscriber_id: bap3.local",
                "url: http://onix-bap3-local:8093/bpp/receiver",
                "type: agent",
                'signing_public_key: "BVl+pRYl232qPt0PWrAqR+ecNMmEeyNoMGUcnEpH4Q4="',
                "network_id: local.fabricsandbox/production",
                "status: active",
            ]
        ),
    )

    assert second.stage == "collecting"
    assert "approve did_provision_skill" in second.text
    assert chat.chat(sid, "approve did_provision_skill").stage == "collecting"
    assert chat.chat(sid, "approve capability_credential_skill").stage == "collecting"
    response = chat.chat(sid, "approve role_registration_skill")
    assert response.stage == "done"
    assert response.text == "Registered bap3.local as BAP."
    assert agent.store.get_json("role:bap3.local:BAP")["subscriber_url"] == "http://onix-bap3-local:8093/bpp/receiver"
    assert agent.store.get_json("role:bap3.local:BPP") is None


def test_chat_bap_single_line_fields_keep_subscriber_id_and_role(tmp_path):
    chat, agent = service(tmp_path)
    sid = "bap-single-line"

    chat.chat(sid, "add provider bap3.local as BAP in staging")
    second = chat.chat(
        sid,
        'subscriber_id: bap3.local url: http://onix-bap3-local:8093/bpp/receiver type: agent signing_public_key: "BVl+pRYl232qPt0PWrAqR+ecNMmEeyNoMGUcnEpH4Q4=" network_id: local.fabricsandbox/production status: active',
    )

    assert "approve did_provision_skill" in second.text
    chat.chat(sid, "approve did_provision_skill")
    chat.chat(sid, "approve capability_credential_skill")
    response = chat.chat(sid, "approve role_registration_skill")
    assert response.text == "Registered bap3.local as BAP."
    assert agent.store.get_json("role:bap3.local:BAP")["type"] == "BAP"
    assert agent.store.get_json("role:bap3.local:BPP") is None


def test_chat_new_network_bootstrap_journey(tmp_path):
    chat, agent = service(tmp_path)
    sid = "network-journey"

    first = chat.chat(
        sid,
        "create new network network.name: local-fabric network.domain: retail:1.1.0 production project_id: sandbox state_backend: tfstate credential_ref: gcp-adc",
    )
    assert first.stage == "collecting"
    assert first.text == "bootstrap_skill requires exact production approval phrase: approve bootstrap_skill"

    first = chat.chat(sid, "approve bootstrap_skill")
    assert first.stage == "config_approval"
    assert first.text == "Bootstrap needs exact phrase: approve bootstrap config"

    second = chat.chat(sid, "approve bootstrap config")
    assert second.stage == "apply_approval"
    assert second.text == "Bootstrap needs exact phrase: spend money now"

    third = chat.chat(sid, "spend money now")
    assert third.stage == "done"
    assert third.text.startswith("Release manifest ")
    assert agent.store.manifest_count() == 1
