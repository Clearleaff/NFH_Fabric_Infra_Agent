from __future__ import annotations

import json
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent import InstantiationAgent
from .llm import GroqLLM, LLMProvider
from .models import AgentError, Stage, TurnKind


SUPPORTED_INTENTS = [
    "bootstrap",
    "onboard_participant",
    "join_network",
    "offboard_participant",
    "publish_catalog",
    "update_catalog",
    "retire_catalog",
    "upgrade",
    "rollback",
    "status",
]

APPROVAL_PHRASES = {
    "approve bootstrap config",
    "spend money now",
    "approve incremental upgrade",
    "approve did_provision_skill",
    "approve capability_credential_skill",
    "approve role_registration_skill",
    "approve participant_offboard_skill",
    "approve catalog_publish_skill",
    "approve catalog_update_skill",
    "approve catalog_retire_skill",
    "approve bootstrap_skill",
    "approve local_execution_skill",
    "approve upgrade_skill",
    "approve drift_reconcile_skill",
}


@dataclass
class ChatResponse:
    session_id: str
    text: str
    stage: str
    intent: str | None
    data: dict[str, Any]


class InstantiationChatService:
    def __init__(
        self,
        *,
        agent: InstantiationAgent | None = None,
        llm: LLMProvider | None = None,
        state_db: Path = Path(".instantiation-agent/state.sqlite3"),
        workspace: Path = Path(".instantiation-agent/work"),
        budget_cap_usd: float = 500.0,
    ):
        self.agent = agent or InstantiationAgent(state_db=state_db, workspace=workspace, budget_cap_usd=budget_cap_usd)
        self.llm = llm or GroqLLM()

    def chat(self, session_id: str | None, message: str) -> ChatResponse:
        sid = session_id or uuid.uuid4().hex
        session = self._load_session(sid)
        clean = message.strip()
        if not clean:
            raise AgentError("message is required")
        operations = _multiple_mutating_intents(clean)
        if len(operations) > 1:
            return ChatResponse(sid, "I found multiple operations in that message: " + ", ".join(operations) + ". I have not changed anything; tell me which operation to perform.", session.get("stage", "collecting"), "clarification", {"operations": operations, "mutating": False})
        turn = _classify_turn(clean)
        if turn == TurnKind.REQUIREMENTS_QUESTION:
            # Preserve the described Fabric as a non-executable preview.  A
            # question never creates a plan/approval or invokes skills, but a
            # following detail line must not lose its local topology.
            extracted = _extract_from_text(clean)
            if not session.get("intent"):
                session.update({"intent": "bootstrap", "arguments": extracted, "stage": "collecting", "approvals": {}, "plan_id": None, "plan_hash": None})
                self._save_session(sid, session)
            subject = _network_intent(_deep_merge(session.get("arguments") or {}, extracted))
            return ChatResponse(sid, _requirements_response(subject), "collecting", "requirements", {"subject": subject, "mutating": False})
        if turn == TurnKind.EXPLANATION_QUESTION and session.get("intent"):
            return ChatResponse(sid, _explanation_response(clean, session), session.get("stage", "collecting"), "explanation", {"mutating": False, "pending_intent": session.get("intent")})
        control = self._control_response(sid, session, clean)
        if control is not None:
            return control
        if _is_help_question(clean):
            return ChatResponse(sid, _help_text(), session.get("stage", "collecting"), "help", {"intents": SUPPORTED_INTENTS})
        if clean in APPROVAL_PHRASES:
            return self._handle_approval(sid, session, clean)
        if session.get("stage") in {"config_approval", "apply_approval"}:
            update = _extract_from_text(clean)
            if update and not _has_explicit_intent(clean):
                session["arguments"] = _deep_merge(session.get("arguments") or {}, update)
                session.update({"approvals": {}, "plan_id": None, "plan_hash": None, "stage": "collecting"})
                self._save_session(sid, session)
                return self._execute(sid, session, str(session.get("intent")), session.get("arguments") or {})
            return self._execute(sid, session, str(session.get("intent")), session.get("arguments") or {})
        if session.get("stage") == "collecting" and session.get("intent") and not _has_explicit_intent(clean):
            intent = str(session["intent"])
            arguments = _deep_merge(session.get("arguments") or {}, _extract_from_text(clean))
            missing = _missing_fields(intent, arguments)
            if missing:
                session.update({"arguments": arguments, "missing": missing})
                self._save_session(sid, session)
                return ChatResponse(sid, _clarifying_question(intent, missing), "collecting", intent, {"missing": missing})
            session["arguments"] = arguments
            self._save_session(sid, session)
            try:
                return self._execute(sid, session, intent, arguments)
            except AgentError as exc:
                return ChatResponse(sid, str(exc), "collecting", intent, {"error": str(exc)})

        try:
            parsed = self.llm.parse_intent(clean, intent_schema())
        except RuntimeError as exc:
            # Groq is preferred for free-form extraction, but a missing/unavailable
            # provider must not turn deterministic local intake into a dead REPL.
            # The fallback only selects a conservative known intent; it never executes.
            if "GROQ_API_KEY" not in str(exc) and "Groq request failed" not in str(exc):
                raise
            parsed = {"intent": _intent_from_text(clean), "arguments": {}}
        parsed = _normalize_parsed(parsed, clean)
        intent = parsed["intent"]
        if session.get("stage") == "collecting" and session.get("intent") and not _has_explicit_intent(clean):
            intent = str(session["intent"])
        # A fresh explicit operation must not inherit fields from a completed
        # or unrelated workflow. Detail lines still merge through the branch above.
        base = {} if _is_new_mutating_intent(clean, intent) else (session.get("arguments") or {})
        arguments = _deep_merge(base, parsed.get("arguments") or {})
        session.update({"intent": intent, "arguments": arguments, "stage": "collecting", "plan_id": None, "plan_hash": None, "approvals": {}})

        if intent == "bootstrap" and _is_generic_fabric_request(clean) and not (arguments.get("cloud") or {}).get("topology"):
            self._save_session(sid, session)
            return ChatResponse(sid, "Before I collect infrastructure details, choose a runtime topology: local Docker or GCP/Terraform. No infrastructure has been changed.", "collecting", intent, {"missing": ["runtime.topology"]})

        missing = _missing_fields(intent, arguments)
        if missing:
            session["missing"] = missing
            self._save_session(sid, session)
            return ChatResponse(sid, _clarifying_question(intent, missing), "collecting", intent, {"missing": missing})

        self._save_session(sid, session)
        try:
            response = self._execute(sid, session, intent, arguments)
        except AgentError as exc:
            return ChatResponse(sid, str(exc), "collecting", intent, {"error": str(exc)})
        return response

    def _handle_approval(self, session_id: str, session: dict[str, Any], phrase: str) -> ChatResponse:
        intent = session.get("intent")
        if not intent or session.get("stage") in {"done", "cancelled", "blocked", "human_review", "noop"}:
            return ChatResponse(session_id, "I do not have a pending action waiting for approval.", "collecting", None, {})
        plan = self._refresh_plan(session)
        if plan and session.get("plan_hash") and session["plan_hash"] != plan["plan_hash"]:
            session.update({"approvals": {}, "plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"]})
            self._save_session(session_id, session)
            return ChatResponse(session_id, "The pending plan changed; previous approval is invalid. Review the new plan.", "planned", str(intent), {"plan": plan})
        approvals = session.setdefault("approvals", {})
        if phrase == "approve bootstrap config":
            approvals["config"] = phrase
        elif phrase == "spend money now":
            approvals["apply"] = phrase
        elif phrase == "approve incremental upgrade":
            approvals["apply"] = phrase
        elif phrase.startswith("approve "):
            approvals[phrase.replace("approve ", "", 1)] = phrase
        session["approvals"] = approvals
        if plan:
            session["plan_id"], session["plan_hash"] = plan["plan_id"], plan["plan_hash"]
            self.agent.store.record_approval({"plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"], "actor": self.agent.actor_did, "phrase": phrase, "policy_version": self.agent.policy.version})
        missing = _missing_fields(intent, session.get("arguments") or {})
        if missing:
            self._save_session(session_id, session)
            return ChatResponse(session_id, _clarifying_question(intent, missing), "collecting", intent, {"missing": missing})
        self._save_session(session_id, session)
        try:
            return self._execute(session_id, session, intent, session.get("arguments") or {})
        except AgentError as exc:
            return ChatResponse(session_id, str(exc), "collecting", intent, {"error": str(exc)})

    def _execute(self, session_id: str, session: dict[str, Any], intent: str, arguments: dict[str, Any]) -> ChatResponse:
        approvals = session.get("approvals") or {}
        if intent == "bootstrap":
            state = self.agent.bootstrap(_network_intent(arguments), approvals)
            response = _state_response(session_id, intent, state)
        elif intent == "onboard_participant":
            network_intent = _network_intent(arguments)
            if (network_intent.get("cloud") or {}).get("topology") == "local":
                state = self.agent.join_network(network_intent, _participant(arguments), approvals)
                intent = "join_network"
            else:
                state = self.agent.onboard_participant(network_intent, _participant(arguments), approvals)
            response = _state_response(session_id, intent, state)
        elif intent == "join_network":
            state = self.agent.join_network(_network_intent(arguments), _participant(arguments), approvals)
            response = _state_response(session_id, intent, state)
        elif intent == "offboard_participant":
            state = self.agent.offboard_participant(_network_intent(arguments), arguments["subscriber_id"], arguments["role"], approvals)
            response = _state_response(session_id, intent, state)
        elif intent in {"publish_catalog", "update_catalog", "retire_catalog"}:
            action = {"publish_catalog": "publish", "update_catalog": "update", "retire_catalog": "retire"}[intent]
            state = self.agent.publish_catalog(_network_intent(arguments), _catalog_entry(arguments), action, approvals)
            response = _state_response(session_id, intent, state)
        elif intent == "upgrade":
            state = self.agent.upgrade(arguments["previous_intent"], arguments["desired_intent"], approvals)
            response = _state_response(session_id, intent, state)
        elif intent == "rollback":
            result = self.agent.rollback(arguments["release_id"])
            response = ChatResponse(session_id, f"Rollback requested for {arguments['release_id']}.", "done", intent, result)
        elif intent == "status":
            response = ChatResponse(session_id, _status_text(self.agent, session), session.get("stage", "collecting"), intent, _session_snapshot(session))
        else:
            raise AgentError(f"unsupported intent: {intent}")

        session["stage"] = response.stage
        session["last_response"] = response.text
        session["data"] = response.data
        # Source resolution/build is deterministic pre-approval preparation.
        # Persist the resulting canonical desired state so the displayed plan
        # and the approval-time plan hash describe the same pinned artifacts.
        resolved = ((response.data.get("state") or {}).get("desired_intent") if isinstance(response.data, dict) else None)
        if intent == "bootstrap" and isinstance(resolved, dict):
            session["arguments"] = resolved
        self._save_session(session_id, session)
        if intent not in {"status", "rollback"} and response.stage in {"config_approval", "apply_approval", "collecting"}:
            plan = self._refresh_plan(session)
            if plan:
                session.update({"plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"]})
                response.data["plan"] = plan
                self._save_session(session_id, session)
        return response

    def _refresh_plan(self, session: dict[str, Any]) -> dict[str, Any] | None:
        intent = session.get("intent")
        if not intent or intent in {"status", "rollback"}:
            return None
        lifecycle = "participant" if intent in {"onboard_participant", "join_network"} else str(intent)
        return self.agent.plan(lifecycle, _network_intent(session.get("arguments") or {}))

    def _control_response(self, sid: str, session: dict[str, Any], clean: str) -> ChatResponse | None:
        text = clean.lower().strip()
        if text in {"hi", "hello", "hey"}:
            return ChatResponse(sid, "Hello — I am a governed NFH Fabric SRE. Ask `help` or describe the Fabric you want.", session.get("stage", "collecting"), "help", {})
        if text in {"cancel", "cancle", "start over"}:
            session.update({"intent": None, "arguments": {}, "approvals": {}, "stage": "cancelled", "plan_id": None, "plan_hash": None})
            self._save_session(sid, session)
            return ChatResponse(sid, "Pending operation cancelled. No infrastructure was changed by this command.", "cancelled", "cancel", {})
        if text in {"run", "execute", "go"}:
            return ChatResponse(
                sid,
                "There is no executable `run` command in chat. Use `show plan` to review a pending operation, then provide its exact approval phrase; or describe the next operation.",
                session.get("stage", "collecting"),
                "clarification",
                {"mutating": False},
            )
        if text in {"show plan", "show diff", "why is approval required?", "why do you need approval"}:
            plan = self._refresh_plan(session)
            if not plan: return ChatResponse(sid, "There is no pending plan.", session.get("stage", "collecting"), None, {})
            session.update({"plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"]})
            self._save_session(sid, session)
            if text.startswith("why"):
                return ChatResponse(sid, f"Approval is required for {plan['risk'].replace('_', ' ')} actions and is bound to immutable {plan['plan_id']} ({plan['plan_hash'][:12]}).", session.get("stage", "planned"), session.get("intent"), {"plan": plan})
            if text == "show diff":
                return ChatResponse(sid, f"Desired: {plan['desired_hash']}\nObserved: {plan['observed_hash']}\nDiff: {plan['diff_hash']}", session.get("stage", "planned"), session.get("intent"), {"plan": plan})
            actions = "\n".join(f"{i}. {v}" for i, v in enumerate(plan["actions"], 1))
            return ChatResponse(sid, f"{plan['plan_id']}\n\n{actions}\n\nRisk: {plan['risk']}\nPolicy: {plan['policy_result']}", session.get("stage", "planned"), session.get("intent"), {"plan": plan})
        if text in {"show current intent", "show missing fields"}:
            missing = _missing_fields(str(session.get("intent") or ""), session.get("arguments") or {})
            return ChatResponse(sid, f"Pending intent: {session.get('intent') or 'none'}. Missing: {', '.join(missing) or 'none'}.", session.get("stage", "collecting"), session.get("intent"), {"missing": missing})
        if text == "show audit":
            events = self.agent.store.audit_events()
            return ChatResponse(sid, f"Audit events: {len(events)}.", "done", "audit", {"events": events})
        discovery = re.match(r"^(?:discover|list|show registered participants|find participant)\b.*\b(bap|bpp|participant)?", text)
        if discovery:
            role_match = re.search(r"\b(bap|bpp)\b", text)
            role = role_match.group(1).upper() if role_match else None
            network = _network_intent(session.get("arguments") or {}).get("network", {}).get("name", "local-fabric")
            members = [item for item in self.agent.store.list_json(f"role:{network}:") if item.get("status") != "REMOVED" and (not role or item.get("type") == role)]
            if not members:
                label = role + "s" if role else "participants"
                return ChatResponse(sid, f"No {label} are registered in Fabric {network}. Registry L3 lookup is not yet configured; this is scoped desired/registration evidence only.", "done", "discover", {"fabric": network, "role": role, "participants": []})
            lines = [f"{item['subscriber_id']}  {item['type']}  registry: registered  runtime: not verified  verification: registration evidence" for item in members]
            return ChatResponse(sid, f"Participants registered in {network}:\n" + "\n".join(lines), "done", "discover", {"fabric": network, "role": role, "participants": members})
        if (text == "status" or text == "check drift" or text.startswith("why is ") or text.startswith("why can't")) and session.get("intent"):
            intent = _network_intent(session.get("arguments") or {})
            observed = self.agent.observe(intent)
            desired = self.agent.fabric_spec(intent).as_dict()
            from .skills import _changed_paths
            drift = _changed_paths(desired, observed)
            if text.startswith("why"):
                return ChatResponse(sid, "Diagnosis complete. No changes were made. " + (observed.get("reason") or "Runtime evidence was inspected."), "done", "diagnose", {"observed": observed, "drift": drift})
            return ChatResponse(sid, f"Fabric: {desired['fabric']['name']}\nRuntime verified: {observed.get('runtime_available', False)}\nContainers: {len(observed.get('containers', []))}\nObserved drift: {'none' if not drift else ', '.join(drift)}", "done", "status" if text == "status" else "check_drift", {"observed": observed, "drift": drift})
        return None

    def _load_session(self, session_id: str) -> dict[str, Any]:
        return self.agent.store.get_json(_session_key(session_id), {"session_id": session_id, "approvals": {}, "arguments": {}})

    def _save_session(self, session_id: str, session: dict[str, Any]) -> None:
        self.agent.store.put_json(_session_key(session_id), session)


