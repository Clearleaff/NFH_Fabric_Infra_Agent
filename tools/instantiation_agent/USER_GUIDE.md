# Instantiation Agent User Guide

## SRE controls

Conversation state is not Fabric desired state. Each mutation is normalized, observed, diffed, planned, policy-checked, approved, executed, and verified. Use `show plan`, `show diff`, `why do you need approval`, `show current intent`, `status`, `check drift`, or `cancel` without corrupting a pending workflow. Approval evidence records plan ID/hash and policy version.

Natural questions such as “what do you need to create a local NFH network?” are read-only requirements requests, not bootstrap commands. The agent retains their subject (local runtime, registry and requested BAP/BPP counts) for the response without inventing participant identities or opening an approval flow. A later command begins normal desired-state collection.

## What This Agent Does

The Infrastructure Instantiation Agent manages the ongoing lifecycle of an NFH Fabric network. It is not just a bootstrap script. It can:

- Bootstrap base GCP infrastructure for a Fabric network, or start a local Docker Compose demo fabric.
- Onboard and offboard participants with DIDs, capability credentials, and registry roles.
- Publish, update, and retire catalog entries without touching infrastructure.
- Apply incremental fabric upgrades such as scaling, adapter version bumps, and policy bundle changes.
- Reconcile drift between desired state and observed state.

The agent is designed around deterministic skills. The LLM is allowed to understand user language and phrase results, but it is not trusted for money, infrastructure, credentials, policy decisions, or final state changes.

## Lifecycle Model

Bootstrap is the highest-risk path. For GCP topologies, it estimates cost, checks policy, requires config approval, requires apply approval, renders GCP Terraform, stores last-applied intent, writes audit events, and writes a signed release manifest.

Local bootstrap is additive and uses the same config/apply approval phrases. Its skill chain is `intent_diff_skill`, `credential_handling_skill`, `bootstrap_skill`, `local_execution_skill`, `release_manifest_skill`. It does not run `cost_estimation_skill`. After approval, it renders `docker-compose.yml`, `registry-seed.json`, and `README.md`, then starts the local registry and gateway with Docker Compose.

Participant lifecycle is lighter. In dev and staging, it can run without production approval. In production, each mutating skill requires an exact approval phrase. The normal sequence is DID provisioning, capability credential issuance, then registry role registration. Local join adds `local_execution_skill` after registration so the participant adapter container is added to the running local fabric.

Catalog lifecycle is business-layer only. Publish, update, and retire operations write catalog state through the Fabric client abstraction and must not render Terraform.

Upgrade is incremental only. The agent allows scale, machine type, adapter version, and policy bundle version changes. Region or topology changes are treated as non-incremental and routed to human review.

Drift reconcile compares observed state to desired state. Only policy-approved bounded actions can auto-heal; everything else goes to human review.

## Approval Phrases

Bootstrap uses two distinct gates:

- Config approval: `approve bootstrap config`
- Apply approval: `spend money now`

Upgrade apply uses:

- `approve incremental upgrade`

Production participant and catalog skills use exact per-skill phrases:

- `approve did_provision_skill`
- `approve capability_credential_skill`
- `approve role_registration_skill`
- `approve participant_offboard_skill`
- `approve catalog_publish_skill`
- `approve catalog_update_skill`
- `approve catalog_retire_skill`

Production local execution also uses:

- `approve local_execution_skill`

Ambiguous approval like `yes` does not advance guarded stages.

## File Guide

`README.md`

Short overview, test commands, LLM provider notes, and rollback command.

`USER_GUIDE.md`

This guide. Explains the concepts, lifecycle layers, approval gates, and each source file.

`RUNBOOK.md`

Hands-on run guide with a local user story and live Groq test commands.

`__init__.py`

Exports the public Python API: `InstantiationAgent`, `AgentError`, `Lifecycle`, and `Stage`.

`agent.py`

The public facade. This is the file most callers import. It exposes:

- `bootstrap`
- `onboard_participant`
- `join_network`
- `offboard_participant`
- `publish_catalog`
- `upgrade`
- `reconcile_drift`
- `rollback`

It wires the state store, policy bundle, local Fabric client, skill runner, session vault, workspace directory, actor DID, and budget cap.

`service.py`

Conversational entrypoint. `InstantiationChatService.chat(session_id, message)` takes raw user text, calls `GroqLLM.parse_intent()` against a schema covering all supported intents, persists session state, asks clarifying questions for missing structured fields, and routes deterministically to the matching `InstantiationAgent` method.

