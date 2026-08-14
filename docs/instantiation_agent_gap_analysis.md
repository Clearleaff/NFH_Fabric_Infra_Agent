# Infrastructure Instantiation Layer Gap Analysis

## NFH Fabric Reading

NFH Fabric treats agents as first-class participants: they register their own identity/keypair in REGISTR, receive scoped and time-bounded verifiable credentials for authority, sign actions into Observability & Audit, may hold tokenisation accounts, and discover offerings through Discovery Edge. The linked product docs identify REGISTR for participant identity/roles, CATALG for catalog publication, DISCOVR for discovery, VC on Edge/OpenCred for DIDs and verifiable credentials, and policy docs for OPA/Rego bundles and signed manifests.

## Existing Repo Findings

The existing `tools/infra_agent` in `/home/cleaff/beckn-onix` already has useful bootstrap primitives: network intent validation, private-key detection, GCP VM/GKE Terraform rendering with GCS backend, user guide output, health checks, smoke search, and `_subscriber_payload`/`_registry_payloads` shapes for BAP/BPP/gateway registry registration. Its handover rollback section is still a skeleton with `TBD` placeholders.

The existing `tools/infra_orchestrator` has the right interaction pattern: a `SkillRunner`, state stages, deterministic cost estimation, session vault concept, explicit config/apply approval stages, audit calls, and an LLM tool-routing layer. It is still infrastructure-chat oriented and lets the LLM route many skill chains.

No `fabric_proposal_schema.json` existed in the writable workspace; the closest file found was `/home/cleaff/beckn-onix/tools/infra_agent/fabric_proposal.schema.json`.

## Capability Gaps

1. Participant roles: partial. Subscriber payload generation exists for BAP/BPP/gateway, but DID provisioning, capability credential issuance, per-participant onboarding/offboarding, credential revocation, and signed audit attribution were missing.
2. Catalog services: partial/missing. The orchestrator references Fabric proposals, but there was no standalone catalog publish/update/retire lifecycle that is explicitly isolated from Terraform.
3. Repeatable infra and upgrades: partial. Terraform rendering exists and GCS state is present for GCP, but content-hash idempotency, incremental upgrade gating, drift reconciliation, signed release manifests, and working rollback were missing.

## New Agent Direction

`tools/instantiation_agent` keeps three independently invokable lifecycle layers: bootstrap, participant lifecycle/catalog, and upgrade/drift. It reuses the proven subscriber payload shape and skill-runner approach while making LLM use narrow and deterministic guardrails mandatory.

