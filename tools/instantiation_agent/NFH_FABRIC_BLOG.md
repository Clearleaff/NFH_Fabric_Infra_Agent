# NFH Fabric: Building a Governed Beckn Infrastructure Agent for Deployment and Management

*How I evolved a natural-language infrastructure assistant into a policy-controlled NFH/Beckn Fabric SRE that can build local Docker infrastructure, manage participants, and report runtime truthfully.*

Building a Beckn network locally looks simple on a whiteboard: start a registry, add a gateway, connect a BAP and BPP, publish a catalog, and send a search request. In practice, every one of those steps has operational and governance consequences. Which images are trusted? Which source commit was used? Did the containers merely start, or can the protocol really complete a transaction? What prevents a conversational model from turning an ambiguous sentence into an infrastructure change?

I built the NFH Fabric agent to answer those questions with a controlled workflow rather than a collection of shell commands. The goal was to make Beckn infrastructure conversational without handing an LLM deploy authority: execution still depends on deterministic code, approvals, source provenance, runtime checks, and audit evidence.

The same pattern can apply well beyond Beckn. Any infrastructure workflow that needs LLM convenience but not LLM authority can benefit from separating conversational planning from deterministic, policy-governed execution. NFH Fabric is my Beckn-specific implementation of that broader governed AI infrastructure pattern.

This is the story of what I built, what failed during real local testing, and what the agent now proves before it claims success.