The LLM does not select skill chains. Bootstrap, onboarding, catalog, upgrade, and rollback sequencing remains hardcoded.

Exact approval phrases remain exact. If a session is waiting at `config_approval` or `apply_approval`, a paraphrase like `yes` does not get sent to the LLM for approval. The service replays the current gate until the literal phrase is typed.

`models.py`

Shared dataclasses and enums:

- `Lifecycle`
- `Stage`
- `AgentState`
- `SkillResult`
- `AuditEvent`
- `AgentError`

It also defines the mutating skill names used by the governance layer.

`skills.py`

Core lifecycle implementation. Each skill receives structured state and arguments, performs one job, appends a human-readable message, writes audit through the runner, and returns the next stage.

Important skills include:

- `intent_diff_skill`
- `cost_estimation_skill`
- `credential_handling_skill`
- `policy_check_skill`
- `did_provision_skill`
- `capability_credential_skill`
- `role_registration_skill`
- `catalog_publish_skill`
- `catalog_update_skill`
- `catalog_retire_skill`
- `bootstrap_skill`
- `upgrade_skill`
- `drift_reconcile_skill`
- `release_manifest_skill`

`store.py`

SQLite-backed local state store. It stores key/value state, audit events, and release manifests. It also provides manifest lookup and manifest counting for tests and rollback.

`policy.py`

Policy bundle loader and evaluator. It reads the JSON policy bundle if provided, or falls back to the built-in dev policy. It decides skill allow/deny, production approval requirements, and drift auto-heal permissions.

`policies/default_policy.json`

Reviewable policy-as-code stub. Dev and staging allow all skills. Production allows read/check skills directly and requires explicit approval for mutating operations.

`terraform.py`

GCP-only Terraform renderer. It supports GKE and VM topology, writes GCS remote state config, and emits `upgrade.diff.json` so reviewers can see changed intent paths.

`local_fabric.py`

Local Docker Compose renderer. It emits registry, gateway, participant adapter services, deterministic host ports, a bridge network, registry seed data, and a short README for approved local topology runs.

`llm.py`

Groq-only LLM client. It supports:

- strict JSON intent parsing
- natural language phrasing of deterministic skill results
- secret redaction in debug logs
- secret rejection before LLM calls
- fallback deterministic phrasing if the LLM drifts from source values

The default model is `llama-3.3-70b-versatile`.

`crypto.py`

Deterministic hashing, synthetic DID generation, canonical JSON serialization, and HMAC-based signing for audit events and release manifests.

`cli.py`

Small command-line entry point. It supports local terminal chat and rollback:

```bash
python3 -m tools.instantiation_agent chat
python3 -m tools.instantiation_agent.cli rollback --state-db .instantiation-agent/state.sqlite3 --release-id <release-id>
```

For chat prompts and test values, see `CHAT_JOURNEY.md`.

`tests/test_agent.py`

CI-safe unit tests for deterministic behavior:

- participant onboarding
- catalog isolation from Terraform
- budget-cap blocking
- bootstrap approval phrase rejection
- bootstrap idempotency no-op
- incremental upgrade allow/reject behavior
- production approval phrase enforcement

`tests/test_llm_safety.py`

CI-safe tests for LLM secret rejection and deterministic phrasing.

`tests/test_live_groq.py`

Live Groq tests. Skipped unless `GROQ_API_KEY` is set and `INSTANTIATION_AGENT_E2E_LIVE=true`.

`tests/test_service.py`

CI-safe tests for the conversational service. They verify clarification, persisted session arguments, deterministic participant execution, and exact approval phrase handling.

## State And Outputs

By default, local state goes under:

```text
.instantiation-agent/state.sqlite3
.instantiation-agent/work/
```

Bootstrap and upgrade render Terraform into a per-session workspace:

```text
.instantiation-agent/work/<session_id>/terraform/
```

Local bootstrap and join render Compose artifacts into:

```text
.instantiation-agent/work/<session_id>/local/
```

Release manifests are stored in SQLite and include:

- release id
- Terraform state hash
- participant snapshot
- policy bundle version
- approving identity
- signature

## Safety Guarantees Covered By Tests

- Ambiguous approval does not pass bootstrap config gate.
- Budget cap blocks apply even when human approval exists.
- Catalog publish does not render Terraform.
- Re-running unchanged bootstrap is a no-op.
- Production mutating participant skills require exact per-skill approval.
- LLM request payloads reject direct credential values.
- Live Groq phrasing preserves deterministic cost values.
