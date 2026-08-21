from __future__ import annotations

from nfh_instantiation_agent.local_fabric import local_compose_files, local_ports, participant_service_name


def test_local_compose_files_include_core_and_participant_services():
    intent = {
        "network": {"name": "local-fabric", "environment": "dev", "domain": "retail:1.1.0", "country": "IND"},
        "cloud": {"provider": "local", "topology": "local"},
        "deployment": {"adapter_version": "1.0.0"},
    }
    participants = [{"subscriber_id": "bap3.local", "type": "BAP", "subscriber_url": "http://bap3.local"}]

    files = local_compose_files(intent, {"changed_paths": ["cloud.topology"]}, participants)

    assert set(files) == {"docker-compose.yml", "registry-seed.json", "README.md"}
    compose = files["docker-compose.yml"]
    assert "registry:" in compose
    assert "gateway:" in compose
    assert "redis:" in compose
    assert "onix-bap-bap3-local:" in compose
    assert 'CONFIG_FILE: "/app/config/local-simple.yaml"' in compose
    assert "./onix-config:/app/config:ro" in compose
    assert '"127.0.0.1:' in compose and ':8081"' in compose
    assert "driver: bridge" in compose
    assert '"127.0.0.1:' in compose
    assert '"subscriber_id": "bap3.local"' in files["registry-seed.json"]


def test_local_ports_are_deterministic_and_distinct():
    participant = {"subscriber_id": "bap3.local", "type": "BAP"}
    first = local_ports("local-fabric", [participant])
    second = local_ports("local-fabric", [participant])
    assert first == second
    assert len(set(first.values())) == 3
    assert participant_service_name("bap3.local", "BAP") == "onix-bap-bap3-local"