def intent_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["intent", "arguments"],
        "properties": {
            "intent": {"enum": SUPPORTED_INTENTS},
            "arguments": {
                "type": "object",
                "properties": {
                    "network": {"type": "object"},
                    "cloud": {"type": "object"},
                    "deployment": {"type": "object"},
                    "subscriber_id": {"type": "string"},
                    "role": {"type": "string"},
                    "unique_key_id": {"type": "string"},
                    "public_url": {"type": "string"},
                    "key_ref": {"type": "string"},
                    "catalog_id": {"type": "string"},
                    "descriptor": {"type": "object"},
                    "catalog_entry": {"type": "object"},
                    "previous_intent": {"type": "object"},
                    "desired_intent": {"type": "object"},
                    "release_id": {"type": "string"},
                },
            },
        },
    }


def _normalize_parsed(parsed: dict[str, Any], message: str) -> dict[str, Any]:
    text_intent = _intent_from_text(message)
    intent = parsed.get("intent")
    if text_intent != "onboard_participant":
        intent = text_intent
    elif intent not in SUPPORTED_INTENTS:
        intent = text_intent
    arguments = parsed.get("arguments") or {}
    if not isinstance(arguments, dict):
        arguments = {}
    arguments = _deep_merge(arguments, _extract_from_text(message))
    return {"intent": intent, "arguments": arguments}


