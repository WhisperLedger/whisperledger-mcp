---
name: Edge layer — Stargate, Bifrost, gatekeeper
description: Jupiter's edge services (Stargate, Bifrost) live inside the `gateway` repo, not separate repos. `gatekeeper` is a related auth/policy layer.
type: project
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
**Stargate** and **Bifrost** are Jupiter's edge-layer services — the entry points exposing private backend services to clients. They are not separate repos: both live as subdirectories inside the `gateway` repo (e.g., `bifrost/docs/stargate.md`).

Related repo:
- `gatekeeper` — referenced from `restapi/brahma/stargate/v2.py`. Likely auth/policy layer in front of Stargate.

Cross-references discovered (consumers of Stargate/Bifrost):
- `gateway` (the repo itself)
- `gatekeeper`
- `jupiter-web-platform` (uses `src/platform/api/stargateClient.ts`)
- `prod.jupiter.money` (deploys `services/stargate/pdb.yaml`)
- `user-vault`

**How to apply:**
- When Rohit/devs say "Stargate" or "BiFrost", look in the `gateway` repo, not for repos with those names.
- `gateway` is a high-leverage Phase 0/1 candidate — it's the edge for all client traffic and is referenced by many consumers.
- Architecture mental model: clients → `gateway` (Stargate/Bifrost) → backend services (`platform`, `lms`, etc.). `gatekeeper` likely sits in this path for auth.
