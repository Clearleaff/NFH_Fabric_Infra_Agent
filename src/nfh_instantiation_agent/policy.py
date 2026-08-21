from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_POLICY = {
    "version": "dev-bundle-1",
    "allow": {
        "dev": ["*"],
        "staging": ["*"],
        "production": [
            "cost_estimation_skill",
            "intent_diff_skill",
            "credential_handling_skill",
            "audit_log_skill",
            "policy_check_skill",
            "release_manifest_skill",
            "runtime_preflight_skill",
        ],
    },
    "production_explicit_approval": [
        "did_provision_skill",
        "capability_credential_skill",
        "role_registration_skill",
        "participant_offboard_skill",
        "catalog_publish_skill",
        "catalog_update_skill",
        "catalog_retire_skill",
        "bootstrap_skill",
        "local_execution_skill",
        "upgrade_skill",
        "drift_reconcile_skill",
    ],
    "auto_heal": ["restart_unhealthy_gateway", "refresh_registry_cache"],
    "source": {"approved_repositories": ["https://github.com/beckn/beckn-onix.git"], "allow_branches": False},
}


class PolicyBundle:
    def __init__(self, path: Path | None = None):
        self.path = path
        if path and path.exists():
            self.data = json.loads(path.read_text())
        else:
            self.data = DEFAULT_POLICY

    @property
    def version(self) -> str:
        return str(self.data.get("version", "unknown"))

    def is_allowed(self, actor_did: str, environment: str, skill: str) -> bool:
        del actor_did
        allowed = self.data.get("allow", {}).get(environment, [])
        return "*" in allowed or skill in allowed or self.needs_explicit_approval(environment, skill)

    def needs_explicit_approval(self, environment: str, skill: str) -> bool:
        return environment == "production" and skill in set(self.data.get("production_explicit_approval", []))

    def can_auto_heal(self, action: str) -> bool:
        return action in set(self.data.get("auto_heal", []))

    def decision(self, actor_did: str, environment: str, skill: str, risk: str) -> str:
        if not self.is_allowed(actor_did, environment, skill):
            return "DENY"
        if risk == "destructive":
            return "REQUIRE_PRIVILEGED_APPROVAL"
        if self.needs_explicit_approval(environment, skill) or risk in {"network_mutation", "high_risk_mutation"}:
            return "REQUIRE_APPROVAL"
        return "ALLOW"

    def source_allowed(self, repository: str) -> bool:
        return repository.rstrip("/").removesuffix(".git") in {item.rstrip("/").removesuffix(".git") for item in self.data.get("source", {}).get("approved_repositories", [])}