The implementation discussed here lives in [Clearleaff/NFH_Fabric_Infra_Agent](https://github.com/Clearleaff/NFH_Fabric_Infra_Agent/). I have kept this article close to the repository's actual behaviour, including the parts that are still incomplete.

## The problem I was solving

A Beckn Fabric has several moving parts:

- a Registry for participant discovery and key material;
- a Gateway for network routing;
- a BAP (Beckn Application Platform) that starts buyer-side flows;
- a BPP (Beckn Provider Platform) that serves provider-side flows;
- supporting state such as Redis for the ONIX adapter;
- configuration, signing identities, domains, routes, and callback URLs.

The operational problem is not only provisioning these pieces. It is preventing accidental cross-network contamination, using the correct ONIX source, keeping approvals meaningful, and distinguishing a healthy container from a healthy protocol flow.

I wanted an operator to be able to say:

```text
create a local nfh fabric with one registry one bap one bpp
```

and then safely progress through a governed workflow instead of manually assembling a Compose file, guessing image names, and hoping that a `docker compose up -d` means the Fabric is usable.

## Natural language is an interface, not an authority

The most important architectural decision was to keep the LLM out of the execution path.

Groq can help interpret conversational input and extract possible fields. It cannot decide which skills run, run Docker, invoke Terraform, bypass an approval, or write untrusted configuration into a Compose file. This policy-governed LLM agent pattern keeps execution deterministic and places those decisions in Python code and a policy bundle.

```text
                        ┌────────────────────────────┐
                        │      Operator / Chat CLI    │
                        └─────────────┬──────────────┘
                                      │
                        natural-language request
                                      │
             ┌────────────────────────▼────────────────────────┐
             │ InstantiationChatService                         │
             │ session continuity • field collection • help     │
             │ cancel/cancle • show plan • why • status         │
             └────────────────────────┬────────────────────────┘
                                      │
             ┌────────────────────────▼────────────────────────┐
             │ Deterministic control plane                      │
             │ typo normalization • intent guard • schema       │
             │ multi-operation rejection • topology selection   │
             └───────────────┬──────────────────────┬───────────┘
                             │                      │
                    candidate fields only           │ fixed routing
                             │                      │
                    ┌────────▼────────┐    ┌────────▼──────────┐
                    │ Optional Groq   │    │ InstantiationAgent │
                    │ parsing/phrasing│    │ plan + skill chain │
                    └─────────────────┘    └────────┬──────────┘
                                                     │
              ┌──────────────────────────────────────▼────────────────────────────────────┐
              │ Policy bundle + immutable plan hash + exact approval phrases + SkillRunner │
              └──────────────────────┬────────────────────────────────────┬────────────────┘
                                     │ Local Docker                       │ GCP/Terraform
                       ┌─────────────▼─────────────┐          ┌────────────▼────────────┐
                       │ ONIX source pin + build    │          │ Deterministic IaC files │
                       │ Compose + runtime checks   │          │ cost and policy controls │
                       └─────────────┬─────────────┘          └─────────────────────────┘
                                     │
                       ┌─────────────▼─────────────┐
                       │ SQLite state and evidence  │
                       │ plans • approvals • audits │
                       │ desired state • manifests  │
                       └───────────────────────────┘
```

That separation gives the agent a useful conversational surface without granting a probabilistic system operational authority.

## Turning a request into a governed plan

The agent collects the minimum Fabric requirements progressively:

```text
network.name
network.environment
network.domain
runtime topology: local Docker or GCP/Terraform
```

For local Docker, the cloud-specific GCP fields are not requested. The control plane also understands common natural inputs and mistakes such as `network.enviroment`, `cancle`, `local`, or `name money`.

Before a mutation happens, the agent derives an immutable execution plan. The plan contains the desired-state hash, observed-state hash, diff hash, policy version, risk level, proposed actions, and required approvals. An approval is tied to that plan hash. If a field changes, the old approval becomes invalid.

For example, local bootstrap requires two separate phrases:

```text
approve bootstrap config
spend money now
```

The split matters. The first confirms the proposed configuration; the second confirms the actual infrastructure action. It is intentionally not possible to replace them with a vague “yes” or “go ahead.”

## Local NFH Fabric architecture for Beckn network automation

Once the plan is approved, the local runtime is built around a dedicated Docker Compose project. This Docker Compose deployment for the Beckn network is scoped to the Fabric, such as `nfh-demo-money`, which keeps one Fabric’s services from being accidentally reused by another.

```text
                        Pinned approved Beckn-ONIX source
                                  │
                    config + schemas copied into workspace
                                  │ read-only mounts
                                  ▼
 ┌─────────────────────┐   signed /search   ┌──────────────────┐    route/discovery   ┌─────────────────────┐
 │ BAP ONIX adapter    ├────────────────────►│ Beckn Gateway    ├─────────────────────►│ BPP ONIX adapter    │
 │ onix-bap-<name>     │                     │ gateway :4000    │                      │ onix-bpp-<name>     │
 │ published :8081     │                     └────────┬─────────┘                      │ published :8081     │
 └──────────┬──────────┘                              │                                └──────────┬──────────┘
            │                                         │                                           │
            │ cache + transaction state               │ subscriber/key lookup                     │ cache + transaction state
            ▼                                         ▼                                           ▼
      ┌─────────────┐                         ┌─────────────────┐                     ┌─────────────┐
      │ Redis       │                         │ Beckn Registry  │                     │ Redis       │
      │ internal    │                         │ registry :3000  │                     │ internal    │
      │ :6379       │                         │ worker :3030    │                     │ :6379       │
      └─────────────┘                         └─────────────────┘                     └─────────────┘
```

The agent does not use guessed ONIX image names. It resolves an approved ONIX source repository, pins a Git commit, inspects the source assets, and builds the adapter from that pinned source. The runtime uses the plugin-enabled adapter Dockerfile because ONIX’s local configuration requires those plugins.

The source resolver, Compose renderer, and runtime checks are part of the [NFH Fabric repository](https://github.com/Clearleaff/NFH_Fabric_Infra_Agent/). Tying the generated runtime to reviewed source is far easier to reason about than relying on undocumented `latest` image names.

## The bootstrap workflow

The local bootstrap path is a fixed chain of skills:

```text
intent diff
  → credential/reference handling
  → approved source resolution
  → plugin-enabled ONIX adapter build
  → local image preflight
  → Compose rendering and Docker validation
  → docker compose up -d
  → listener and service-stability verification
  → signed release manifest
```

Each skill is policy checked, audited, and has a defined stop condition. For example, if the source origin is not approved, if the source cannot be pinned, if a required image is unavailable, or if Compose validation fails, the agent stops before starting the Fabric.

## Guardrails that make the system governable

I designed the guardrails around the failures operators actually see in infrastructure automation.

| Area | Guardrail | Result |
|---|---|---|
| LLM safety | The LLM extracts candidates only; deterministic code selects the intent and skill sequence. | A model cannot execute arbitrary operations. |
| Approval | Exact phrases are bound to an immutable plan hash. | Changed plans invalidate previous approval. |
| Source supply chain | ONIX source origin is policy-approved and a commit is pinned. | The adapter is built from approved, inspectable code. |
| Runtime artifacts | Compose images must exist locally before execution. | No late failure from imaginary image references. |
| Configuration safety | Compose scalar values are rendered safely and ONIX config mounts are read-only. | User values do not become arbitrary Compose syntax. |
| Identity safety | Subscriber IDs are DNS-like and reject URLs, secret-like terms, and injected field content. | Participant identity cannot become configuration payload. |
| Fabric isolation | Participant records use Fabric-scoped keys. | A BAP/BPP from another Fabric is not rendered into this one. |
| Operational safety | `cancel` clears only pending state; `run` is deliberately non-executable. | Chat controls cannot silently run infrastructure. |
| Auditability | Every skill records audit evidence; releases get signed manifests and rollback references. | The agent leaves a traceable operational record. |

## What I learned by running it for real

The most useful part of this work was not the happy path. It was running a real local Fabric and allowing the runtime to prove the design wrong where it was wrong.

### Failure 1: fake or unavailable runtime images

The original local Compose generation referred to ONIX image names that were not available. Catching the pull failure was not enough; the real fix was a source resolver that validates the approved repository, pins the commit, builds the adapter, and checks every generated runtime image before Compose starts.

### Failure 2: adapters that start and immediately crash

At first, registry and gateway containers could be running while BAP/BPP adapters were crash-looping. The cause was real: the adapter had no `CONFIG_FILE`, no ONIX config/schema mount, and the bare adapter image lacked the plugins that the source configuration required.

I fixed the runtime generation to:

```text
use Dockerfile.adapter-with-plugins
mount generated pinned ONIX config read-only
mount schemas path read-only
set CONFIG_FILE=/app/config/local-simple.yaml
add Redis, required by the adapter configuration
use the source-configured adapter port :8081
```

I also changed the release safety check. It no longer verifies only registry and gateway listeners. If BAP or BPP is expected, the agent verifies its host listener and requires all expected Compose services—including Redis—to be stably running.

### Failure 3: chat parsing changed a Fabric name

During the guide test, I found a subtle conversation bug. A one-line join command containing `network.name` followed by natural fields such as `key id`, `callback`, and `key ref` could consume the trailing text as part of the Fabric name.

I fixed the parser to bound `network.name` to a valid DNS-like token before generic field extraction. I added regression tests and reran the complete guided chat flow successfully: bootstrap, BPP creation, BAP creation, and catalog publication all completed with the correct Fabric name.

The runnable guide and supporting agent code are maintained in [Clearleaff/NFH_Fabric_Infra_Agent](https://github.com/Clearleaff/NFH_Fabric_Infra_Agent/), so readers can compare this architecture with the implementation.

## BAP BPP local setup and catalog operations

After bootstrap, an operator can use the agent to create local participant adapters:

```text
join seller.demo as BPP to local network \
  key id seller-demo-key \
  callback http://seller.demo/bpp/receiver \
  key ref secret://seller-demo/signing

join buyer.demo as BAP to local network \
  key id buyer-demo-key \
  callback http://buyer.demo/bap/receiver \
  key ref secret://buyer-demo/signing
```

Those commands do more than write a record. They provision governed participant metadata, regenerate Compose for the Fabric, create the dedicated BAP/BPP adapter containers, and verify their listeners.

Catalog publication is also a governed lifecycle operation:

```text
publish catalog \
  catalog_id: dummy-money-exchange-v1 \
  subscriber_id: seller.demo \
  descriptor.name: Dummy Money Exchange
```

In a development policy it can complete after the normal policy check. In an environment configured to require catalog approval, the agent asks for the exact `approve catalog_publish_skill` phrase.

## The difference between runtime health and protocol health

This distinction is the core of reliable SRE automation.

```text
L1 — Runtime: expected containers are present and stable
L2 — Service: required listeners are reachable
L3 — Protocol: BAP request is signed, routed, registry-resolved,
               received by BPP, and correlated with an on_search response
```

The agent now requires L1 and L2 before a local release is signed. I also ran a real BAP `/search` request against the generated ONIX adapter. The request reached the BAP adapter, but correctly returned a NACK. For anyone troubleshooting a Beckn NACK signature verification error, this is the useful boundary: a reachable adapter does not prove that the Registry can resolve the signer’s key.

This is a protocol boundary, not a cosmetic limitation. The runtime ONIX sample configuration signs as its sample identities, while the agent’s local desired-state registration uses the Fabric’s participant identities. The agent does not yet submit matching authenticated subscriber/key records to the running Beckn Registry or generate per-participant adapter signer configuration from approved key material.

In other words:

```text
agent registration evidence          ✅
local BAP/BPP runtime                ✅
catalog lifecycle evidence           ✅
real request reaches ONIX BAP        ✅
registry/key/signature reconciliation ❌ pending
correlated BAP → BPP → on_search     ❌ pending
```

The right behavior is to report that NACK and keep the L3 claim blocked. A platform agent that says “transaction successful” merely because Docker is running is worse than no agent at all.

## Where I am taking it next

The next implementation milestone is clear:

1. generate a per-participant ONIX adapter configuration from the approved participant identity and key reference;
2. perform authenticated subscriber/key registration against the running Beckn Registry;
3. verify that gateway lookup resolves the exact BAP and BPP records;
4. send a BAP search and require a correlated BPP `on_search` response before reporting L3 success;
5. store transaction evidence alongside the release and audit records.

That will turn the current reliable local runtime Fabric into a protocol-verified Fabric.

## Closing perspective

With NFH Fabric, I am using an LLM as a conversational interface around infrastructure, while keeping the infrastructure itself governed by explicit code and policy.

The agent now has a controlled local Docker path, source-pinned ONIX builds, plan-bound approvals, scoped BAP/BPP creation, catalog governance, Compose validation, runtime verification, signed manifests, and a documented line between what it has proved and what it has not.

For Beckn infrastructure, that honesty is a feature. The useful agent is not the one that always says “done.” It is the one that can explain exactly what changed, what it verified, what failed, and what must happen next.

## Frequently asked questions

### Is this an LLM that can run Docker commands directly?

No. Natural language is used for conversational intake and candidate-field extraction. Policy checks, plan generation, approval validation, source resolution, Compose generation, and Docker execution remain deterministic code. The LLM does not choose arbitrary skills or receive shell authority.

### Why are there two bootstrap approvals in a natural language infrastructure deployment?

The first phrase approves the reviewed configuration; the second approves the infrastructure action. If the desired plan changes, its hash changes and the earlier approval becomes invalid.

### Does `join ... as BAP` or `join ... as BPP` create a real local participant?

Yes, at the local runtime layer. The agent records the Fabric-scoped role, regenerates Compose, creates a dedicated ONIX adapter container, and verifies expected listeners and service stability.

### Are BAP and BPP fully registered in the running Beckn Registry?

Not yet. The agent has governed desired-state registration evidence and working local adapters, but authenticated subscriber/key registration in the running Registry has not yet been reconciled with the per-participant adapter signer. That is why a real search remains NACK rather than being misreported as successful.

### Can I publish a catalog through the agent?

Yes. Catalog publishing is a governed lifecycle operation with policy checks, audit evidence, and environment-specific approval when policy requires it. The local catalog record is not yet automatically transformed into a BPP `on_search` response.

### What does source-pinned mean for a Beckn ONIX agent?

Before local deployment, the agent checks the ONIX source origin, resolves a specific commit, and builds the plugin-enabled adapter image from that commit. This makes generated artifacts traceable to an approved source revision.

### Where can I run the project myself?

Start with [Clearleaff/NFH_Fabric_Infra_Agent](https://github.com/Clearleaff/NFH_Fabric_Infra_Agent/). It is the source of truth for the current code, setup instructions, and release updates.

## References

- [NFH Fabric Infrastructure Agent repository](https://github.com/Clearleaff/NFH_Fabric_Infra_Agent/)
- [Beckn Protocol](https://becknprotocol.io/)
- [Beckn-ONIX source repository](https://github.com/beckn/beckn-onix)
- [Docker Compose documentation](https://docs.docker.com/compose/)

---

For the current implementation, setup material, and release updates, visit [Clearleaff/NFH_Fabric_Infra_Agent](https://github.com/Clearleaff/NFH_Fabric_Infra_Agent/).
