---
name: Service registry + lookup_service — shipped 2026-05-31
description: Pre-built nightly registry of ~200 Jupiter microservices + lookup_service tool exposed on agent / MCP / HTTP. Killed the recurring "Aura hits MAX_ITERATIONS doing service discovery" pattern.
type: project
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
## What shipped

End-to-end service discovery in one tool call (was 18 iterations + $1 of grep loops on average for Aura's repeated "where is service X" questions).

**Source code (commits `4b3c859` + `904e2b7` + `1b301d6`, all on jupitermoney/jarvis main):**
- `scripts/build_service_registry.py` — scans ~250 cloned repos for K8s + Route53 URL hardcodes, maps Gradle/SBT modules to source repos, extracts OpenAPI paths, applies operator overrides. Output: `~/jarvis/index/service_registry.json` (~200 services, 2-3 min build).
- `scripts/service_registry_overrides.json` — operator-curated supplements (versioned). 3 entries seeded: LLM (aka lending-lifecycle-manager-ms with Nikhil's Route53 confirmation), deposit-manager-ms (legacy), deposit-platform-blostem (active replacement).
- `scripts/agent/tools.py` — `lookup_service(name_or_alias)` function. Two-pass alias index: pass 1 = canonical + auto-derived `-ms`-stripped, pass 2 = explicit aliases (overrides always win). Prevents shadow collisions like `llm-ms` (auto-discovered stale config) hijacking `llm` → `lending-lifecycle-manager-ms`.
- `scripts/jarvis_mcp/server.py` — `jarvis_lookup_service` MCP wrapper
- `scripts/api/server.py` — `GET /api/v1/services/{name}` (Bearer auth)
- `scripts/agent/agent.py` — SYSTEM_PROMPT tells agent to try `lookup_service` FIRST for service-discovery questions, only fall back to `grep_all_repos` on miss.
- `scripts/agent/capabilities.py` — manifest entry (shipped 2026-05-31, category=qa)

## Tracked services (drift-alert trigger list)

`scripts/check_registry_drift.py` DMs Rohit if any of these vanish from a new nightly build:
- bullet-ms, lending-lifecycle-manager-ms, deposit-platform-blostem, deposit-manager-ms, platform-auth-ms, bff-core

**Why:** services I've personally documented or Q&A-validated. Adding to the list = adding a paged-alert promise; grow this list deliberately, not reflexively.

## Drift policy

`check_registry_drift.py` runs after every nightly registry build. Alerts on:
1. Service count drops > 10% (broad indexing breakage)
2. Tracked service disappears
3. Overrides entry not in new registry (builder skipped it)

Snapshot rolls forward on every run (drift or not) — single DM per onset, not nightly re-pages on a persistent degraded state. Override DM target via `JARVIS_ALERTS_CHANNEL` env var (defaults to operator UID per CLAUDE.md alert-routing rule).

## How to extend

- **Add a service alias / cross-cluster URL platform-eng confirmed verbally:** edit `scripts/service_registry_overrides.json` (versioned config), push, next nightly picks it up. Or trigger manual build: `ssh box && bash ~/jarvis/scripts/build_service_registry.sh`.
- **Add a tracked-service paged-alert entry:** edit `TRACKED_SERVICES` set in `check_registry_drift.py`.
- **Promote registry build out of nightly into incremental:** not needed yet (~3min nightly cost). Revisit when service count or rebuild time grows materially.

## Background — what this killed

Pre-2026-05-31: Aura repeatedly hit MAX_ITERATIONS=18 on "where is service X / what URL / what paths / who calls" questions. Even after I shipped `grep_all_repos` (cross-repo grep), agent burned 18 iterations stitching together K8s URL + OpenAPI paths + consumer list manually. Now it's one `lookup_service` call.

Validated by smoke: agent answers "Where is bullet-ms deployed and what HTTP paths does it expose?" in 1 tool call, 29s, $0.10 — vs ~$1+ and 60-90s previously.
