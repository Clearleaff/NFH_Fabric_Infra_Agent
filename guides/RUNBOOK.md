# Instantiation Agent Runbook

## Governed local run

Run `nfh-instantiation-agent chat`, request a local Beckn Fabric, provide its name/domain/environment, inspect with `show plan`, then use the existing exact approval phrases. Compose and Docker daemon checks run before startup; `status` and `check drift` remain read-only.

This runbook gives you a concrete way to test the agent locally.

## User Story

You are a network facilitator setting up a staging NFH Fabric network for retail commerce in India.

You want to:

1. Bootstrap the network infrastructure on GCP in plan/artifact mode.
2. Rerun bootstrap with the same intent and confirm it no-ops.
3. Onboard a seller as a BPP.
4. Publish that seller's catalog.
5. Try a safe incremental upgrade by scaling node count.
6. Run the live Groq tests to verify strict JSON parsing, cost phrasing, and secret log safety.

You can run the story two ways:

- Direct Python API calls against `InstantiationAgent`.
- Conversational turns through `InstantiationChatService`, where Groq parses intent but deterministic code executes skills.

## Prerequisites

Run commands from the repository root. Create an isolated environment and install the package:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

For live Groq tests:

```bash
export GROQ_API_KEY="your-groq-key"
export INSTANTIATION_AGENT_E2E_LIVE=true
```

## 1. Run The Unit Test Suite

```bash
pytest -q
```

Expected output without live env vars:

```text
45 passed, 3 skipped
```

Expected output with live Groq env vars:

```text
48 passed
```

## 2. Execute The User Story Locally

Run this from the repository root after installation:

```bash
python3 - <<'PY'
from pathlib import Path
from nfh_instantiation_agent import InstantiationAgent, Stage

intent = {
    "network": {
        "name": "retail-staging",
        "environment": "staging",
        "domain": "retail:1.1.0",
        "country": "IND",
    },
    "cloud": {
        "provider": "gcp",
        "topology": "gke",
        "project_id": "gcp-sandbox-project",
        "region": "asia-south1",
        "state_backend": "retail-staging-tfstate",
        "credential_ref": "gcp-adc",
    },
    "deployment": {
        "node_count": 2,
        "machine_type": "e2-standard-4",
        "adapter_version": "1.0.0",
    },
}

participant = {
    "subscriber_id": "seller.example",
    "role": "BPP",
    "unique_key_id": "seller-key-1",
    "public_url": "https://seller.example/bpp",
    "key_ref": "secret://seller/signing-key",
}

catalog = {
    "catalog_id": "seller-seeds-v1",
    "subscriber_id": "seller.example",
    "descriptor": {"name": "Seed catalog"},
    "items": [
        {"id": "seed-rice-1", "descriptor": {"name": "Rice seeds"}},
        {"id": "seed-wheat-1", "descriptor": {"name": "Wheat seeds"}},
    ],
}

agent = InstantiationAgent(
    state_db=Path(".instantiation-agent/demo.sqlite3"),
    workspace=Path(".instantiation-agent/work"),
    budget_cap_usd=500,
)

bootstrap = agent.bootstrap(
    intent,
    approvals={
        "config": "approve bootstrap config",
        "apply": "spend money now",
    },
)
print("BOOTSTRAP:", bootstrap.stage.value, bootstrap.cost_estimate)
print("BOOTSTRAP MESSAGE:", bootstrap.messages[-1])

rerun = agent.bootstrap(
    intent,
    approvals={
        "config": "approve bootstrap config",
        "apply": "spend money now",
    },
)
print("RERUN:", rerun.stage.value, rerun.diff)

onboard = agent.onboard_participant(intent, participant)
print("ONBOARD:", onboard.stage.value, onboard.participant["did"])

published = agent.publish_catalog(intent, catalog)
print("CATALOG:", published.stage.value, published.messages[-1])

upgraded_intent = {
    **intent,
    "deployment": {
        **intent["deployment"],
        "node_count": 3,
        "adapter_version": "1.1.0",
    },
}

upgrade = agent.upgrade(
    intent,
    upgraded_intent,
    approvals={"apply": "approve incremental upgrade"},
)
print("UPGRADE:", upgrade.stage.value, upgrade.diff["changed_paths"])

assert bootstrap.stage == Stage.DONE
assert rerun.stage == Stage.NOOP
assert onboard.stage == Stage.DONE
assert published.stage == Stage.DONE
assert upgrade.stage == Stage.DONE
PY
```

