from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AgentError(Exception):
    """User-facing instantiation-agent error."""


class Lifecycle(str, Enum):
    BOOTSTRAP = "bootstrap"
    PARTICIPANT = "participant"
    CATALOG = "catalog"
    UPGRADE = "upgrade"
    DRIFT = "drift"
    DIAGNOSTIC = "diagnostic"
    STATUS = "status"


class Stage(str, Enum):
    INTAKE = "intake"
    COSTED = "costed"
    CONFIG_APPROVAL = "config_approval"
    APPLY_APPROVAL = "apply_approval"
    RUNNING = "running"
    DONE = "done"
    NOOP = "noop"
    BLOCKED = "blocked"
    HUMAN_REVIEW = "human_review"
    PLANNED = "planned"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RiskClass(str, Enum):
    READ_ONLY = "read_only"
    LOW_RISK_MUTATION = "low_risk_mutation"
    NETWORK_MUTATION = "network_mutation"
    HIGH_RISK_MUTATION = "high_risk_mutation"
    DESTRUCTIVE = "destructive"


class TurnKind(str, Enum):
    """Conversational act, intentionally separate from infrastructure intent."""
    EXECUTE_INTENT = "execute_intent"
    REQUIREMENTS_QUESTION = "requirements_question"
    EXPLANATION_QUESTION = "explanation_question"
    WORKFLOW_DATA = "workflow_data"
    CONTROL_COMMAND = "control_command"
    APPROVAL = "approval"
    CANCEL = "cancel"
    GREETING = "greeting"
    HELP = "help"


@dataclass(frozen=True)
class FabricSpec:
    """Canonical desired state.  Conversation arguments are never persisted as this type."""
    fabric: dict[str, Any]
    source: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    registry: dict[str, Any] = field(default_factory=lambda: {"enabled": True})
    gateway: dict[str, Any] = field(default_factory=lambda: {"enabled": True})
    participants: tuple[dict[str, Any], ...] = ()
    catalogs: tuple[dict[str, Any], ...] = ()
    security: dict[str, Any] = field(default_factory=dict)
    observability: dict[str, Any] = field(default_factory=dict)
    requested_composition: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"fabric": self.fabric, "source": self.source, "runtime": self.runtime,
                "registry": self.registry, "gateway": self.gateway,
                "participants": list(self.participants), "catalogs": list(self.catalogs),
                "security": self.security, "observability": self.observability,
                "requested_composition": self.requested_composition}


@dataclass(frozen=True)
class ExecutionPlan:
    plan_id: str
    lifecycle: str
    actor_did: str
    desired_hash: str
    observed_hash: str
    diff_hash: str
    actions: tuple[str, ...]
    risk: str
    policy_result: str
    required_approvals: tuple[str, ...]
    policy_version: str
    plan_hash: str


MUTATING_SKILLS = {
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
    "release_manifest_skill",
}


@dataclass
class AuditEvent:
    actor_did: str
    skill: str
    inputs_hash: str
    outcome: str
    timestamp: str
    signature: str
    approval_id: str | None = None


@dataclass
class SkillResult:
    next_stage: Stage
    message: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentState:
    session_id: str
    actor_did: str
    lifecycle: Lifecycle
    stage: Stage = Stage.INTAKE
    desired_intent: dict[str, Any] = field(default_factory=dict)
    last_applied_intent: dict[str, Any] = field(default_factory=dict)
    observed_state: dict[str, Any] = field(default_factory=dict)
    participant: dict[str, Any] = field(default_factory=dict)
    catalog_entry: dict[str, Any] = field(default_factory=dict)
    diff: dict[str, Any] = field(default_factory=dict)
    cost_estimate: dict[str, Any] = field(default_factory=dict)
    approvals: dict[str, str] = field(default_factory=dict)
    approval_id: str | None = None
    messages: list[str] = field(default_factory=list)
