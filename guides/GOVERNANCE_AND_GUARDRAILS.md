# Governance and Guardrails

This document describes the controls that make the NFH Fabric Infrastructure Instantiation Agent a governed control plane rather than an unrestricted conversational deployment tool. It maps each control to the code and policy files that enforce it.

## Governance model

The agent separates conversation, decision-making, execution, and evidence:

```text
operator input
  -> chat/session handling
  -> deterministic intent validation and immutable plan
  -> policy decision and required approval
  -> fixed skill chain
  -> validation, execution, verification, and persisted evidence
```

An LLM provider is optional assistance for parsing natural language or phrasing a response. It does not select an operation, construct an executable skill chain, or directly invoke Docker or infrastructure tooling. Those decisions remain in the deterministic service, agent, policy, and skill code.

## Guardrails and implementation references

| Guardrail | Behaviour | Enforced by |
| --- | --- | --- |
| Deterministic operation routing | Supported intents, field handling, and lifecycle dispatch are controlled in code; free-form text does not become a shell or infrastructure command. | [`service.py`](../tools/instantiation_agent/service.py), [`agent.py`](../tools/instantiation_agent/agent.py) |
| LLM containment | The LLM interface only exposes intent parsing and response phrasing. It checks for credential-like input before provider calls and redacts sensitive values in its debug log. | [`llm.py`](../tools/instantiation_agent/llm.py) |
| Immutable plan and plan-bound approval | A plan contains desired and observed state hashes, diff, risk, policy result, and required approvals. The chat service invalidates pending approvals when the plan hash changes and records approval evidence with the plan ID/hash. | [`agent.py`](../tools/instantiation_agent/agent.py), [`service.py`](../tools/instantiation_agent/service.py), [`store.py`](../tools/instantiation_agent/store.py) |
| Exact approvals | Bootstrap requires `approve bootstrap config` followed by `spend money now`. Production skills require their exact `approve <skill>` phrase. | [`skills.py`](../tools/instantiation_agent/skills.py), [`service.py`](../tools/instantiation_agent/service.py) |
| Policy allow-list and risk decisions | The policy bundle determines which skills are allowed, which production skills need approval, permitted auto-heal actions, and approved source repositories. The policy decision distinguishes allow, approval-required, and privileged-approval-required work. | [`policy.py`](../tools/instantiation_agent/policy.py), [`default_policy.json`](../tools/instantiation_agent/policies/default_policy.json) |
| Fixed skill chains | Each lifecycle invokes a code-defined sequence of skills. A model cannot add, remove, or reorder skills. | [`agent.py`](../tools/instantiation_agent/agent.py), [`skills.py`](../tools/instantiation_agent/skills.py) |
| Read-only and cancellation controls | Plan, diff, audit, status, drift, and explanatory chat commands are handled without executing a mutation. `cancel` clears the pending session state. There is deliberately no executable chat `run` command. | [`service.py`](../tools/instantiation_agent/service.py) |
| Participant input and Fabric isolation | Participant identities are validated as DNS-like identifiers and role records are scoped to the Fabric, reducing unsafe input and cross-Fabric state mixing. | [`skills.py`](../tools/instantiation_agent/skills.py), [`local_fabric.py`](../tools/instantiation_agent/local_fabric.py) |
| Approved-source provenance | The local runtime path checks that the source origin is policy-approved and resolves/pins a commit before an adapter build. An unavailable or unapproved source blocks the workflow. | [`skills.py`](../tools/instantiation_agent/skills.py), [`policy.py`](../tools/instantiation_agent/policy.py) |
| Generated-runtime validation | Before local startup, the agent validates the generated Compose configuration, checks Docker availability, resolves required images, and blocks before mutation if preflight fails. | [`skills.py`](../tools/instantiation_agent/skills.py), [`local_fabric.py`](../tools/instantiation_agent/local_fabric.py) |
| Evidence-based release | Local execution verifies expected services and health. The release-manifest skill withholds a release if required verification evidence is absent or failed. | [`skills.py`](../tools/instantiation_agent/skills.py), [`store.py`](../tools/instantiation_agent/store.py) |
| Audit trail and rollback reference | Plans, approvals, audit events, and release manifests are persisted in SQLite. Rollback loads a specific recorded release manifest rather than acting on an unspecified deployment. | [`store.py`](../tools/instantiation_agent/store.py), [`agent.py`](../tools/instantiation_agent/agent.py), [`cli.py`](../tools/instantiation_agent/cli.py) |

## Approval rules

The bootstrap flow uses two separate approvals:

```text
approve bootstrap config
spend money now
```

The split lets an operator inspect the normalized plan before allowing an apply step. An approval is only accepted for the current plan; changing relevant desired-state fields causes the chat service to require review of a newly generated plan.

In production, the policy can require an exact approval per mutating skill. The default policy lists participant, catalog, bootstrap, local-execution, upgrade, and drift-reconciliation skills that use this additional gate. Consult the current [`default_policy.json`](../tools/instantiation_agent/policies/default_policy.json) rather than relying on this document for the active policy configuration.

## Local execution controls

The local path is a real execution mode, so it has additional controls before `docker compose up -d`:

1. Render Fabric-specific Compose and registry assets.
2. Validate the Compose file with Docker’s configuration parser.
3. Confirm the Docker daemon is available.
4. Enumerate and preflight required runtime images.
5. Resolve and validate an approved, pinned source when a runtime build is required.
6. Start the approved Compose project only after the required policy and approvals pass.
7. Verify health and stable expected services before allowing a release manifest.

The implementation is in [`skills.py`](../tools/instantiation_agent/skills.py); generated topology and Fabric-scoped port/service naming live in [`local_fabric.py`](../tools/instantiation_agent/local_fabric.py).

## Evidence levels and current boundary

The project intentionally distinguishes:

- **L1 runtime evidence:** expected services are running and stable.
- **L2 service evidence:** required host listeners are reachable.
- **L3 protocol evidence:** a signed and correlated Beckn transaction completes across participants.

The local release gate currently requires L1 and L2 evidence. L3 is a separate protocol-level claim and must not be inferred solely from containers starting. See the [architecture guide](ARCHITECTURE.md) for the full trust boundary and the known L3 gap.

## Related documentation

- [Project overview](README.md)
- [Architecture](ARCHITECTURE.md)
- [User Guide](USER_GUIDE.md)
- [Runbook](RUNBOOK.md)
- [Chat Journey](CHAT_JOURNEY.md)
- [Local E2E Test Guide](LOCAL_E2E_TEST_GUIDE.md)