Expected shape:

```text
BOOTSTRAP: done {'currency': 'USD', 'monthly_cost': 254.0, ...}
RERUN: noop {'changed': False, ...}
ONBOARD: done did:nfh:participant:...
CATALOG: done Published catalog seller-seeds-v1.
UPGRADE: done ['deployment.adapter_version', 'deployment.node_count']
```

## 3. Execute A Conversational Onboarding Flow

This flow uses natural language. The LLM only chooses the intent and extracts candidate fields. The service still routes deterministically to `InstantiationAgent.onboard_participant`, and the participant skill sequence stays hardcoded.

```bash
export GROQ_API_KEY="your-groq-key"

python3 - <<'PY'
from pathlib import Path
from nfh_instantiation_agent import InstantiationAgent, InstantiationChatService
from nfh_instantiation_agent.llm import GroqLLM

service = InstantiationChatService(
    agent=InstantiationAgent(
        state_db=Path(".instantiation-agent/chat-demo.sqlite3"),
        workspace=Path(".instantiation-agent/work"),
    ),
    llm=GroqLLM(),
)

session_id = "seller-onboarding-demo"

first = service.chat(session_id, "onboard seller.example as a BPP in staging")
print("FIRST:", first.stage)
print(first.text)

second = service.chat(
    session_id,
    "key id key-1 callback https://seller.example/bpp key ref secret://seller/key",
)
print("SECOND:", second.stage)
print(second.text)
PY
```

Expected shape:

```text
FIRST: collecting
I can onboard the participant, but I need: unique_key_id, public_url, key_ref...
SECOND: done
Registered seller.example as BPP.
```

Approval phrases still must be literal. For example, if a bootstrap chat is waiting at `config_approval`, `yes` or `looks good` will not advance it. The user must type:

```text
approve bootstrap config
```

Then, at the apply gate:

```text
spend money now
```

## 4. Inspect Rendered Terraform

After bootstrap or upgrade:

```bash
find .instantiation-agent/work -maxdepth 3 -type f | sort
```

You should see files like:

```text
.instantiation-agent/work/<session_id>/terraform/README.md
.instantiation-agent/work/<session_id>/terraform/main.tf
.instantiation-agent/work/<session_id>/terraform/providers.tf
.instantiation-agent/work/<session_id>/terraform/upgrade.diff.json
```

This agent renders Terraform artifacts. It does not run `terraform apply`.

## 5. Execute A Local Docker Fabric

This local mode is intentionally different from the GCP path: after the same bootstrap approval gates, it runs Docker Compose directly. Use a disposable state database so repeated runs are clean.

```bash
python3 - <<'PY'
from pathlib import Path
import subprocess
from nfh_instantiation_agent import InstantiationAgent, Stage

intent = {
    "network": {
        "name": "local-fabric",
        "environment": "staging",
        "domain": "retail:1.1.0",
        "country": "IND",
    },
    "cloud": {
        "provider": "local",
        "topology": "local",
    },
    "deployment": {
        "adapter_version": "1.0.0",
    },
}

bap = {
    "subscriber_id": "bap3.local",
    "role": "BAP",
    "unique_key_id": "bap-key",
    "public_url": "http://bap3.local",
    "key_ref": "secret://bap3/key",
}

bpp = {
    "subscriber_id": "bpp2.local",
    "role": "BPP",
    "unique_key_id": "bpp-key",
    "public_url": "http://bpp2.local",
    "key_ref": "secret://bpp2/key",
}

agent = InstantiationAgent(
    state_db=Path(".instantiation-agent/local-demo.sqlite3"),
    workspace=Path(".instantiation-agent/work"),
)

bootstrap = agent.bootstrap(
    intent,
    approvals={
        "config": "approve bootstrap config",
        "apply": "spend money now",
    },
)
print("BOOTSTRAP:", bootstrap.stage.value, bootstrap.messages[-1])
assert bootstrap.stage == Stage.DONE
assert bootstrap.cost_estimate == {}

join_bap = agent.join_network(intent, bap)
join_bpp = agent.join_network(intent, bpp)
print("BAP:", join_bap.messages[-1])
print("BPP:", join_bpp.messages[-1])
assert join_bap.stage == Stage.DONE
assert join_bpp.stage == Stage.DONE

compose_dir = Path(agent.store.get_json("local_workspace:local-fabric"))
compose = compose_dir / "docker-compose.yml"
ps = subprocess.run(
    ["docker", "compose", "-f", str(compose), "ps"],
    capture_output=True,
    text=True,
    check=True,
)
print(ps.stdout)
assert "registry" in ps.stdout
assert "gateway" in ps.stdout
assert "onix-bap-bap3-local" in ps.stdout
assert "onix-bpp-bpp2-local" in ps.stdout

agent.offboard_participant(intent, "bap3.local", "BAP")

subprocess.run(["docker", "compose", "-f", str(compose), "down"], check=True)
print("LOCAL CLEANUP: docker compose down complete")
PY
```

