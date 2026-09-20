---
name: Jupiter cross-cluster service routing — Route53 internal zones
description: Jupiter runs multiple AWS accounts/clusters. Services NOT in the same cluster as the caller must be reached via Route53 internal-zone hostnames, NOT K8s svc.cluster.local DNS. Two confirmed cross-cluster accounts so far. Pattern + service registry below.
type: reference
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---

## The pattern

Jupiter has at least 3 AWS accounts running production clusters:
- `jupiterprod` (the main one — `jupiter` namespace, hosts bullet, growth/rewards, banking/dcms, gateway, lending-orchestrator, gatekeeper, etc.)
- `lending-v2prod` (separate AWS account — hosts lending platform services like `lending-lifecycle-manager`)
- `investmentprod` (separate AWS account — hosts investment services like `deposit-platform`)

**Within the same cluster:** services call each other via K8s in-cluster DNS:
```
http://<svc>-ms.<namespace>.svc.cluster.local:<port>
```
e.g. `http://bullet-ms.jupiter.svc.cluster.local:8080`, `http://lending-lifecycle-manager-ms.jupiter.svc.cluster.local:80`

**Cross-cluster:** the K8s DNS form is NOT resolvable. Use Route53 internal-zone DNS:
```
http://<svc>-ms.<account>.internal
```
- No `.svc.cluster.local`
- Usually no port (defaults to 80/443)
- The `<account>` is the destination AWS account

## Confirmed cross-cluster service registry

| Service | Repo (module) | Same-cluster URL (jupiterprod) | Cross-cluster URL | Confirmed by |
|---|---|---|---|---|
| LLM (Loan Lifecycle Manager) | `lending-lifecycle-manager` (Scala/Play) | `lending-lifecycle-manager-ms.jupiter.svc.cluster.local:80` | `lending-lifecycle-manager-ms.lending-v2prod.internal` (no port) | Nikhil Kataria, platform-eng, 2026-05-30 |
| deposit-platform | `jupiter-investments` → `deposit-platform/` module | (none — service runs in `investmentprod`) | `deposit-platform-blostem.investmentprod.internal` | Found in `jupiter-investments/investment-deposit/application.yml:276` (`blostemDepositPlatformService`), 2026-05-29 |

## When to invoke this knowledge

A caller (Aura, AI cluster, anything outside `jupiterprod`) reports:
- DNS-fail on `<svc>-ms.<ns>.svc.cluster.local` even though the service IS deployed
- Returns 503 / Connection-refused / NXDOMAIN
- The user has tried multiple namespaces (`jupiter`, `lending`, `cbs`) and nothing resolves

→ It's almost certainly cross-cluster. Suggest the Route53 form. Look for `*-ms.*.internal` hostnames in the service's own `application.yml` or any consumer config — the actual hostname is hardcoded there.

## Use lookup_service first (shipped 2026-05-31)

For "where is service X / what's the URL / what paths" — call `lookup_service(name_or_alias)` (agent tool) or `GET /api/v1/services/{name}` (HTTP) or `jarvis_lookup_service` (MCP). Returns both K8s and Route53 URLs from the pre-built nightly registry — sub-second answer, no grep needed. See `project_service_registry.md`.

## How to find new cross-cluster mappings (when registry misses)

`grep_all_repos("\\.internal$", file_glob="*.yml")` (or restrict by `*.yaml`) — surfaces every `<host>.<account>.internal` hostname hardcoded in any config across the indexed corpus. Pair the hostname with the surrounding config key to identify the service. Then add to `scripts/service_registry_overrides.json` so the next nightly build captures it permanently.

## Related

- The K8s service discovery pattern in `scripts/agent/agent.py` SYSTEM_PROMPT now tells the agent to try `lookup_service` FIRST and only fall back to consumer-config grep if it misses.
- Yesterday's stale-Feign discovery (`/bullet/v1/edgecard/internal/user/account/details` vs canonical `/bullet/v1/credit-card/user/account/summary`) is a SEPARATE issue from cross-cluster — Feign clients drift independent of cluster topology.