def _has_explicit_intent(message: str) -> bool:
    if re.match(r"^\s*[A-Za-z_][A-Za-z0-9_ -]*\s*:", message):
        return False
    return _intent_from_text(message) != "onboard_participant" or bool(re.search(r"\b(onboard|add|register)\b", message, re.IGNORECASE))


def _session_snapshot(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session.get("session_id"),
        "intent": session.get("intent"),
        "stage": session.get("stage"),
        "missing": session.get("missing"),
        "arguments": session.get("arguments") or {},
        "approvals": session.get("approvals") or {},
        "last_response": session.get("last_response"),
    }


def _intent_from_text(message: str) -> str:
    text = message.lower()
    if "rollback" in text:
        return "rollback"
    if "offboard" in text or "remove participant" in text:
        return "offboard_participant"
    if "update catalog" in text:
        return "update_catalog"
    if "retire catalog" in text:
        return "retire_catalog"
    if "publish catalog" in text or "add this catalog" in text or "add catalog" in text or "catalog" in text:
        return "publish_catalog"
    if "upgrade" in text or "scale" in text:
        return "upgrade"
    if "join network" in text or re.search(r"\bjoin\b.*\b(as|network)\b", text):
        return "join_network"
    if "bootstrap" in text or "create network" in text or "new network" in text or re.search(r"\b(create|build|make|want)\b.*\b(nfh|beckn|fabric|network)\b", text):
        return "bootstrap"
    if "status" in text:
        return "status"
    return "onboard_participant"


