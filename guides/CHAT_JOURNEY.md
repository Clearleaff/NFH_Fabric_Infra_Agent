# Instantiation Agent Chat Journey

## SRE chat controls

`hi`, `help`, `show plan`, `show diff`, `why do you need approval`, `show current intent`, `status`, `check drift`, `show audit`, and `cancel` are safe control turns, not field values. Approvals are attached to the displayed plan hash.

Questions about requirements are also safe: `what do you need to build a local Beckn network with a registry?` returns local-only network requirements. It does not request GCP credentials or start a pending apply.

This guide shows how to test the local terminal chat REPL:

```bash
export GROQ_API_KEY="your-rotated-groq-key"
nfh-instantiation-agent chat
```

Type one prompt, read the answer, then type the next prompt. Exit with `quit`, `exit`, or Ctrl-D.

Important: if you use `production`, mutating actions need exact approval phrases. `yes`, `ok`, or `approved` will not work.

## User Story

As an NFH Fabric operator, I want to chat with the instantiation agent so that I can create a network, register BAP/BPP participants, publish/update/retire catalogs, and check status turn by turn without starting any HTTP server.

Acceptance checks:

- One terminal chat session remembers earlier fields.
- Provider/BPP registration accepts subscriber id, callback URL, role/type, and signing key details.
- BAP registration works with BAP/BAAP wording.
- Catalog publish, update, and retire work for a provider.
- Production actions ask for exact approval phrases.
- Bootstrap asks for production skill approval, then config approval, then apply approval.
- A bad turn prints an error and returns to the prompt instead of crashing.

## Journey 1: Add A Provider/BPP

Prompt:

```text
add provider bap3.local as BAP in staging
```

Expected response asks for missing key and callback fields.

Prompt:

```text
subscriber_id: bap3.local
url: http://onix-bap3-local:8093/bpp/receiver
type: agent
signing_public_key: "BVl+pRYl232qPt0PWrAqR+ecNMmEeyNoMGUcnEpH4Q4="
network_id: local.fabricsandbox/production
status: active
```

Because this says `production`, approve each production skill:

```text
approve did_provision_skill
approve capability_credential_skill
approve role_registration_skill
```

Expected final response:

```text
Registered bpp2.local as BPP.
```

For a no-approval local test, use `network_id: local.fabricsandbox/staging` instead of production.

## Journey 2: Publish Catalog

Prompt:

```text
publish catalog catalog id bpp2-seeds-v1 descriptor name Local Seeds for bpp2.local
```

If the session is production, approve:

```text
approve catalog_publish_skill
```

Expected response:

```text
Published catalog bpp2-seeds-v1.
```

## Journey 3: Update Catalog

Prompt:

```text
update catalog catalog id bpp2-seeds-v1 descriptor name Local Seeds Updated for bpp2.local
```

If the session is production, approve:

```text
approve catalog_update_skill
```

Expected response:

```text
Updated catalog bpp2-seeds-v1.
```

## Journey 4: Retire Catalog

Prompt:

```text
retire catalog catalog id bpp2-seeds-v1
```

If the session is production, approve:

```text
approve catalog_retire_skill
```

Expected response:

```text
Retired catalog bpp2-seeds-v1.
```

## Journey 5: Register A BAP

Prompt:

```text
add baap bap1.local key id bap-key-1 callback http://onix-bap-local:8081/bap/receiver key ref secret://bap1/signing in staging
```

Expected response:

```text
Registered bap1.local as BAP.
```

If you use `production`, approve:

```text
approve did_provision_skill
approve capability_credential_skill
approve role_registration_skill
```

## Journey 6: Create A New Network

Prompt:

```text
create new network network.name: local-fabric network.domain: retail:1.1.0 production project_id: sandbox state_backend: tfstate credential_ref: gcp-adc
```

Approve the production bootstrap skill:

```text
approve bootstrap_skill
```

Approve rendered config:

```text
approve bootstrap config
```

Approve apply:

```text
spend money now
```

Expected final response starts with:

```text
Release manifest
```

The full response includes a rollback command.

## Journey 7: Check Status

Prompt:

```text
status
```

Expected response shape:

```text
Session stage: done. Pending intent: status.
```

When you paste provider details, `status: active` is treated as a field line, not as the status command.

## Journey 8: Local Network

Bootstrap a local Docker fabric:

```text
create local network network.name: local-fabric network.domain: retail:1.1.0 staging
approve bootstrap config
spend money now
```

Expected final response names the registry and gateway ports:

```text
Local network 'local-fabric' is up: registry:<port>, gateway:<port>. 0 participants joined.
```

Join a BAP:

```text
join bap3.local as BAP to local network network.name: local-fabric key id bap-key callback http://bap3.local key ref secret://bap3/key
```

Expected response:

```text
bap3.local joined local-fabric as BAP. Container onix-bap-bap3-local is running.
```

Join a BPP:

```text
join bpp2.local as BPP to local network network.name: local-fabric key id bpp-key callback http://bpp2.local key ref secret://bpp2/key
```

Publish, update, and retire a catalog for the local BPP:

```text
publish catalog catalog id bpp2-seeds-v1 descriptor name Local Seeds for bpp2.local
update catalog catalog id bpp2-seeds-v1 descriptor name Local Seeds Updated for bpp2.local
retire catalog catalog id bpp2-seeds-v1
```

Deregister a participant:

```text
remove participant bap3.local as BAP from local network network.name: local-fabric
```

Check local status:

```text
status
```

For production local sessions, approve the same production mutating skills exactly, including `approve local_execution_skill` when local container execution is gated by policy.

## Automated Tests

Run the deterministic journey tests:

```bash
pytest tests/test_chat_journeys.py
```

Run all instantiation-agent tests:

```bash
pytest
```

Live Groq tests require a valid key in your own shell:

```bash
export GROQ_API_KEY="your-rotated-groq-key"
INSTANTIATION_AGENT_E2E_LIVE=true pytest tests/test_live_groq.py -s
```
