from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def local_compose_files(intent: dict[str, Any], diff: dict[str, Any] | None = None, participants: list[dict[str, Any]] | None = None) -> dict[str, str]:
    network = intent.get("network") or {}
    deployment = intent.get("deployment") or {}
    name = network.get("name", "local-fabric")
    slug = _slug(name)
    active_participants = [participant for participant in participants or [] if participant.get("status") != "REMOVED"]
    ports = local_ports(name, active_participants)
    adapter_version = deployment.get("adapter_version", "latest")
    # These are the verified published artifacts referenced by Beckn-ONIX's
    # supplied install compose. The adapter is built locally from pinned source.
    registry_image = deployment.get("registry_image", "fidedocker/registry")
    gateway_image = deployment.get("gateway_image", "fidedocker/gateway")
    adapter_image = deployment.get("adapter_image", f"nfh/onix-adapter:{adapter_version}")
    entries = [_registry_entry(intent, participant) for participant in active_participants]

    services = [
        _service_block("registry", registry_image, ports["registry"], slug, {"BECKN_NETWORK": name, "REGISTRY_SEED": "/config/registry-seed.json"}, ["./registry-seed.json:/config/registry-seed.json:ro"], container_port=3000),
        _service_block("gateway", gateway_image, ports["gateway"], slug, {"BECKN_NETWORK": name, "REGISTRY_URL": "http://registry:3000"}, [], container_port=4000),
        _internal_service_block("redis", "redis:alpine", slug),
    ]
    for participant in active_participants:
        sid = participant["subscriber_id"]
        role = participant["type"]
        services.append(
            _service_block(
                participant_service_name(sid, role),
                adapter_image,
                ports[participant_service_name(sid, role)],
                slug,
                {
                    "BECKN_NETWORK": name,
                    "SUBSCRIBER_ID": sid,
                    "SUBSCRIBER_ROLE": role,
                    "REGISTRY_URL": "http://registry:3000",
                    "GATEWAY_URL": "http://gateway:4000",
                    "CONFIG_FILE": "/app/config/local-simple.yaml",
                },
                ["./onix-config:/app/config:ro", "./onix-schemas:/app/schemas:ro"],
                container_port=8081,
            )
        )

    compose = (
        f"name: nfh-{slug}\n"
        "services:\n"
        + "".join(services)
        + f"networks:\n  {slug}:\n    driver: bridge\n"
    )
    seed = json.dumps({"network": network, "entries": entries}, indent=2, sort_keys=True) + "\n"
    changed = ", ".join(sorted((diff or {}).get("changed_paths", []))) or "initial render"
    readme = (
        f"Local Docker Compose fabric for {name}.\n"
        f"Generated services: registry on {ports['registry']}, gateway on {ports['gateway']}, {len(entries)} participant adapter(s).\n"
        f"Changed intent paths: {changed}.\n"
        "Run with: docker compose -f docker-compose.yml up -d\n"
    )
    return {"docker-compose.yml": compose, "registry-seed.json": seed, "README.md": readme}


def local_ports(network_name: str, participants: list[dict[str, Any]] | None = None) -> dict[str, int]:
    base = 30000 + (int(hashlib.sha256(network_name.encode()).hexdigest()[:8], 16) % 20000)
    ports = {"registry": base, "gateway": base + 1}
    for index, participant in enumerate(participants or [], start=2):
        ports[participant_service_name(participant["subscriber_id"], participant["type"])] = base + index
    return ports


def participant_service_name(subscriber_id: str, role: str) -> str:
    prefix = "onix-bap" if role.upper() == "BAP" else "onix-bpp" if role.upper() == "BPP" else "onix-bg"
    return f"{prefix}-{_slug(subscriber_id)}"


def _service_block(name: str, image: str, host_port: int, network_name: str, env: dict[str, str], volumes: list[str], *, container_port: int = 8080) -> str:
    # JSON scalar encoding is valid YAML and prevents untrusted values from
    # becoming YAML syntax (or a Compose option) during deterministic rendering.
    lines = [f"  {name}:", f"    image: {json.dumps(image)}", "    restart: unless-stopped", "    ports:", f"      - \"127.0.0.1:{host_port}:{container_port}\"", "    environment:"]
    lines.extend(f"      {key}: {json.dumps(str(value))}" for key, value in sorted(env.items()))
    if volumes:
        lines.append("    volumes:")
        lines.extend(f"      - {volume}" for volume in volumes)
    lines.extend(["    networks:", f"      - {network_name}"])
    return "\n".join(lines) + "\n"


def _internal_service_block(name: str, image: str, network_name: str) -> str:
    """Render a dependency that intentionally has no host-published port."""
    return "\n".join([
        f"  {name}:",
        f"    image: {json.dumps(image)}",
        "    restart: unless-stopped",
        "    networks:",
        f"      - {network_name}",
    ]) + "\n"


def _registry_entry(intent: dict[str, Any], participant: dict[str, Any]) -> dict[str, Any]:
    network = intent.get("network") or {}
    return {
        "subscriber_id": participant["subscriber_id"],
        "subscriber_url": participant.get("subscriber_url"),
        "domain": participant.get("domain") or network.get("domain"),
        "type": participant["type"],
        "country": participant.get("country") or network.get("country"),
        "status": participant.get("status", "SUBSCRIBED"),
    }


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-") or "fabric"