def _multiple_mutating_intents(message: str) -> list[str]:
    text = _normalize_operator_text(message).lower()
    found: list[str] = []
    patterns = {
        "bootstrap Fabric": r"\b(create|bootstrap|build)\b.*\b(network|fabric|beckn|nfh)\b",
        "join participant": r"\b(join|onboard|register|add)\b.*\b(bap|bpp|participant)\b",
        "remove participant": r"\b(offboard|remove|deregister)\b.*\b(bap|bpp|participant|local)\b",
        "publish catalog": r"\b(publish|update|retire)\b.*\bcatalog\b",
        "upgrade": r"\bupgrade\b",
    }
    for label, pattern in patterns.items():
        if re.search(pattern, text): found.append(label)
    return found


def _is_new_mutating_intent(message: str, intent: str) -> bool:
    text = _normalize_operator_text(message).lower()
    return intent == "bootstrap" and bool(re.search(r"\b(create|bootstrap|build|make)\b", text)) or intent in {"offboard_participant", "upgrade"}


def _is_generic_fabric_request(message: str) -> bool:
    text = _normalize_operator_text(message).lower().strip().rstrip(".?!")
    return bool(re.fullmatch(r"(?:create|build|make) (?:a |an )?(?:nfh|beckn) fabric", text))


def _is_help_question(message: str) -> bool:
    text = message.lower().strip()
    return text in {"help", "what can you do", "what all can you do", "what do you do"} or bool(
        re.search(r"\b(what|which)\b.*\b(can you do|commands|actions|capabilities)\b", text)
    )


