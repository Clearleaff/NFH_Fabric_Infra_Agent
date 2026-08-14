# Local NFH Fabric E2E Test Guide

Ye guide local machine par agent ke through ek dummy NFH/Beckn Fabric banane ke liye hai:

```text
Fabric bootstrap → BPP register → BAP register → catalog publish → BAP search request
```

Docker daemon running hona chahiye. Groq key optional hai: deterministic local field extraction and guardrails key ke bina bhi work karte hain. Chat mein API keys paste mat karo.

## 1. Start the agent chat

Repository root se:

```bash
nfh-instantiation-agent chat
```

Har `>` line ke baad ek command enter karni hai.

## 2. Create a local NFH Fabric

Is exact flow ko use karo:

```text
> create a local nfh fabric with one registry one bap one bpp
> name demo-money
> network.environment dev
> network.domain retail:1.1.0
```

Expected response:

```text
Bootstrap needs exact phrase: approve bootstrap config
```

Phir approvals do:

```text
> approve bootstrap config
> spend money now
```

Successful output mein release manifest aur rollback command aayega. Is point par registry aur gateway local Docker Compose mein running hone chahiye.

Check:

```text
> status
```

Status Fabric name aur running services ke localhost ports dikhayega. `nfh-demo-money` project ka registry/gateway alag rahega; kisi aur Fabric ke containers mix nahi honge.

## 3. Register a dummy BPP (provider)

### What this command creates locally

`join ... as BPP` is not just a database insert. The agent does these deterministic steps:

```text
1. validates seller.demo identity
2. provisions its synthetic DID and capability credential
3. records the Fabric-scoped BPP registration evidence
4. regenerates this Fabric's Docker Compose
5. creates/reconciles onix-bpp-seller-demo container
6. mounts pinned ONIX config, schemas, and plugin-enabled adapter image
7. waits for BPP port 8081 and all expected services to be stable
```

So the local BPP is the `onix-bpp-seller-demo` Docker container; it is not a remote URL that you must create manually.

Dummy BPP values:

| Field | Value |
|---|---|
| Subscriber ID | `seller.demo` |
| Role | `BPP` |
| Key ID | `seller-demo-key` |
| URL | `http://seller.demo/bpp/receiver` |
| Key reference | `secret://seller-demo/signing` |

Chat command:

```text
> join seller.demo as BPP to local network key id seller-demo-key callback http://seller.demo/bpp/receiver key ref secret://seller-demo/signing
```

For a `dev` Fabric, this should reconcile Compose and start an `onix-bpp-seller-demo` adapter container. Verify it:

```text
> status
> discover BPP
```

`discover BPP` currently shows the Fabric-scoped registration evidence. It is not an assertion that the external registry has completed a cryptographic L3 subscriber lookup.

To prove that the BPP container itself was created, copy the Compose path from agent `status` output (or locate it under `.instantiation-agent/work/.../local/`) and run:

```bash
docker compose -f /absolute/path/to/docker-compose.yml ps onix-bpp-seller-demo
```

Expected state: `running` / `Up`, with a localhost mapping ending in `->8081/tcp`.

## 4. Register a dummy BAP (buyer app)

### What this command creates locally

The BAP command follows the same pattern and creates the local `onix-bap-buyer-demo` ONIX adapter container. It is the BAP process that receives the later `/bap/caller/search` request.

Dummy BAP values:

| Field | Value |
|---|---|
| Subscriber ID | `buyer.demo` |
| Role | `BAP` |
| Key ID | `buyer-demo-key` |
| URL | `http://buyer.demo/bap/receiver` |
| Key reference | `secret://buyer-demo/signing` |

Chat command:

```text
> join buyer.demo as BAP to local network key id buyer-demo-key callback http://buyer.demo/bap/receiver key ref secret://buyer-demo/signing
```

Expected successful local-runtime message looks like:

```text
buyer.demo joined demo-money as BAP. Container onix-bap-buyer-demo is running.
```

Run `status` again. You should now see registry, gateway, Redis, BAP adapter, and BPP adapter. The agent checks each expected container and each published BAP/BPP listener before it calls this local join successful.

To inspect both locally created agents directly:

```bash
docker compose -f /absolute/path/to/docker-compose.yml ps \
  onix-bap-buyer-demo onix-bpp-seller-demo
```

## 4.1 Registration: what is complete vs pending

There are two different meanings of “register”, and they must not be confused:

| Registration layer | Current result after `join` |
|---|---|
| Agent/Fabric desired state | Complete. The BAP/BPP role is scoped to `demo-money`, audited, and used to generate its local adapter container. |
| Docker runtime | Complete only when the adapter container is stable and port `8081` is reachable. |
| Running Beckn Registry subscriber/key API | **Pending implementation.** The agent has not yet submitted a signed subscriber/key record that matches the per-participant adapter signer. |

A proper full network registration test must prove this final API-level relationship:

```text
buyer.demo adapter signing key
          ==
Registry subscriber record for buyer.demo / buyer-demo-key

seller.demo adapter public endpoint and key
          ==
Registry subscriber record for seller.demo / seller-demo-key
```

Until that is implemented, the guide's BAP search is deliberately expected to NACK at the signing/registry boundary. This is safe behavior, not a successful BAP-to-BPP transaction.

## 5. Publish a dummy catalog

Use this catalog:

```text
Catalog ID:      dummy-money-exchange-v1
Provider:        seller.demo
Display name:    Dummy Money Exchange
Example item:    USD to INR
```

In chat:

```text
> publish catalog catalog_id: dummy-money-exchange-v1 subscriber_id: seller.demo descriptor.name: Dummy Money Exchange
```

In a `dev` Fabric, publication completes immediately after policy checks. In an environment whose policy requires catalog approval, the agent will ask for the exact phrase below:

```text
> approve catalog_publish_skill
```

Expected output:

```text
Published catalog dummy-money-exchange-v1.
```

The catalog lifecycle is governed and audited. At present it is persisted via the agent's Fabric client state; it is not yet automatically transformed into a BPP `on_search` business response.

## 6. Send a real BAP `search` transaction

First get the BAP host port from:

```text
> status
```

Find the line for `onix-bap-buyer-demo`; it contains a mapping like:

```text
127.0.0.1:35350->8081/tcp
```

Here `BAP_PORT` is `35350`. Use your actual port, not that example value.

Run from another repository-root terminal:

```bash
BAP_PORT=35350
TXN_ID="demo-money-$(date +%s)"

curl -i -sS --max-time 20 \
  -X POST "http://127.0.0.1:${BAP_PORT}/bap/caller/search" \
  -H 'Content-Type: application/json' \
  --data "{
    \"context\": {
      \"domain\": \"retail:1.1.0\",
      \"country\": \"IND\",
      \"city\": \"std:080\",
      \"action\": \"search\",
      \"core_version\": \"1.1.0\",
      \"version\": \"1.1.0\",
      \"bap_id\": \"buyer.demo\",
      \"bap_uri\": \"http://onix-bap-buyer-demo:8081/bap/receiver\",
      \"transaction_id\": \"${TXN_ID}\",
      \"message_id\": \"${TXN_ID}\",
      \"timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"
    },
    \"message\": {
      \"intent\": {
        \"item\": {\"descriptor\": {\"name\": \"USD to INR\"}}
      }
    }
  }"
```

## What is currently expected from the transaction

The request is a real request to the generated ONIX BAP adapter. Current runtime result is expected to be a truthful `NACK` / `NET_INTERNAL_ERROR`, not a fake ACK. The agent's runtime work has verified that BAP/BPP containers start with source-pinned configuration and plugins; the remaining semantic/L3 blocker is:

```text
agent desired-state role registration
        is not yet
authenticated running-registry subscriber/key registration

and

per-participant runtime adapter signer identity
        is not yet generated from the registered participant identity.
```

The source sample config signs as `bap-network` / `bpp-network`, while this guide registers `buyer.demo` / `seller.demo`. ONIX therefore correctly refuses to find the signing key for `buyer.demo`.

## What counts as passed today

| Test | Expected result |
|---|---|
| Bootstrap | Signed release manifest after source/build/Compose verification |
| BAP/BPP joins | Dedicated stable adapter containers, reachable on generated ports |
| Catalog publish | Approved, audited `PUBLISHED` catalog record |
| BAP `/search` reaches adapter | Actual HTTP request and ONIX processing logs |
| Full BAP → gateway → BPP → `on_search` | **Not implemented yet**; must remain NACK/blocked until runtime registry/key reconciliation exists |

## Optional Docker inspection

The agent prints service ports through `status`. If deeper inspection is needed, find the generated Compose file in `.instantiation-agent/work/.../local/docker-compose.yml`, then run:

```bash
docker compose -f /absolute/path/to/docker-compose.yml ps
docker compose -f /absolute/path/to/docker-compose.yml logs --tail=100 onix-bap-buyer-demo
```

Do not use `run` in agent chat; it is intentionally a non-executable clarification command. Use `show plan`, the exact requested approval phrase, `status`, or the operation you want to perform.
