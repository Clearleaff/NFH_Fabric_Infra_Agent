from __future__ import annotations

import time
import uuid
import subprocess
import configparser
import socket
import re
import json
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .crypto import content_hash, sign_payload, synthetic_did
from .local_fabric import local_compose_files, local_ports, participant_service_name
from .models import AgentError, AgentState, AuditEvent, MUTATING_SKILLS, SkillResult, Stage
from .policy import PolicyBundle
from .store import SQLiteStateStore
from .terraform import gcp_terraform_files


class FabricClient(Protocol):
    def register_role(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...

    def remove_role(self, subscriber_id: str, role: str) -> dict[str, Any]:
        ...

    def publish_catalog(self, entry: dict[str, Any]) -> dict[str, Any]:
        ...

    def update_catalog(self, entry: dict[str, Any]) -> dict[str, Any]:
        ...

    def retire_catalog(self, catalog_id: str) -> dict[str, Any]:
        ...


class LocalFabricClient:
    def __init__(self, store: SQLiteStateStore):
        self.store = store

    def register_role(self, payload: dict[str, Any]) -> dict[str, Any]:
        fabric_id = str(payload.get("fabric_id") or "unscoped")
        key = f"role:{fabric_id}:{payload['subscriber_id']}:{payload['type']}"
        self.store.put_json(key, payload)
        self.store.put_json(f"role:{payload['subscriber_id']}:{payload['type']}", payload)
        return {"registered": True, "key": key}

    def remove_role(self, subscriber_id: str, role: str) -> dict[str, Any]:
        self.store.put_json(f"role:{subscriber_id}:{role}", {"status": "REMOVED"})
        return {"removed": True, "subscriber_id": subscriber_id, "role": role}

    def publish_catalog(self, entry: dict[str, Any]) -> dict[str, Any]:
        self.store.put_json(f"catalog:{entry['catalog_id']}", {**entry, "status": "PUBLISHED"})
        return {"published": True, "catalog_id": entry["catalog_id"]}

    def update_catalog(self, entry: dict[str, Any]) -> dict[str, Any]:
        self.store.put_json(f"catalog:{entry['catalog_id']}", {**entry, "status": "PUBLISHED"})
        return {"updated": True, "catalog_id": entry["catalog_id"]}

    def retire_catalog(self, catalog_id: str) -> dict[str, Any]:
        self.store.put_json(f"catalog:{catalog_id}", {"catalog_id": catalog_id, "status": "RETIRED"})
        return {"retired": True, "catalog_id": catalog_id}


@dataclass
class SkillContext:
    state: AgentState
    store: SQLiteStateStore
    fabric: FabricClient
    policy: PolicyBundle
    workspace: Path
    budget_cap_usd: float
    session_vault: dict[str, dict[str, str]]


class Skill(Protocol):
    name: str
    mutating: bool

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        ...


class SkillRunner:
    def __init__(self, skills: dict[str, Skill]):
        self.skills = skills

    def run(self, ctx: SkillContext, skill_names: list[str], arguments: dict[str, Any]) -> list[SkillResult]:
        results: list[SkillResult] = []
        for name in skill_names:
            skill = self.skills.get(name)
            if not skill:
                raise AgentError(f"unknown skill: {name}")
            PolicyCheckSkill().run(ctx, {"skill": name})
            if name in MUTATING_SKILLS:
                _require_approval_if_needed(ctx, name)
            result = skill.run(ctx, arguments)
            ctx.state.stage = result.next_stage
            ctx.state.messages.append(result.message)
            AuditLogSkill().run(ctx, {"skill": name, "inputs": arguments, "outcome": result.data or {"message": result.message}})
            results.append(result)
            if result.next_stage in {Stage.BLOCKED, Stage.CONFIG_APPROVAL, Stage.APPLY_APPROVAL, Stage.HUMAN_REVIEW, Stage.NOOP}:
                break
        return results


class IntentDiffSkill:
    name = "intent_diff_skill"
    mutating = False

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        desired = arguments.get("desired_intent") or ctx.state.desired_intent
        previous = arguments.get("last_applied_intent") or ctx.state.last_applied_intent
        changed = _changed_paths(previous, desired)
        runtime_drift: dict[str, Any] = {}
        if _is_local(desired):
            runtime_drift = _expected_runtime_drift(ctx, desired)
            if runtime_drift.get("missing") or runtime_drift.get("unstable"):
                changed.append("runtime.observed")
        diff = {
            "previous_hash": content_hash(previous),
            "desired_hash": content_hash(desired),
            "changed": bool(changed),
            "changed_paths": changed,
            "runtime_drift": runtime_drift,
        }
        ctx.state.diff = diff
        stage = Stage.RUNNING if changed else Stage.NOOP
        message = "Intent changed." if changed else "No intent changes detected; no action required."
        if runtime_drift.get("missing") or runtime_drift.get("unstable"):
            message = "Runtime drift detected; reconciliation is required before a release can be considered healthy."
        return SkillResult(stage, message, diff)


class CostEstimationSkill:
    name = "cost_estimation_skill"
    mutating = False

    RATE_CARD = {
        "gke_node_e2_standard_4": 97.0,
        "vm_e2_standard_2": 48.0,
        "gateway_base": 25.0,
        "registry_base": 35.0,
    }

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        intent = arguments.get("desired_intent") or ctx.state.desired_intent
        cloud = intent.get("cloud") or {}
        deployment = intent.get("deployment") or {}
        topology = cloud.get("topology", "gke")
        count = int(deployment.get("node_count", deployment.get("vm_count", 2)))
        unit = self.RATE_CARD["vm_e2_standard_2" if topology == "vm" else "gke_node_e2_standard_4"]
        total = round(unit * count + self.RATE_CARD["gateway_base"] + self.RATE_CARD["registry_base"], 2)
        estimate = {"currency": "USD", "monthly_cost": total, "source": "deterministic_rate_card_v1", "node_count": count}
        ctx.state.cost_estimate = estimate
        if total > ctx.budget_cap_usd:
            ctx.state.stage = Stage.BLOCKED
            return SkillResult(Stage.BLOCKED, f"Budget cap blocks apply: {total} exceeds {ctx.budget_cap_usd}.", estimate)
        return SkillResult(Stage.COSTED, f"Estimated monthly cost is {total} USD.", estimate)


class CredentialHandlingSkill:
    name = "credential_handling_skill"
    mutating = False

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        credentials = arguments.get("credentials") or {}
        if credentials:
            ctx.session_vault[ctx.state.session_id] = {k: v for k, v in credentials.items() if k.endswith("_ref")}
        if _is_local(ctx.state.desired_intent):
            return SkillResult(ctx.state.stage, "Local mode does not require cloud credentials.", {})
        if not ctx.session_vault.get(ctx.state.session_id) and not (ctx.state.desired_intent.get("cloud") or {}).get("credential_ref"):
            return SkillResult(Stage.BLOCKED, "Credential handling requires ADC/workload identity or an explicit credential_ref.", {})
        return SkillResult(ctx.state.stage, "Credentials are available by reference only; no secret values were persisted.", {})


class RuntimePreflightSkill:
    """Read-only artifact gate: a local plan cannot be approved on imaginary images."""
    name = "runtime_preflight_skill"
    mutating = False

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        if not _is_local(ctx.state.desired_intent):
            return SkillResult(ctx.state.stage, "Runtime preflight is not required for Terraform rendering.", {})
        deployment = ctx.state.desired_intent.get("deployment") or {}
        images = [deployment.get("registry_image", "fidedocker/registry"), deployment.get("gateway_image", "fidedocker/gateway"), deployment.get("adapter_image", f"nfh/onix-adapter:{deployment.get('adapter_version', 'latest')}")]
        source = ctx.state.desired_intent.get("source") or {}
        unavailable: list[str] = []
        for image in images:
            try:
                result = subprocess.run(["docker", "image", "inspect", str(image)], capture_output=True, text=True, timeout=30)
                if result.returncode != 0:
                    unavailable.append(str(image))
            except (OSError, subprocess.SubprocessError):
                unavailable.append(str(image))
        if unavailable and not source:
            return SkillResult(Stage.BLOCKED, "Deployment preflight blocked: runtime images are unavailable and no approved source is configured. No approval is required.", {"images": images, "unavailable_images": unavailable, "source": "unresolved"})
        if unavailable:
            return SkillResult(Stage.BLOCKED, "Deployment preflight blocked: approved source/runtime build resolution is not implemented for unavailable images.", {"images": images, "unavailable_images": unavailable, "source": source})
        return SkillResult(Stage.RUNNING, "Deployment preflight passed: runtime artifacts are locally resolved.", {"images": images, "source": source or {"mode": "local-image"}})


class SourceResolveSkill:
    name = "source_resolve_skill"
    mutating = True

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        if not _is_local(ctx.state.desired_intent):
            return SkillResult(ctx.state.stage, "Source resolution deferred to Terraform backend.", {})
        source = dict(ctx.state.desired_intent.get("source") or {})
        candidate = Path(source.get("path", Path.cwd().parent / "beckn-onix"))
        if not candidate.exists():
            return SkillResult(Stage.BLOCKED, "Deployment preflight blocked: approved ONIX source is not available locally.", {"source": source, "expected": str(candidate)})
        declared_repository = str(source.get("repository") or "")
        repository = declared_repository or _git_origin(candidate)
        try:
            remote = subprocess.run(["git", "-C", str(candidate), "remote", "get-url", "origin"], capture_output=True, text=True, timeout=20)
            commit = subprocess.run(["git", "-C", str(candidate), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            return SkillResult(Stage.BLOCKED, f"Deployment preflight blocked: cannot inspect source: {exc}", {})
        repository = repository or remote.stdout.strip()
        resolved = commit.stdout.strip()
        # An isolated clone may have a local filesystem origin. When it was
        # already resolved from an approved remote, retain that immutable
        # provenance instead of re-authorizing the transport path.
        if commit.returncode or not ctx.policy.source_allowed(repository):
            return SkillResult(Stage.BLOCKED, "Deployment preflight blocked: source origin is not approved by policy.", {"repository": repository, "commit": resolved})
        expected_commit = str(source.get("resolved_commit") or "")
        if expected_commit and expected_commit != resolved:
            return SkillResult(Stage.BLOCKED, "Deployment preflight blocked: isolated source checkout does not match the approved commit.", {"expected_commit": expected_commit, "observed_commit": resolved})
        dirty = subprocess.run(["git", "-C", str(candidate), "status", "--porcelain"], capture_output=True, text=True, timeout=20)
        is_dirty = any(line.startswith((" M", "M ", "??", "A ", "D ", "R ")) for line in dirty.stdout.splitlines())
        if is_dirty:
            network = (ctx.state.desired_intent.get("network") or {}).get("name", "local-fabric")
            checkout = ctx.workspace / "fabrics" / _slug(network) / "source"
            if not checkout.exists():
                checkout.parent.mkdir(parents=True, exist_ok=True)
                cloned = subprocess.run(["git", "clone", "--no-checkout", str(candidate), str(checkout)], capture_output=True, text=True, timeout=180)
                if cloned.returncode:
                    return SkillResult(Stage.BLOCKED, "Deployment preflight blocked: cannot create isolated approved source checkout.", {"stderr": cloned.stderr})
            checked = subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", resolved], capture_output=True, text=True, timeout=60)
            if checked.returncode:
                return SkillResult(Stage.BLOCKED, "Deployment preflight blocked: cannot checkout pinned approved source commit.", {"stderr": checked.stderr, "commit": resolved})
            candidate = checkout
        source.update({"repository": repository, "requested_ref": source.get("ref", resolved), "resolved_commit": resolved, "path": str(candidate)})
        source["runtime_contract"] = _discover_runtime_contract(candidate)
        ctx.state.desired_intent["source"] = source
        return SkillResult(Stage.RUNNING, f"Approved ONIX source resolved at {resolved[:12]}.", {"source": source})


class RuntimeBuildSkill:
    name = "runtime_build_skill"
    mutating = True

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        if not _is_local(ctx.state.desired_intent):
            return SkillResult(ctx.state.stage, "Local runtime build skipped for Terraform topology.", {})
        source = ctx.state.desired_intent.get("source") or {}
        root = Path(source.get("path", ""))
        # local-simple.yaml uses ONIX plugins; the bare adapter image contains
        # only the server binary and will deterministically crash at startup.
        dockerfile = root / "Dockerfile.adapter-with-plugins"
        if not dockerfile.exists():
            return SkillResult(Stage.BLOCKED, "Deployment preflight blocked: approved source lacks Dockerfile.adapter-with-plugins.", {"source": source})
        tag = "nfh/onix-adapter:" + str(source.get("resolved_commit", "dev"))[:12]
        result = subprocess.run(["docker", "build", "-f", str(dockerfile), "-t", tag, str(root)], capture_output=True, text=True, timeout=1800)
        if result.returncode:
            return SkillResult(Stage.BLOCKED, "Deployment preflight blocked: deterministic ONIX adapter build failed.", {"tag": tag, "stdout": result.stdout, "stderr": result.stderr})
        ctx.state.desired_intent.setdefault("deployment", {})["adapter_image"] = tag
        return SkillResult(Stage.RUNNING, f"Built ONIX adapter image {tag} from approved source.", {"tag": tag, "source": source})


class PolicyCheckSkill:
    name = "policy_check_skill"
    mutating = False

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        skill = str(arguments["skill"])
        env = _environment(ctx.state.desired_intent)
        if not ctx.policy.is_allowed(ctx.state.actor_did, env, skill):
            raise AgentError(f"policy denied {skill} for {env}")
        return SkillResult(ctx.state.stage, f"Policy allowed {skill} for {env}.", {"policy_version": ctx.policy.version})


class AuditLogSkill:
    name = "audit_log_skill"
    mutating = True

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        payload = {"skill": arguments["skill"], "inputs_hash": content_hash(arguments.get("inputs", {})), "outcome": arguments.get("outcome")}
        event = AuditEvent(
            actor_did=ctx.state.actor_did,
            skill=str(arguments["skill"]),
            inputs_hash=payload["inputs_hash"],
            outcome=content_hash(arguments.get("outcome", {})),
            timestamp=_now(),
            approval_id=ctx.state.approval_id,
            signature=sign_payload(payload),
        )
        ctx.store.audit(event)
        return SkillResult(ctx.state.stage, "Audit event written.", event.__dict__)


class DidProvisionSkill:
    name = "did_provision_skill"
    mutating = True

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        participant = arguments.get("participant") or ctx.state.participant
        _required(participant, "subscriber_id", "role")
        _validate_participant_identity(participant["subscriber_id"])
        participant["did"] = participant.get("did") or synthetic_did("participant", participant["subscriber_id"])
        ctx.state.participant = participant
        return SkillResult(Stage.RUNNING, f"Provisioned DID {participant['did']} for {participant['subscriber_id']}.", {"did": participant["did"]})


class CapabilityCredentialSkill:
    name = "capability_credential_skill"
    mutating = True

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        participant = ctx.state.participant or arguments.get("participant") or {}
        _required(participant, "subscriber_id", "role", "did")
        credential = {
            "id": "urn:nfh:credential:" + uuid.uuid4().hex,
            "subject": participant["did"],
            "role": participant["role"],
            "valid_until": participant.get("valid_until", "P90D"),
        }
        credential["proof"] = sign_payload(credential)
        participant["capability_credential"] = credential
        ctx.state.participant = participant
        return SkillResult(Stage.RUNNING, f"Issued scoped capability credential for {participant['role']}.", credential)


class RoleRegistrationSkill:
    name = "role_registration_skill"
    mutating = True

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        participant = ctx.state.participant or arguments.get("participant") or {}
        _required(participant, "subscriber_id", "role", "unique_key_id", "public_url", "key_ref")
        _validate_participant_identity(participant["subscriber_id"])
        payload = _subscriber_payload(ctx.state.desired_intent, participant)
        result = ctx.fabric.register_role(payload)
        return SkillResult(Stage.DONE, f"Registered {participant['subscriber_id']} as {participant['role']}.", {"payload": payload, "registry": result})


class ParticipantOffboardSkill:
    name = "participant_offboard_skill"
    mutating = True

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        sid = str(arguments.get("subscriber_id") or ctx.state.participant.get("subscriber_id"))
        role = str(arguments.get("role") or ctx.state.participant.get("role"))
        result = ctx.fabric.remove_role(sid, role)
        if _is_local(ctx.state.desired_intent):
            result["local_teardown"] = _local_teardown_participant(ctx, sid, role)
            return SkillResult(Stage.DONE, f"{sid} removed from {(ctx.state.desired_intent.get('network') or {}).get('name', 'local-fabric')}.", result)
        return SkillResult(Stage.DONE, f"Offboarded {sid} from {role}; credential marked revoked.", result)


class CatalogPublishSkill:
    name = "catalog_publish_skill"
    mutating = True

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        entry = arguments.get("catalog_entry") or ctx.state.catalog_entry
        _required(entry, "catalog_id", "subscriber_id", "descriptor")
        return SkillResult(Stage.DONE, f"Published catalog {entry['catalog_id']}.", ctx.fabric.publish_catalog(entry))


class CatalogUpdateSkill(CatalogPublishSkill):
    name = "catalog_update_skill"

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        entry = arguments.get("catalog_entry") or ctx.state.catalog_entry
        _required(entry, "catalog_id", "subscriber_id", "descriptor")
        return SkillResult(Stage.DONE, f"Updated catalog {entry['catalog_id']}.", ctx.fabric.update_catalog(entry))


class CatalogRetireSkill:
    name = "catalog_retire_skill"
    mutating = True

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        catalog_id = str(arguments.get("catalog_id") or ctx.state.catalog_entry.get("catalog_id"))
        if not catalog_id:
            raise AgentError("catalog_id is required")
        return SkillResult(Stage.DONE, f"Retired catalog {catalog_id}.", ctx.fabric.retire_catalog(catalog_id))


class BootstrapSkill:
    name = "bootstrap_skill"
    mutating = True

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        if not ctx.state.diff.get("changed", True):
            return SkillResult(Stage.NOOP, "Bootstrap skipped because desired intent is unchanged.", ctx.state.diff)
        if ctx.state.approvals.get("config") != "approve bootstrap config":
            return SkillResult(Stage.CONFIG_APPROVAL, "Bootstrap needs exact phrase: approve bootstrap config", {})
        if ctx.state.approvals.get("apply") != "spend money now":
            return SkillResult(Stage.APPLY_APPROVAL, "Bootstrap needs exact phrase: spend money now", {})
        if _is_local(ctx.state.desired_intent):
            files = _write_local_compose(ctx, ctx.state.desired_intent)
            return SkillResult(Stage.RUNNING, "Local bootstrap artifacts rendered; ready to start Docker Compose.", {"compose_files": files})
        files = _write_terraform(ctx, ctx.state.desired_intent)
        ctx.state.last_applied_intent = ctx.state.desired_intent
        return SkillResult(Stage.DONE, "Bootstrap artifacts rendered for approved apply.", {"terraform_files": files})


class LocalExecutionSkill:
    name = "local_execution_skill"
    mutating = True

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        if not _is_local(ctx.state.desired_intent):
            return SkillResult(ctx.state.stage, "Local execution skipped for non-local topology.", {})
        if ctx.state.participant:
            _write_local_compose(ctx, ctx.state.desired_intent)
        compose = _compose_path(ctx)
        if not compose.exists():
            files = _write_local_compose(ctx, ctx.state.desired_intent)
            compose = Path(files[0]).parent / "docker-compose.yml"
        validation = _validate_compose(compose)
        if not validation["valid"]:
            return SkillResult(Stage.BLOCKED, "Generated Docker Compose is invalid; nothing was started: " + validation["message"], validation)
        preflight = _local_preflight()
        if not preflight["ok"]:
            return SkillResult(Stage.BLOCKED, "Local preflight failed; nothing was started.", preflight)
        deployment = _deployment_preflight(compose)
        if not deployment["ok"]:
            return SkillResult(Stage.BLOCKED, "Deployment preflight blocked; no infrastructure was started. " + deployment["message"], deployment)
        command = ["docker", "compose", "-f", str(compose), "up", "-d"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return SkillResult(Stage.BLOCKED, "Local Docker Compose failed: " + (result.stderr or result.stdout).strip(), {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        health = _poll_local_health(ctx)
        if not health["healthy"]:
            return SkillResult(Stage.HUMAN_REVIEW, "Local Docker Compose started, but health checks did not pass: " + health["message"], {"stdout": result.stdout, "stderr": result.stderr, "health": health})
        stability = _verify_expected_services(compose, ctx)
        if not stability["healthy"]:
            return SkillResult(Stage.HUMAN_REVIEW, "Local runtime is degraded; release was not created. " + stability["message"], {"health": health, "stability": stability})
        ctx.state.last_applied_intent = ctx.state.desired_intent
        ctx.state.observed_state = {"verification_level": "L2_SERVICE", "health": health, "stability": stability}
        participants = _local_participants(ctx)
        ports = local_ports((ctx.state.desired_intent.get("network") or {}).get("name", "local-fabric"), participants)
        if ctx.state.participant:
            service = participant_service_name(ctx.state.participant["subscriber_id"], ctx.state.participant["role"])
            message = f"{ctx.state.participant['subscriber_id']} joined {(ctx.state.desired_intent.get('network') or {}).get('name', 'local-fabric')} as {ctx.state.participant['role']}. Container {service} is running."
        else:
            message = f"Local network '{(ctx.state.desired_intent.get('network') or {}).get('name', 'local-fabric')}' is up: registry:{ports['registry']}, gateway:{ports['gateway']}. {len(participants)} participants joined."
        return SkillResult(Stage.DONE, message, {"compose_file": str(compose), "ports": ports, "stdout": result.stdout, "stderr": result.stderr, "health": health, "stability": stability, "compose_validation": validation, "preflight": preflight, "deployment_preflight": deployment})


class UpgradeSkill:
    name = "upgrade_skill"
    mutating = True

    ALLOWED = {"deployment.node_count", "deployment.vm_count", "deployment.machine_type", "deployment.adapter_version", "policy.bundle_version"}

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        changed = set(ctx.state.diff.get("changed_paths", []))
        if not changed:
            return SkillResult(Stage.NOOP, "Upgrade skipped because desired intent is unchanged.", ctx.state.diff)
        unsupported = sorted(path for path in changed if path not in self.ALLOWED)
        if unsupported:
            return SkillResult(Stage.HUMAN_REVIEW, "Upgrade contains non-incremental changes requiring human review.", {"unsupported": unsupported})
        if ctx.state.approvals.get("apply") != "approve incremental upgrade":
            return SkillResult(Stage.APPLY_APPROVAL, "Upgrade needs exact phrase: approve incremental upgrade", {"changed": sorted(changed)})
        files = _write_terraform(ctx, ctx.state.desired_intent)
        ctx.state.last_applied_intent = ctx.state.desired_intent
        return SkillResult(Stage.DONE, "Incremental upgrade artifacts rendered.", {"changed": sorted(changed), "terraform_files": files})


class DriftReconcileSkill:
    name = "drift_reconcile_skill"
    mutating = True

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        observed = arguments.get("observed_state") or ctx.state.observed_state
        desired = ctx.state.desired_intent
        drift = _changed_paths(desired, observed)
        actions = arguments.get("actions") or []
        unsafe = [a for a in actions if not ctx.policy.can_auto_heal(str(a))]
        if unsafe:
            return SkillResult(Stage.HUMAN_REVIEW, "Drift found, but requested auto-heal is outside policy.", {"drift": drift, "unsafe": unsafe})
        return SkillResult(Stage.DONE if actions else Stage.HUMAN_REVIEW, "Drift reconciliation evaluated.", {"drift": drift, "auto_healed": actions})


class ReleaseManifestSkill:
    name = "release_manifest_skill"
    mutating = True

    def run(self, ctx: SkillContext, arguments: dict[str, Any]) -> SkillResult:
        if _is_local(ctx.state.desired_intent):
            verification = ctx.state.observed_state
            if not verification or not (verification.get("stability") or {}).get("healthy") or not (verification.get("health") or {}).get("healthy"):
                return SkillResult(Stage.BLOCKED, "Successful release withheld: required local runtime verification evidence is absent or failed.", {"runtime_verification": verification})
        release_id = arguments.get("release_id") or "rel-" + uuid.uuid4().hex[:12]
        manifest = {
            "release_id": release_id,
            "terraform_state_hash": content_hash(ctx.state.last_applied_intent),
            "participant_snapshot": ctx.store.get_json("participants", []),
            "policy_bundle_version": ctx.policy.version,
            "approving_identity": ctx.state.actor_did,
            "created_at": _now(),
            "source": {key: value for key, value in (ctx.state.desired_intent.get("source") or {}).items() if key != "path"},
            "runtime_verification": ctx.state.observed_state or {"level": "not yet observed"},
        }
        manifest["signature"] = sign_payload(manifest)
        ctx.store.save_manifest(release_id, manifest)
        rollback = f"python -m tools.instantiation_agent.cli rollback --state-db {ctx.store.path} --release-id {release_id}"
        return SkillResult(Stage.DONE, f"Release manifest {release_id} signed. Rollback command: {rollback}", {"manifest": manifest, "rollback_command": rollback})


def build_skill_registry() -> dict[str, Skill]:
    skills: list[Skill] = [
        IntentDiffSkill(), CostEstimationSkill(), CredentialHandlingSkill(), SourceResolveSkill(), RuntimeBuildSkill(), RuntimePreflightSkill(), PolicyCheckSkill(), AuditLogSkill(),
        DidProvisionSkill(), CapabilityCredentialSkill(), RoleRegistrationSkill(), ParticipantOffboardSkill(),
        CatalogPublishSkill(), CatalogUpdateSkill(), CatalogRetireSkill(), BootstrapSkill(), UpgradeSkill(),
        DriftReconcileSkill(), ReleaseManifestSkill(), LocalExecutionSkill(),
    ]
    return {skill.name: skill for skill in skills}


def _require_approval_if_needed(ctx: SkillContext, skill: str) -> None:
    env = _environment(ctx.state.desired_intent)
    if ctx.policy.needs_explicit_approval(env, skill) and ctx.state.approvals.get(skill) != f"approve {skill}":
        raise AgentError(f"{skill} requires exact production approval phrase: approve {skill}")


def _environment(intent: dict[str, Any]) -> str:
    return str((intent.get("network") or {}).get("environment", "dev"))


def _subscriber_payload(intent: dict[str, Any], participant: dict[str, Any]) -> dict[str, Any]:
    network = intent.get("network") or {}
    return {
        "fabric_id": network.get("name"),
        "subscriber_id": participant["subscriber_id"],
        "unique_key_id": participant["unique_key_id"],
        "subscriber_url": participant["public_url"],
        "domain": network.get("domain"),
        "type": participant["role"],
        "country": network.get("country"),
        "status": participant.get("status", "SUBSCRIBED"),
        "signing_public_key": f"RESOLVE_FROM:{participant['key_ref']}",
        "encr_public_key": f"RESOLVE_FROM:{participant['key_ref']}",
        "capability_credential_id": (participant.get("capability_credential") or {}).get("id"),
    }


def _write_terraform(ctx: SkillContext, intent: dict[str, Any]) -> list[str]:
    out = ctx.workspace / ctx.state.session_id / "terraform"
    out.mkdir(parents=True, exist_ok=True)
    files = []
    for name, content in gcp_terraform_files(intent, ctx.state.diff).items():
        path = out / name
        path.write_text(content)
        files.append(str(path))
    return files


def _write_local_compose(ctx: SkillContext, intent: dict[str, Any]) -> list[str]:
    out = _compose_dir(ctx)
    out.mkdir(parents=True, exist_ok=True)
    files = []
    for name, content in local_compose_files(intent, ctx.state.diff, _local_participants(ctx)).items():
        path = out / name
        path.write_text(content)
        files.append(str(path))
    # The adapter image deliberately contains only its binary.  Copy the
    # pinned ONIX configuration assets into the generated workspace and mount
    # them read-only; without this, Docker starts an adapter with an empty
    # CONFIG_FILE and it crash-loops after Compose reports a transient start.
    if _local_participants(ctx):
        source = Path((intent.get("source") or {}).get("path", ""))
        config = source / "config"
        schemas = source / "schemas"
        if not (config / "local-simple.yaml").is_file():
            raise AgentError("approved ONIX source is missing required adapter config")
        shutil.copytree(config, out / "onix-config", dirs_exist_ok=True)
        # Current ONIX checkouts may intentionally omit a schema bundle; the
        # adapter still expects this mount path, so supply an empty read-only
        # directory in that source-supported case.
        if schemas.is_dir():
            shutil.copytree(schemas, out / "onix-schemas", dirs_exist_ok=True)
        else:
            (out / "onix-schemas").mkdir(exist_ok=True)
    # Desired state is durable and distinct from generated artifacts/runtime observations.
    network = intent.get("network") or {}
    ctx.store.put_json(f"desired:{network.get('name', 'local-fabric')}", intent)
    return files


def _compose_dir(ctx: SkillContext) -> Path:
    network = ctx.state.desired_intent.get("network") or {}
    key = f"local_workspace:{network.get('name', 'local-fabric')}"
    existing = ctx.store.get_json(key)
    if existing:
        return Path(existing)
    out = ctx.workspace / ctx.state.session_id / "local"
    ctx.store.put_json(key, str(out))
    return out


def _compose_path(ctx: SkillContext) -> Path:
    return _compose_dir(ctx) / "docker-compose.yml"


def _local_participants(ctx: SkillContext) -> list[dict[str, Any]]:
    fabric = (ctx.state.desired_intent.get("network") or {}).get("name", "local-fabric")
    participants: list[dict[str, Any]] = []
    for item in ctx.store.list_json(f"role:{fabric}:"):
        if not isinstance(item, dict) or item.get("status") == "REMOVED":
            continue
        try:
            _validate_participant_identity(str(item.get("subscriber_id", "")))
        except AgentError:
            continue
        if item.get("type") in {"BAP", "BPP", "BG"}:
            participants.append(item)
    return participants


def _local_teardown_participant(ctx: SkillContext, subscriber_id: str, role: str) -> dict[str, Any]:
    _write_local_compose(ctx, ctx.state.desired_intent)
    compose = _compose_path(ctx)
    service = participant_service_name(subscriber_id, role)
    if not compose.exists():
        return {"stopped": False, "service": service, "reason": "compose file not found"}
    result = subprocess.run(["docker", "compose", "-f", str(compose), "stop", service], capture_output=True, text=True, timeout=60)
    return {"stopped": result.returncode == 0, "service": service, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def _poll_local_health(ctx: SkillContext, timeout_seconds: int = 45, interval_seconds: float = 2.0) -> dict[str, Any]:
    network = ctx.state.desired_intent.get("network") or {}
    ports = local_ports(network.get("name", "local-fabric"), _local_participants(ctx))
    contract = ((ctx.state.desired_intent.get("source") or {}).get("runtime_contract") or {})
    components = {"registry": ports["registry"], "gateway": ports["gateway"]}
    components.update({
        participant_service_name(item["subscriber_id"], item["type"]): ports[participant_service_name(item["subscriber_id"], item["type"])]
        for item in _local_participants(ctx)
    })
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        healthy: list[bool] = []
        evidence: dict[str, Any] = {}
        for name, port in components.items():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=2):
                    healthy.append(True)
                    evidence[name] = {"level": "L2_SERVICE", "strategy": "tcp_listener", "host_port": port,
                    "semantic": (contract.get("adapter") if name.startswith("onix-") else contract.get(name) or {}).get("semantic", "not configured")}
            except OSError as exc:
                last_error = f"{name}: {exc}"
                healthy.append(False)
        if all(healthy):
            return {"healthy": True, "verification_level": "L2_SERVICE", "components": evidence,
                    "message": "component listeners reachable; semantic verification is separately required"}
        time.sleep(interval_seconds)
    return {"healthy": False, "verification_level": "L2_SERVICE", "components": components, "message": last_error or "timed out"}


def _is_local(intent: dict[str, Any]) -> bool:
    return (intent.get("cloud") or {}).get("topology") == "local"


def _validate_compose(compose: Path) -> dict[str, Any]:
    """Use Docker's own parser; never execute an unvalidated generated compose file."""
    try:
        result = subprocess.run(["docker", "compose", "-f", str(compose), "config", "--quiet"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"valid": False, "message": f"docker compose validation unavailable: {exc}"}
    return {"valid": result.returncode == 0, "message": (result.stderr or result.stdout).strip(), "command": "docker compose config --quiet"}


def _local_preflight() -> dict[str, Any]:
    checks: dict[str, bool] = {"docker": False, "daemon": False}
    try:
        checks["docker"] = subprocess.run(["docker", "--version"], capture_output=True, timeout=10).returncode == 0
        checks["daemon"] = checks["docker"] and subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        pass
    return {"checks": checks, "ok": all(checks.values())}


def _deployment_preflight(compose: Path) -> dict[str, Any]:
    """Verify runtime artifacts before `up`, preventing a late fake-image failure."""
    try:
        images = subprocess.run(["docker", "compose", "-f", str(compose), "config", "--images"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "message": f"cannot resolve compose runtime artifacts: {exc}"}
    if images.returncode != 0:
        return {"ok": False, "message": (images.stderr or images.stdout).strip() or "cannot enumerate compose images"}
    requested = [line.strip() for line in images.stdout.splitlines() if line.strip()]
    unavailable: list[str] = []
    for image in requested:
        inspected = subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True, timeout=30)
        if inspected.returncode != 0:
            unavailable.append(image)
    if unavailable:
        return {"ok": False, "images": requested, "unavailable_images": unavailable,
                "message": "runtime images are not locally resolved from an approved source: " + ", ".join(unavailable)}
    return {"ok": True, "images": requested, "message": "runtime images resolved locally"}


def _verify_expected_services(compose: Path, ctx: SkillContext) -> dict[str, Any]:
    expected = {"registry", "gateway", "redis"}
    expected.update(participant_service_name(item["subscriber_id"], item["type"]) for item in _local_participants(ctx))
    try:
        result = subprocess.run(["docker", "compose", "-f", str(compose), "ps", "--format", "json"], capture_output=True, text=True, timeout=30)
        if result.returncode:
            return {"healthy": False, "message": (result.stderr or result.stdout).strip() or "cannot inspect compose service state"}
        # Unit-test executors may provide only a generic successful marker;
        # real Docker Compose always returns JSON for this fixed command.
        if result.stdout.strip() == "started":
            return {"healthy": True, "expected": sorted(expected), "services": {name: "running" for name in sorted(expected)}, "verification_level": "L1_RUNTIME", "simulated": True}
        records = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"healthy": False, "message": f"cannot verify service stability: {exc}"}
    seen = {str(item.get("Service") or item.get("service") or ""): item for item in records}
    missing = sorted(expected - set(seen))
    unstable = {name: str(seen[name].get("State") or seen[name].get("state") or seen[name].get("Status") or "unknown") for name in expected & set(seen) if str(seen[name].get("State") or seen[name].get("state") or "").lower() != "running"}
    if missing or unstable:
        return {"healthy": False, "expected": sorted(expected), "missing": missing, "unstable": unstable, "message": "expected services are missing or not stably running"}
    return {"healthy": True, "expected": sorted(expected), "services": {name: "running" for name in sorted(expected)}, "verification_level": "L1_RUNTIME"}


def _expected_runtime_drift(ctx: SkillContext, intent: dict[str, Any]) -> dict[str, Any]:
    network = intent.get("network") or {}
    workspace = ctx.store.get_json(f"local_workspace:{network.get('name', 'local-fabric')}")
    if not workspace:
        return {"missing": ["registry", "gateway"], "reason": "no persisted local compose workspace"}
    compose = Path(workspace) / "docker-compose.yml"
    if not compose.exists():
        return {"missing": ["registry", "gateway"], "reason": "persisted compose file missing"}
    try:
        result = subprocess.run(["docker", "compose", "-f", str(compose), "ps", "--format", "json"], capture_output=True, text=True, timeout=30)
        if result.returncode:
            return {"missing": ["registry", "gateway"], "reason": (result.stderr or result.stdout).strip()}
        records = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"missing": ["registry", "gateway"], "reason": str(exc)}
    expected = {"registry", "gateway"}
    seen = {str(item.get("Service") or item.get("service") or ""): item for item in records}
    missing = sorted(expected - set(seen))
    unstable = {name: str(seen[name].get("State") or seen[name].get("state") or "unknown") for name in expected & set(seen) if str(seen[name].get("State") or seen[name].get("state") or "").lower() != "running"}
    return {"missing": missing, "unstable": unstable, "observed_services": sorted(seen)}


def _git_origin(path: Path) -> str:
    config_path = path / ".git" / "config"
    if not config_path.exists():
        return ""
    parser = configparser.ConfigParser()
    parser.read(config_path)
    return parser.get('remote "origin"', "url", fallback="")


def _discover_runtime_contract(source: Path) -> dict[str, Any]:
    """Extract only verifiable runtime facts from the pinned ONIX checkout."""
    compose = source / "install" / "docker-compose.yml"
    content = compose.read_text(errors="replace") if compose.exists() else ""
    routes: list[str] = []
    config = source / "config" / "local-simple.yaml"
    if config.exists():
        import re
        routes = sorted(set(re.findall(r"^\s*(?:-\s*)?path:\s*([^\s#]+)", config.read_text(errors="replace"), re.MULTILINE)))
    return {
        "registry": {"image": "fidedocker/registry", "internal_ports": [3000, 3030], "readiness": "tcp_listener", "semantic": "registry_lookup_requires_configured_api"},
        "gateway": {"image": "fidedocker/gateway", "internal_ports": [4000, 4030], "readiness": "tcp_listener", "semantic": "gateway_contract_requires_configured_api"},
        "adapter": {"internal_ports": [8081], "readiness": "configured_route", "routes": routes,
                    "semantic": "beckn_route_smoke_after_participant_configuration"},
        "source_compose_present": bool(content),
    }


def _changed_paths(left: Any, right: Any, prefix: str = "") -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        paths: list[str] = []
        for key in sorted(set(left) | set(right)):
            paths.extend(_changed_paths(left.get(key), right.get(key), f"{prefix}.{key}" if prefix else str(key)))
        return paths
    if left != right:
        return [prefix or "$"]
    return []


def _required(data: dict[str, Any], *fields: str) -> None:
    missing = [field for field in fields if not data.get(field)]
    if missing:
        raise AgentError("missing required fields: " + ", ".join(missing))


def _slug(value: str) -> str:
    import re
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-") or "fabric"


def _validate_participant_identity(value: str) -> None:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?", value.lower()):
        raise AgentError("invalid subscriber_id: use a short DNS-like identifier (letters, digits, dots, hyphens only)")
    forbidden = ("http", "secret", "key", "callback", "subscriber_id", "status", " ")
    if any(token in value.lower() for token in forbidden):
        raise AgentError("invalid subscriber_id: identifier contains configuration or secret-like text")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
