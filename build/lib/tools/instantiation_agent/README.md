# NFH Fabric Infrastructure Instantiation Agent

The NFH Fabric Infrastructure Instantiation Agent is a governed control plane for creating and operating an NFH/Beckn Fabric. It gives an operator a conversational interface for infrastructure and participant workflows while keeping all state-changing actions deterministic, policy-checked, approval-gated, and auditable.

It is designed for local development fabrics as well as cloud-backed infrastructure. The project can render and run a local Docker Compose runtime, or generate infrastructure-as-code artifacts for a configured cloud provider. Provider-specific integrations are implementation details: the agent’s governance model does not depend on a particular LLM or cloud vendor.

## What it manages

- Fabric bootstrap, including desired-state planning, policy evaluation, approvals, and release evidence.
- Local Fabric execution with Docker Compose, runtime-image preflight, configuration validation, and service health checks.
- Cloud infrastructure artifact generation through a provider-specific infrastructure-as-code path.
- Participant onboarding and offboarding, including DIDs, capability credentials, and registry roles.
- Catalog publication, updates, and retirement without changing the underlying infrastructure.
- Bounded incremental upgrades and drift detection/reconciliation.

## How it works

Natural-language input is a convenience layer, not an execution authority. An optional LLM provider may extract candidate fields or phrase a response, but deterministic code selects supported operations and runs fixed skill chains. It cannot execute shell, container, or infrastructure commands; bypass policy; or reuse an approval after the plan changes.

Every mutating workflow follows the same control-plane pattern:

```text
operator request
  -> normalized desired state and immutable plan
  -> policy and risk checks
  -> plan-bound approval
  -> deterministic skill execution
  -> verification, audit evidence, and release manifest
```

Safe chat controls such as `show plan`, `show diff`, `status`, `check drift`, `show audit`, and `cancel` do not mutate Fabric state.

## Quick start

Run these commands from the repository root:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

nfh-instantiation-agent chat
```

Use `exit`, `quit`, or Ctrl-D to leave the terminal chat. The agent can also be used through its public Python API; the runnable examples are in the [runbook](../../../../guides/RUNBOOK.md).

## Governance and safety

- **Plan-bound approvals:** approvals are recorded against an immutable plan ID and hash. Changing desired state invalidates the prior approval.
- **Policy enforcement:** each skill is evaluated against the policy bundle, environment, actor, and risk level.
- **Source and artifact controls:** local execution validates rendered Compose configuration and checks required runtime images before starting services.
- **Secret boundaries:** secret references are used in state and configuration; credential values and raw provider responses must not be sent to an LLM context.
- **Truthful verification:** a release is recorded only after the required runtime checks succeed. The project distinguishes running containers, reachable services, and a complete protocol transaction.
- **Auditability:** plans, approvals, desired state, skill evidence, and release manifests are stored in the local state and audit store.

Bootstrap has two explicit gates:

```text
approve bootstrap config
spend money now
```

Production participant and catalog operations can require additional exact, skill-specific approvals. See the [User Guide](../../../../guides/USER_GUIDE.md) for the complete lifecycle and approval model.

## Project map

| Resource | Purpose |
| --- | --- |
| [Documentation overview](../../../../guides/README.md) | Installation, high-level project overview, and command reference. |
| [Governance and Guardrails](../../../../guides/GOVERNANCE_AND_GUARDRAILS.md) | Control-by-control governance model with direct references to the policy and implementation files. |
| [Architecture](../../../../guides/ARCHITECTURE.md) | System design, trust boundaries, fixed skill chains, runtime topology, and verification levels. |
| [User Guide](../../../../guides/USER_GUIDE.md) | Lifecycle behavior, approvals, API surface, state, policy, and source-file guide. |
| [Runbook](../../../../guides/RUNBOOK.md) | End-to-end setup and operational examples. |
| [Chat Journey](../../../../guides/CHAT_JOURNEY.md) | Step-by-step examples for the terminal chat interface. |
| [Local E2E Test Guide](../../../../guides/LOCAL_E2E_TEST_GUIDE.md) | Local Fabric bootstrap, participant setup, and protocol-testing guidance. |
| [Design article](../../../../tools/instantiation_agent/NFH_FABRIC_BLOG.md) | Background, design rationale, and known verification boundaries. |

## Rollback

A successful bootstrap or upgrade writes a signed release manifest. To roll back a recorded release:

```bash
nfh-instantiation-agent rollback \
  --state-db .instantiation-agent/state.sqlite3 \
  --release-id <release-id>
```

## Testing

Run the regular test suite from the repository root:

```bash
pytest
```

Some integration tests require an explicitly configured LLM provider and are skipped unless their documented environment variables are present. See the [Runbook](../../../../guides/RUNBOOK.md) for setup and test details.

## Current scope

The local runtime release gate verifies container stability and required network listeners. A fully correlated Beckn transaction is tracked separately as protocol-level evidence; the architecture guide documents the current boundary and planned reliability work. The agent therefore reports the level of evidence it has rather than treating a started container as proof of an end-to-end Fabric transaction.