def _help_text() -> str:
    return (
        "I can bootstrap local Docker or GCP/Terraform networks, join/deregister participants, manage catalogs, upgrade, rollback, and show status. "
        "Try: `create local network network.name: local-fabric network.domain: retail:1.1.0`, "
        "`bootstrap GCP network network.name: prod-fabric network.domain: retail:1.1.0 cloud.project_id: my-project cloud.state_backend: tfstate cloud.credential_ref: gcp-adc`, "
        "`join bap3.local as BAP key id bap-key callback http://bap3.local key ref secret://bap3/key`, "
        "`remove participant bap3.local as BAP`, or `status`."
    )


def _extract_from_text(message: str) -> dict[str, Any]:
    message = _normalize_operator_text(message)
    if message.strip().lower() in {"local", "want this local"}:
        return {"cloud": {"provider": "local", "topology": "local"}}
    extracted: dict[str, Any] = {}
    fields = _key_value_fields(message)
    subscriber_id = fields.get("subscriber_id") or fields.get("subscriber")
    if subscriber_id:
        extracted["subscriber_id"] = subscriber_id
    for sid in re.finditer(r"\b([a-z0-9.-]+\.[a-z]{2,})\b", message, re.IGNORECASE):
        value = sid.group(1)
        if value.lower() not in {"network.name", "network.domain", "cloud.project_id"} and "subscriber_id" not in extracted:
            extracted["subscriber_id"] = value
            break
    role_text = re.sub(r"https?://\S+", " ", message)
    role = re.search(r"\b(BAAP|BAP|BPP|BG|gateway)\b", role_text, re.IGNORECASE)
    if role:
        value = role.group(1).upper()
        extracted["role"] = "BAP" if value == "BAAP" else "BG" if value == "GATEWAY" else value
    participant_type = (fields.get("type") or fields.get("role") or "").lower()
    if "role" not in extracted and participant_type in {"provider", "bpp"}:
        extracted["role"] = "BPP"
    elif "role" not in extracted and participant_type in {"buyer", "bap", "baap", "agent"}:
        extracted["role"] = "BAP"
    elif "role" not in extracted and participant_type in {"gateway", "bg"}:
        extracted["role"] = "BG"
    env = re.search(r"\b(dev|staging|production)\b", message, re.IGNORECASE)
    if env:
        extracted.setdefault("network", {})["environment"] = env.group(1).lower()
    elif fields.get("network.environment") or fields.get("environment"):
        candidate = fields.get("network.environment") or fields.get("environment")
        if candidate in {"dev", "staging", "production"}:
            extracted.setdefault("network", {})["environment"] = candidate
    network_id = fields.get("network_id")
    if network_id:
        network_env = re.search(r"/(dev|staging|production)\b", network_id, re.IGNORECASE)
        if network_env:
            extracted.setdefault("network", {})["environment"] = network_env.group(1).lower()
    url = re.search(r"https?://\S+", message)
    if fields.get("url"):
        extracted["public_url"] = fields["url"]
    elif fields.get("callback"):
        extracted["public_url"] = fields["callback"]
    elif url:
        extracted["public_url"] = url.group(0).rstrip(".,")
    key_id = re.search(r"(?:key id|key_id|unique key id)\s+([A-Za-z0-9._-]+)", message, re.IGNORECASE)
    if key_id:
        extracted["unique_key_id"] = key_id.group(1)
    elif fields.get("unique_key_id"):
        extracted["unique_key_id"] = fields["unique_key_id"]
    elif fields.get("signing_public_key"):
        extracted["unique_key_id"] = "signing-key-1"
    key_ref = re.search(r"(?:key ref|key_ref|secret)\s+([A-Za-z0-9:/.@_-]+)", message, re.IGNORECASE)
    if key_ref:
        extracted["key_ref"] = key_ref.group(1)
    elif fields.get("key_ref"):
        extracted["key_ref"] = fields["key_ref"]
    elif fields.get("signing_public_key"):
        extracted["key_ref"] = f"inline-public-key:{fields['signing_public_key']}"
    catalog_id = fields.get("catalog_id")
    catalog_match = re.search(r"(?:catalog id|catalog_id)\s+([A-Za-z0-9._-]+)", message, re.IGNORECASE)
    if catalog_match:
        catalog_id = catalog_match.group(1)
    if catalog_id:
        extracted["catalog_id"] = catalog_id
    descriptor_name = fields.get("descriptor.name") or fields.get("name")
    descriptor_match = re.search(r"(?:descriptor name|catalog name|name)\s+([A-Za-z0-9][A-Za-z0-9 ._-]*)", message, re.IGNORECASE)
    if descriptor_match:
        descriptor_name = descriptor_match.group(1).strip()
    if descriptor_name:
        extracted.setdefault("descriptor", {})["name"] = descriptor_name
    # A natural one-line join often continues after network.name with
    # `key id`, `callback`, and `key ref`, none of which use a colon.  Bound
    # the Fabric name before considering generic key/value extraction so the
    # trailing participant fields cannot become part of the network name.
    explicit_network_name = re.search(r"\bnetwork\.name\s*:?-?\s*([a-z0-9][a-z0-9-]{1,62})\b", message, re.IGNORECASE)
    network_name = (explicit_network_name.group(1) if explicit_network_name else None) or fields.get("network.name") or _inline_value(message, "network.name") or fields.get("name")
    if not network_name:
        named = re.search(r"\b(?:call it|named?|name it)\s+([a-z0-9][a-z0-9-]{1,62})", message, re.IGNORECASE)
        if named:
            network_name = named.group(1)
    if not network_name:
        named = re.search(r"\b(?:network\s+)?name\s+(?:is\s+)?([a-z0-9][a-z0-9-]{1,62})\b", message, re.IGNORECASE)
        if named:
            network_name = named.group(1)
    if not network_name:
        named = re.search(r"\bnetwork\.name\s*:?-?\s*([a-z0-9][a-z0-9-]{1,62})\b", message, re.IGNORECASE)
        if named:
            network_name = named.group(1)
    if network_name and ("bootstrap" in message.lower() or "new network" in message.lower() or "network.name" in fields or "network.name" in message.lower() or re.search(r"\b(?:call|name)\s+it\b|\bnetwork\s+name\b", message, re.IGNORECASE)):
        extracted.setdefault("network", {})["name"] = network_name.lstrip("- ")
    if not (extracted.get("network") or {}).get("name"):
        plain_name = re.match(r"^\s*name\s*:?-?\s*([a-z0-9][a-z0-9-]{1,62})\s*$", message, re.IGNORECASE)
        if plain_name:
            extracted.setdefault("network", {})["name"] = plain_name.group(1)
    network_domain = fields.get("network.domain") or _inline_value(message, "network.domain") or fields.get("domain")
    if network_domain:
        protocol = re.match(r"^([a-z][a-z0-9-]*:\d+(?:\.\d+){1,3})\b", network_domain, re.IGNORECASE)
        if protocol:
            network_domain = protocol.group(1)
    if not network_domain:
        domain_match = re.search(r"\b([a-z][a-z0-9-]*:\d+(?:\.\d+){1,3})\b", message, re.IGNORECASE)
        if domain_match:
            network_domain = domain_match.group(1)
    if network_domain:
        if _is_beckn_domain(network_domain):
            extracted.setdefault("network", {})["domain"] = network_domain
        elif _is_hostname(network_domain):
            extracted.setdefault("network", {})["hostname"] = network_domain
    hostname = re.search(r"\b(?:https?://)?(www\.[a-z0-9.-]+\.[a-z]{2,})\b", message, re.IGNORECASE)
    if hostname:
        extracted.setdefault("network", {})["hostname"] = hostname.group(1).lower()
    project_id = fields.get("cloud.project_id") or _inline_value(message, "cloud.project_id") or fields.get("project_id") or _inline_value(message, "project_id")
    if project_id:
        extracted.setdefault("cloud", {})["project_id"] = project_id
    state_backend = fields.get("cloud.state_backend") or _inline_value(message, "cloud.state_backend") or fields.get("state_backend") or _inline_value(message, "state_backend")
    if state_backend:
        extracted.setdefault("cloud", {})["state_backend"] = state_backend
    credential_ref = fields.get("cloud.credential_ref") or _inline_value(message, "cloud.credential_ref") or fields.get("credential_ref") or _inline_value(message, "credential_ref")
    if credential_ref:
        extracted.setdefault("cloud", {})["credential_ref"] = credential_ref
    if _mentions_local_runtime(message):
        extracted.setdefault("cloud", {})["provider"] = "local"
        extracted.setdefault("cloud", {})["topology"] = "local"
    elif re.search(r"\b(gcp|gke)\b", message, re.IGNORECASE):
        extracted.setdefault("cloud", {})["provider"] = "gcp"
        extracted.setdefault("cloud", {})["topology"] = "gke"
    if re.search(r"\bregistry\b", message, re.IGNORECASE):
        extracted["registry"] = {"enabled": True}
    composition = _participant_composition(message)
    if composition:
        extracted["requested_composition"] = composition
        # A count describes a desired slot, not a fully identified participant.
        extracted.pop("role", None)
    return extracted


