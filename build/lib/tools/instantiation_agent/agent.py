from __future__ import annotations

import uuid
import shutil
import subprocess
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .crypto import synthetic_did
from .crypto import content_hash
from .local_fabric import local_ports
from .models import AgentState, ExecutionPlan, FabricSpec, Lifecycle, RiskClass
from .policy import PolicyBundle
from .skills import LocalFabricClient, SkillContext, SkillRunner, build_skill_registry
from .store import SQLiteStateStore


class InstantiationAgent:
    def __init__(
        self,
        *,
        state_db: Path = Path(".instantiation-agent/state.sqlite3"),
        workspace: Path = Path(".instantiation-agent/work"),
        policy_path: Path | None = None,
        budget_cap_usd: float = 500.0,
        actor_did: str | None = None,
    ):
        self.store = SQLiteStateStore(state_db)
        self.workspace = workspace
        self.policy = PolicyBundle(policy_path)
        self.budget_cap_usd = budget_cap_usd
        self.actor_did = actor_did or synthetic_did("agent", "instantiation-agent")
        self.session_vault: dict[str, dict[str, str]] = {}
        self.runner = SkillRunner(build_skill_registry())
        self.fabric = LocalFabricClient(self.store)

    def fabric_spec(self, intent: dict[str, Any]) -> FabricSpec:
        """Normalize legacy intent into durable desired state without breaking cloud callers."""
        network, cloud = intent.get("network") or {}, intent.get("cloud") or {}
        participants = tuple(item for item in self.store.list_json(f"role:{network.get('name', 'local-fabric')}:") if item.get("status") != "REMOVED")
        catalogs = tuple(item for item in self.store.list_json("catalog:") if item.get("status") != "RETIRED")
        return FabricSpec(
            fabric={"name": network.get("name", "local-fabric"), "environment": network.get("environment", "dev"),
                    "domain": network.get("domain", "retail:1.1.0"), "country": network.get("country", "IND")},
            source=dict(intent.get("source") or {}), runtime={"topology": cloud.get("topology", "gke")},
            registry=dict(intent.get("registry") or {"enabled": True}), gateway=dict(intent.get("gateway") or {"enabled": True}),
            participants=participants, catalogs=catalogs, security=dict(intent.get("security") or {"credential_mode": "references_only"}),
            observability=dict(intent.get("observability") or {"logs": True, "audit": True}),
            requested_composition=dict(intent.get("requested_composition") or {}),
        )

    def observe(self, intent: dict[str, Any]) -> dict[str, Any]:
        """Read runtime facts. SQLite is only desired/control state, never health proof."""
        spec = self.fabric_spec(intent).as_dict()
        # Timestamp is evidence metadata, deliberately excluded from plan/diff identity.
        observed: dict[str, Any] = {"topology": spec["runtime"]["topology"], "containers": [], "runtime_available": False}
        if spec["runtime"]["topology"] != "local":
            return observed
        workspace = self.store.get_json(f"local_workspace:{spec['fabric']['name']}")
        if not workspace:
            observed["reason"] = "no local workspace"
            return observed
        compose = Path(workspace) / "docker-compose.yml"
        if not compose.exists():
            observed["reason"] = "generated compose missing"
            return observed
        try:
            result = subprocess.run(["docker", "compose", "-f", str(compose), "ps", "--format", "json"], capture_output=True, text=True, timeout=30)
            if result.returncode:
                observed["reason"] = (result.stderr or result.stdout).strip()
                return observed
            import json
            text = result.stdout.strip()
            observed["containers"] = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line.strip()]
            observed["runtime_available"] = True
            observed["compose_hash"] = content_hash(compose.read_text())
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            observed["reason"] = str(exc)
        return observed

    def preflight(self, intent: dict[str, Any]) -> dict[str, Any]:
        local = (intent.get("cloud") or {}).get("topology") == "local"
        checks = {"workspace_writable": self.workspace.parent.exists() or self.workspace.parent.mkdir(parents=True, exist_ok=True) is None,
                  "git_available": bool(shutil.which("git"))}
        if local:
            checks["docker_available"] = bool(shutil.which("docker"))
            if checks["docker_available"]:
                try:
                    checks["docker_daemon_reachable"] = subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
                except OSError:
                    checks["docker_daemon_reachable"] = False
            else:
                checks["docker_daemon_reachable"] = False
            ports = local_ports((intent.get("network") or {}).get("name", "local-fabric"))
            for label, port in ports.items():
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try: checks[f"{label}_port_available"] = sock.connect_ex(("127.0.0.1", port)) != 0
                finally: sock.close()
        return {"checks": checks, "ok": all(checks.values()), "mutating": False}

    def plan(self, lifecycle: str, intent: dict[str, Any], *, observed: dict[str, Any] | None = None) -> dict[str, Any]:
        observed = observed if observed is not None else self.observe(intent)
        local = (intent.get("cloud") or {}).get("topology") == "local"
        actions = _plan_actions(lifecycle, local)
        risk = RiskClass.READ_ONLY.value if lifecycle in {"status", "diagnostic", "drift"} else (RiskClass.NETWORK_MUTATION.value if local else RiskClass.LOW_RISK_MUTATION.value)
        desired_hash, observed_hash = content_hash(self.fabric_spec(intent).as_dict()), content_hash(observed)
        core = {"lifecycle": lifecycle, "actor": self.actor_did, "desired_hash": desired_hash, "observed_hash": observed_hash,
                "actions": actions, "risk": risk, "policy_version": self.policy.version}
        core["source"] = dict(intent.get("source") or {"status": "unresolved"})
        core["diff_hash"] = content_hash({"desired": desired_hash, "observed": observed_hash})
        core["policy_result"] = self.policy.decision(self.actor_did, (intent.get("network") or {}).get("environment", "dev"), "local_execution_skill" if local else "bootstrap_skill", risk)
        core["required_approvals"] = ["approve bootstrap config", "spend money now"] if lifecycle == "bootstrap" else []
        core["plan_hash"] = content_hash(core)
        core["plan_id"] = "OP-" + core["plan_hash"][:10].upper()
        self.store.save_plan(core["plan_id"], core)
        return core

    def new_state(self, lifecycle: Lifecycle | str, desired_intent: dict[str, Any]) -> AgentState:
        return AgentState(
            session_id=uuid.uuid4().hex,
            actor_did=self.actor_did,
            lifecycle=Lifecycle(lifecycle),
            desired_intent=desired_intent,
        )

    def bootstrap(self, intent: dict[str, Any], approvals: dict[str, str] | None = None) -> AgentState:
        state = self.new_state(Lifecycle.BOOTSTRAP, intent)
        state.last_applied_intent = self.store.get_json(_intent_key("bootstrap", intent), {})
        state.approvals = approvals or {}
        if (intent.get("cloud") or {}).get("topology") == "local":
            skills = ["intent_diff_skill", "credential_handling_skill", "source_resolve_skill", "runtime_build_skill", "runtime_preflight_skill", "bootstrap_skill", "local_execution_skill", "release_manifest_skill"]
        else:
            skills = ["intent_diff_skill", "cost_estimation_skill", "credential_handling_skill", "bootstrap_skill", "release_manifest_skill"]
        self._run(state, skills, {"desired_intent": intent})
        if state.last_applied_intent == intent:
            self.store.put_json(_intent_key("bootstrap", intent), intent)
        return state

    def onboard_participant(self, intent: dict[str, Any], participant: dict[str, Any], approvals: dict[str, str] | None = None) -> AgentState:
        state = self.new_state(Lifecycle.PARTICIPANT, intent)
        state.participant = participant
        state.approvals = approvals or {}
        self._run(state, ["did_provision_skill", "capability_credential_skill", "role_registration_skill"], {"participant": participant})
        return state

    def join_network(self, intent: dict[str, Any], participant: dict[str, Any], approvals: dict[str, str] | None = None) -> AgentState:
        state = self.new_state(Lifecycle.PARTICIPANT, intent)
        state.participant = participant
        state.approvals = approvals or {}
        self._run(state, ["did_provision_skill", "capability_credential_skill", "role_registration_skill", "local_execution_skill"], {"participant": participant})
        return state

    def offboard_participant(self, intent: dict[str, Any], subscriber_id: str, role: str, approvals: dict[str, str] | None = None) -> AgentState:
        state = self.new_state(Lifecycle.PARTICIPANT, intent)
        state.approvals = approvals or {}
        self._run(state, ["participant_offboard_skill"], {"subscriber_id": subscriber_id, "role": role})
        return state

    def publish_catalog(self, intent: dict[str, Any], entry: dict[str, Any], action: str = "publish", approvals: dict[str, str] | None = None) -> AgentState:
        state = self.new_state(Lifecycle.CATALOG, intent)
        state.catalog_entry = entry
        state.approvals = approvals or {}
        skill = {"publish": "catalog_publish_skill", "update": "catalog_update_skill", "retire": "catalog_retire_skill"}[action]
        args = {"catalog_entry": entry, "catalog_id": entry.get("catalog_id")}
        self._run(state, [skill], args)
        return state

    def upgrade(self, previous_intent: dict[str, Any], desired_intent: dict[str, Any], approvals: dict[str, str] | None = None) -> AgentState:
        state = self.new_state(Lifecycle.UPGRADE, desired_intent)
        state.last_applied_intent = previous_intent
        state.approvals = approvals or {}
        self._run(state, ["intent_diff_skill", "cost_estimation_skill", "upgrade_skill", "release_manifest_skill"], {"desired_intent": desired_intent, "last_applied_intent": previous_intent})
        return state

    def reconcile_drift(self, intent: dict[str, Any], observed_state: dict[str, Any], actions: list[str] | None = None) -> AgentState:
        state = self.new_state(Lifecycle.DRIFT, intent)
        state.observed_state = observed_state
        self._run(state, ["drift_reconcile_skill"], {"observed_state": observed_state, "actions": actions or []})
        return state

    def rollback(self, release_id: str) -> dict[str, Any]:
        manifest = self.store.load_manifest(release_id)
        if not manifest:
            raise FileNotFoundError(f"release manifest not found: {release_id}")
        self.store.put_json("rollback:requested", manifest)
        return {"rollback_requested": True, "release_id": release_id, "manifest": manifest}

    def _run(self, state: AgentState, skills: list[str], arguments: dict[str, Any]) -> None:
        ctx = SkillContext(
            state=state,
            store=self.store,
            fabric=self.fabric,
            policy=self.policy,
            workspace=self.workspace,
            budget_cap_usd=self.budget_cap_usd,
            session_vault=self.session_vault,
        )
        self.runner.run(ctx, skills, arguments)


def _intent_key(lifecycle: str, intent: dict[str, Any]) -> str:
    network = intent.get("network") or {}
    return f"last_applied:{lifecycle}:{network.get('name', 'network')}"


def _plan_actions(lifecycle: str, local: bool) -> list[str]:
    if lifecycle == "bootstrap":
        return (["preflight.inspect", "workspace.prepare", "source.resolve", "config.render", "compose.render", "compose.validate", "registry.start", "registry.health.verify", "gateway.start", "gateway.health.verify", "release.record"] if local else ["intent.diff", "cost.estimate", "terraform.render", "release.record"])
    if lifecycle in {"participant", "join_network"}:
        return ["identity.provision", "credential.issue", "participant.config.render", "participant.start", "registry.register", "participant.verify"]
    if lifecycle == "offboard_participant": return ["participant.inspect", "registry.disable", "participant.stop", "absence.verify", "release.record"]
    return ["inspect", "diff", "policy.evaluate"]