If Docker is not installed or the images are unavailable locally, the agent returns a blocked or human-review message with captured Docker output instead of reporting success.

## 6. Test Approval Rejection

```bash
python3 - <<'PY'
from pathlib import Path
from nfh_instantiation_agent import InstantiationAgent

intent = {
    "network": {"name": "approval-demo", "environment": "staging", "domain": "retail:1.1.0", "country": "IND"},
    "cloud": {"provider": "gcp", "topology": "gke", "project_id": "sandbox", "state_backend": "tfstate", "credential_ref": "gcp-adc"},
    "deployment": {"node_count": 2, "machine_type": "e2-standard-4", "adapter_version": "1.0.0"},
}

agent = InstantiationAgent(state_db=Path(".instantiation-agent/approval-demo.sqlite3"))
state = agent.bootstrap(intent, approvals={"config": "yes", "apply": "yes"})
print(state.stage.value)
print(state.messages[-1])
PY
```

Expected:

```text
config_approval
Bootstrap needs exact phrase: approve bootstrap config
```

## 6. Test Budget Blocking

```bash
python3 - <<'PY'
from pathlib import Path
from nfh_instantiation_agent import InstantiationAgent

intent = {
    "network": {"name": "budget-demo", "environment": "staging", "domain": "retail:1.1.0", "country": "IND"},
    "cloud": {"provider": "gcp", "topology": "gke", "project_id": "sandbox", "state_backend": "tfstate", "credential_ref": "gcp-adc"},
    "deployment": {"node_count": 2, "machine_type": "e2-standard-4", "adapter_version": "1.0.0"},
}

agent = InstantiationAgent(state_db=Path(".instantiation-agent/budget-demo.sqlite3"), budget_cap_usd=10)
state = agent.bootstrap(intent, approvals={"config": "approve bootstrap config", "apply": "spend money now"})
print(state.stage.value)
print(state.messages[-1])
PY
```

Expected:

```text
blocked
Budget cap blocks apply: 254.0 exceeds 10.
```

## 7. Run Live Groq Tests

```bash
export GROQ_API_KEY="your-groq-key"
export INSTANTIATION_AGENT_E2E_LIVE=true
pytest tests/test_live_groq.py -q -s
```

Expected:

```text
LIVE SUMMARY: participant intent JSON parsed; Groq debug logs did not contain the credential value.
.LIVE SUMMARY: deterministic cost survived LLM phrasing without numeric drift.
.LIVE SUMMARY: natural-language onboarding conversation clarified missing fields and executed the deterministic participant skills.
.
3 passed
```

## 8. Rollback A Manifest

Release manifests are created after successful bootstrap and upgrade.

To list manifest IDs:

```bash
python3 - <<'PY'
import sqlite3
conn = sqlite3.connect(".instantiation-agent/demo.sqlite3")
for row in conn.execute("select release_id from manifests"):
    print(row[0])
PY
```

Then request rollback:

```bash
nfh-instantiation-agent rollback \
  --state-db .instantiation-agent/demo.sqlite3 \
  --release-id <release-id>
```

The command records the rollback request in state. It does not destroy or recreate cloud resources automatically.

## Troubleshooting

If live Groq tests skip, confirm:

```bash
echo "$INSTANTIATION_AGENT_E2E_LIVE"
test -n "$GROQ_API_KEY" && echo "GROQ key present"
```

If Groq returns 403 from Python but curl works, make sure [llm.py](../src/nfh_instantiation_agent/llm.py) includes `Accept: application/json` and `User-Agent: curl/8.4.0` headers.

If pytest warns about `.pytest_cache`, run the tests from a writable repository checkout or set a writable cache directory. The tests can still pass without cache writes.