def _key_value_fields(message: str) -> dict[str, str]:
    message = _normalize_operator_text(message)
    fields: dict[str, str] = {}
    known = (
        "subscriber_id",
        "subscriber",
        "url",
        "callback",
        "type",
        "role",
        "signing_public_key",
        "unique_key_id",
        "key_ref",
        "network_id",
        "status",
        "catalog_id",
        "descriptor.name",
        "name",
        "network.name",
        "network.domain",
        "network.environment",
        "environment",
        "cloud.project_id",
        "cloud.state_backend",
        "cloud.credential_ref",
        "project_id",
        "state_backend",
        "credential_ref",
    )
    known_pattern = "|".join(re.escape(key) for key in sorted(known, key=len, reverse=True))
    normalized = re.sub(rf"(?<!^)(?<![\n\s])({known_pattern})\s*:", r"\n\1:", message, flags=re.IGNORECASE)
    for line in normalized.splitlines():
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_. -]*)\s*:\s*(.+?)\s*$", line)
        if match:
            key = match.group(1).strip().lower().replace(" ", "_")
            value = match.group(2).strip().lstrip("- ").strip('"').strip("'")
            fields[key] = value
    inline = re.finditer(
        rf"\b({known_pattern})\s*:\s*(.+?)(?=(?:\s*{known_pattern})\s*:|$)",
        normalized,
        re.IGNORECASE,
    )
    for match in inline:
        key = match.group(1).strip().lower().replace(" ", "_")
        value = match.group(2).strip().lstrip("- ").strip('"').strip("'")
        fields[key] = value
    return fields


