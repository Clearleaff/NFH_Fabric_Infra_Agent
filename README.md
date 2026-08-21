# NFH Fabric Infrastructure Instantiation Agent

The NFH Fabric Infrastructure Instantiation Agent is a governed control plane for creating and operating an NFH/Beckn Fabric. It provides a conversational interface for infrastructure and participant workflows while keeping every state-changing action deterministic, policy-checked, approval-gated, and auditable.

The agent supports local Docker Compose Fabrics and cloud-backed infrastructure artifact generation. Its governance model is provider-neutral: an optional LLM provider can help interpret an operator’s request, and a configured cloud provider can supply infrastructure, but neither replaces deterministic policy and execution controls.

## What it manages

- Fabric bootstrap, planning, approval, execution, and release evidence.
- Local Docker Compose runtime generation, preflight validation, and service health checks.
- Cloud infrastructure-as-code artifact generation through a configured provider path.
- Participant onboarding and offboarding, including DIDs, capability credentials, and registry roles.
- Catalog publication, updates, and retirement without changing infrastructure.
- Bounded incremental upgrades and drift detection/reconciliation.

## How it works

Natural language is an interface, not an execution authority. The agent follows a governed workflow:

```text
operator request
  -> normalized desired state and immutable plan
  -> policy and risk checks
  -> plan-bound approval
  -> deterministic skill execution
  -> verification, audit evidence, and release manifest
```

Safe controls such as `show plan`, `show diff`, `status`, `check drift`, `show audit`, and `cancel` are read-only or cancellation actions; they do not mutate Fabric state.

## Quick start

From the repository root:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

nfh-instantiation-agent chat
```

Use `exit`, `quit`, or Ctrl-D to leave the chat. Runnable API and operational examples are available in the [Runbook](guides/RUNBOOK.md).

## Governance and guardrails

- **Deterministic workflows:** code, not an LLM, selects supported operations and fixed skill chains.
- **Plan-bound approvals:** an approval belongs to one immutable plan; a relevant state change requires a new review and approval.
- **Exact approval phrases:** high-risk changes use explicit approval phrases rather than ambiguous confirmation.
- **Policy enforcement:** environment, skill, risk, source provenance, and auto-healing limits are evaluated through the policy bundle.
- **Runtime preflight:** generated Compose configuration, Docker availability, source provenance, and required images are checked before local execution.
- **Evidence-based releases:** the agent distinguishes running containers, reachable services, and a complete protocol transaction; it does not overstate verified outcomes.
- **Auditing and rollback:** plans, approvals, skill evidence, and release manifests are persisted for review and controlled rollback.

Read the [Governance and Guardrails guide](guides/GOVERNANCE_AND_GUARDRAILS.md) for the detailed model and direct references to the source files that enforce each control.

## Documentation

| Document | Description |
| --- | --- |
| [Documentation overview](guides/README.md) | Installation, project overview, and command reference. |
| [Governance and Guardrails](guides/GOVERNANCE_AND_GUARDRAILS.md) | Governance model, guardrails, and links to implementation and policy files. |
| [Architecture](guides/ARCHITECTURE.md) | System design, trust boundaries, fixed skill chains, runtime topology, and verification levels. |
| [User Guide](guides/USER_GUIDE.md) | Lifecycles, approvals, API surface, policy behavior, state, and source-file guide. |
| [Runbook](guides/RUNBOOK.md) | End-to-end setup and operational examples. |
| [Chat Journey](guides/CHAT_JOURNEY.md) | Step-by-step terminal chat examples. |
| [Local E2E Test Guide](guides/LOCAL_E2E_TEST_GUIDE.md) | Local Fabric bootstrap, participant setup, and protocol-testing guidance. |

## Rollback

Successful bootstrap or upgrade operations create a release manifest. Roll back a recorded release with:

```bash
nfh-instantiation-agent rollback \
  --state-db .instantiation-agent/state.sqlite3 \
  --release-id <release-id>
```

## Testing

Run the regular suite from the repository root:

```bash
pytest
```

Some integration tests require an explicitly configured LLM provider. See the [Runbook](guides/RUNBOOK.md) for the required environment and test commands.

## Repository layout

- `src/nfh_instantiation_agent/` is the only distributable application package.
- `tests/` contains the test suite and is excluded from built distributions.
- `.instantiation-agent/`, `build/`, `dist/`, and `*.egg-info/` are local/generated artifacts and are ignored by Git.

## Current verification scope

For a local Fabric, the release gate verifies stable expected services and required network listeners. A signed, correlated end-to-end Beckn transaction is protocol-level evidence and is tracked separately. See [Architecture](guides/ARCHITECTURE.md) for the current trust boundary.