def _inline_value(message: str, key: str) -> str | None:
    escaped = re.escape(key)
    known = r"network\.name|network\.domain|cloud\.project_id|cloud\.state_backend|cloud\.credential_ref|project_id|state_backend|credential_ref"
    match = re.search(rf"\b{escaped}\s*:\s*(.+?)(?=\s+(?:{known})\s*:|$)", message, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip().strip('"').strip("'")


def _missing_fields(intent: str, args: dict[str, Any]) -> list[str]:
    required = {
        "bootstrap": _bootstrap_required(args),
        "onboard_participant": ["subscriber_id", "role", "unique_key_id", "public_url", "key_ref"],
        "join_network": ["subscriber_id", "role", "unique_key_id", "public_url", "key_ref"],
        "offboard_participant": ["subscriber_id", "role"],
        "publish_catalog": ["catalog_id", "subscriber_id", "descriptor"],
        "update_catalog": ["catalog_id", "subscriber_id", "descriptor"],
        "retire_catalog": ["catalog_id"],
        "upgrade": ["previous_intent", "desired_intent"],
        "rollback": ["release_id"],
        "status": [],
    }.get(intent, [])
    return [field for field in required if _get_path(args, field) in (None, "", {})]


def _clarifying_question(intent: str, missing: list[str]) -> str:
    labels = ", ".join(missing)
    if intent == "bootstrap":
        return _bootstrap_clarification(labels)
    if intent in {"onboard_participant", "join_network"}:
        return f"I can onboard the participant, but I need: {labels}. For example: key id key-1, callback https://seller.example/bpp, key ref secret://seller/key."
    return f"I need these fields before I can run {intent}: {labels}."


def _bootstrap_clarification(labels: str) -> str:
    # Session arguments are not available here; the caller's regular message
    # remains concise while requirements questions provide the full topology view.
    return f"I can prepare the requested Fabric. I still need: {labels}. No infrastructure has been changed."


def _network_intent(args: dict[str, Any]) -> dict[str, Any]:
    network = {
        "name": "chat-network",
        "environment": "dev",
        "domain": "retail:1.1.0",
        "country": "IND",
        **(args.get("network") or {}),
    }
    cloud = {
        "provider": "gcp",
        "topology": "gke",
        "project_id": "sandbox",
        "state_backend": "tfstate",
        "credential_ref": "gcp-adc",
        **(args.get("cloud") or {}),
    }
    deployment = {
        "node_count": 2,
        "machine_type": "e2-standard-4",
        "adapter_version": "1.0.0",
        **(args.get("deployment") or {}),
    }
    return {"network": network, "cloud": cloud, "deployment": deployment,
            "registry": dict(args.get("registry") or {"enabled": True}),
            "gateway": dict(args.get("gateway") or {"enabled": True}),
            "requested_composition": dict(args.get("requested_composition") or {}),
            "source": dict(args.get("source") or {})}


def _participant(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "subscriber_id": args["subscriber_id"],
        "role": args["role"],
        "unique_key_id": args["unique_key_id"],
        "public_url": args["public_url"],
        "key_ref": args["key_ref"],
    }


def _catalog_entry(args: dict[str, Any]) -> dict[str, Any]:
    entry = args.get("catalog_entry") or {}
    return {
        "catalog_id": args.get("catalog_id") or entry.get("catalog_id"),
        "subscriber_id": args.get("subscriber_id") or entry.get("subscriber_id"),
        "descriptor": args.get("descriptor") or entry.get("descriptor"),
        **entry,
    }


def _state_response(session_id: str, intent: str, state: Any) -> ChatResponse:
    text = state.messages[-1] if state.messages else f"{intent} reached {state.stage.value}."
    return ChatResponse(session_id, text, state.stage.value, intent, {"messages": state.messages, "state": state.__dict__})


def _status_text(agent: InstantiationAgent, session: dict[str, Any]) -> str:
    base = f"Session stage: {session.get('stage', 'collecting')}. Pending intent: {session.get('intent') or 'none'}."
    intent = _network_intent(session.get("arguments") or {})
    if (intent.get("cloud") or {}).get("topology") != "local":
        return base
    network = intent.get("network") or {}
    compose_dir = agent.store.get_json(f"local_workspace:{network.get('name', 'local-fabric')}")
    if not compose_dir:
        return base + " No local compose workspace found."
    compose = Path(compose_dir) / "docker-compose.yml"
    if not compose.exists():
        return base + f" Local compose file is missing at {compose}."
    result = subprocess.run(["docker", "compose", "-f", str(compose), "ps", "--format", "json"], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return base + " Local docker status unavailable: " + (result.stderr or result.stdout).strip()
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    containers: list[dict[str, Any]] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            return base + " Local docker status was returned in an unreadable format."
        if isinstance(parsed, dict):
            containers.append(parsed)
    if not containers:
        return base + " Local containers: none."
    summary = []
    for item in containers:
        service = item.get("Service") or item.get("Name") or "unknown"
        state = item.get("State") or item.get("Status") or "unknown"
        image = item.get("Image") or "unknown image"
        ports = item.get("Ports") or "no published ports"
        summary.append(f"{service}: {state} ({image}; {ports})")
    return base + " Local containers: " + "; ".join(summary)


def _bootstrap_required(args: dict[str, Any]) -> list[str]:
    topology = ((args.get("cloud") or {}).get("topology"))
    if topology == "local":
        return ["network.name", "network.environment", "network.domain"]
    return ["network.name", "network.environment", "network.domain", "cloud.project_id", "cloud.state_backend", "cloud.credential_ref"]


def _classify_turn(message: str) -> TurnKind:
    """Classify speech act before intent parsing; no authorization decisions live here."""
    text = " ".join(_normalize_operator_text(message).lower().split())
    if text in {"hi", "hello", "hey"}: return TurnKind.GREETING
    if _is_help_question(text): return TurnKind.HELP
    if text in APPROVAL_PHRASES: return TurnKind.APPROVAL
    if text in {"cancel", "cancle", "start over"}: return TurnKind.CANCEL
    mentions_build = bool(re.search(r"\b(create|build|make|deploy|bootstrap|network|fabric|beckn|nfh)\b", text))
    asks_requirements = bool(re.search(r"\b(what|which|tell me|info|information|values?|fields?|requirements?)\b.*\b(need|needed|require|before|provide|from me)\b", text))
    asks_requirements |= bool(re.search(r"\bwhat\s+(?:all\s+)?(?:values?|info(?:rmation)?|fields?)\b", text))
    if mentions_build and asks_requirements:
        return TurnKind.REQUIREMENTS_QUESTION
    if text.endswith("?") and mentions_build and re.match(r"^(can|could|would)\b", text):
        return TurnKind.REQUIREMENTS_QUESTION
    if re.match(r"^(why|what will|can i|how does)\b", text) or re.search(r"\bi (?:do not|don't) (?:know|have)\b.*\b(domain|environment)\b", text):
        return TurnKind.EXPLANATION_QUESTION
    return TurnKind.EXECUTE_INTENT


def _mentions_local_runtime(message: str) -> bool:
    text = message.lower()
    direct = r"(?<![\w-])local(?:ly)?(?![\w-])"
    return bool(re.search(direct, text) and re.search(r"\b(network|beckn|nfh|fabric|registry|docker|runtime|machine|gcp)\b", text)) or bool(re.search(r"\bon my machine\b", text))


def _participant_composition(message: str) -> dict[str, int]:
    text = message.lower()
    values: dict[str, int] = {}
    number = {"one": 1, "two": 2, "three": 3, "a": 1, "an": 1}
    def count_for(pattern: str) -> int | None:
        match = re.search(rf"\b(\d+|one|two|three|a|an)\s*{pattern}\b", text)
        if not match: return None
        raw = match.group(1)
        return int(raw) if raw.isdigit() else number[raw]
    bap = count_for(r"(?:bap|baap|buyer\s+app(?:lication)?s?)")
    bpp = count_for(r"(?:bpps?|providers?|seller\s+apps?)")
    if bap is not None: values["bap_count"] = bap
    if bpp is not None: values["bpp_count"] = bpp
    return values


def _requirements_response(intent: dict[str, Any]) -> str:
    local = (intent.get("cloud") or {}).get("topology") == "local"
    network_fields = _bootstrap_required(intent)
    labels = ", ".join(field.replace("network.", "") for field in network_fields)
    composition = intent.get("requested_composition") or {}
    parts: list[str] = ["I can define that as a " + ("local NFH Fabric" if local else "NFH Fabric") + "."]
    understood = []
    if local: understood.append("runtime: local")
    if (intent.get("registry") or {}).get("enabled"): understood.append("registry: enabled")
    if composition:
        understood.append("participants: " + ", ".join(f"{value} {key.replace('_count', '').upper()}" for key, value in sorted(composition.items())))
    if understood: parts.append("I understood " + "; ".join(understood) + ".")
    parts.append("To define the network I need: " + labels + ".")
    if composition:
        parts.append("Before each requested participant joins, I will separately need its subscriber ID, callback/public URL, key ID, and key reference.")
    parts.append("No infrastructure changes have been made.")
    return "\n\n".join(parts)


def _explanation_response(message: str, session: dict[str, Any]) -> str:
    text = message.lower()
    if "domain" in text:
        answer = "The Beckn domain scopes discovery and routing compatibility for the Fabric."
    elif "registry" in text:
        answer = "The registry is the discovery authority that records active participant roles."
    elif "bpp" in text or "participant" in text:
        answer = "Yes. BPPs can be joined later through their own governed participant plan."
    else:
        answer = "I need the remaining values to render a deterministic, reproducible Fabric configuration and plan."
    return answer + " Pending operation remains unchanged."


def _normalize_operator_text(message: str) -> str:
    """Conservative lexical repair before semantic extraction; never rewrites IDs/secrets."""
    fixed = message
    replacements = {"alocal": "a local", "regiastry": "registry", "enviroment": "environment", "domin": "domain"}
    for wrong, right in replacements.items():
        fixed = re.sub(rf"\b{wrong}\b", right, fixed, flags=re.IGNORECASE)
    return re.sub(r"\b(\d+)(bap|baap|bpp)\b", r"\1 \2", fixed, flags=re.IGNORECASE)


def _is_beckn_domain(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9-]*:\d+(?:\.\d+){1,3}", value.lower()))


def _is_hostname(value: str) -> bool:
    return bool(re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", value.lower()))


def _session_key(session_id: str) -> str:
    return f"chat_session:{session_id}"


def _get_path(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _deep_merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        elif value not in (None, "", {}):
            merged[key] = value
    return merged
