---
name: Jupiter tech stack and repo landscape
description: Snapshot of Jupiter's GitHub estate as of 2026-05-11 — language mix, repo counts, and notable repos
type: project
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
**Snapshot date:** 2026-05-11. Re-verify against `gh repo list jupitermoney` if assumptions matter.

**Repo counts (jupitermoney org):**
- 372 total repos on GitHub (226 internal, 142 private, 4 public). Org-summary endpoint underreports — it doesn't include INTERNAL visibility.
- 369 non-archived / non-fork / non-empty.
- ~212 active in last 30 days.
- Note: Rohit referred to "150+ repos" — the real active count is closer to 200+.

**Language mix (active repos, primary language):**
- Kotlin — 95 (dominant; backend services)
- Scala — 24 (likely data/streaming)
- Python — 22 (ML, data, scripts, airflow)
- TypeScript — 16 (frontends + BFFs)
- HCL — 7 (Terraform infra)
- Java — 9, JS — 6, Mustache — 6, Shell — 5

**Notable / high-leverage repos identified:**
- `platform` (Kotlin, 92MB) — powers 6 services: Auth, Lobby, Pay, Consent Management, DMS, Mitter. Mono-style.
- `bff-core` (TS, 53MB) — central BFF.
- `lms` (Kotlin, 77MB) — Lending Management System.
- `jupiter` (TS/React Native, 596MB) — customer-facing mobile app. Largest single repo.
- `gateway` (HTML primary, 53MB) — edge services exposing private services to frontend. Verify if it's a service or a portal.
- `bank-transfer` (Kotlin) — payment rails.
- `ds-jm-fraud-detector` (Java) — fraud detection.
- `prod.jupiter.money` / `staging.jupiter.money` (HCL) — production / staging Terraform.
- `kotlin-utils` — shared common-code patterns (defer to Phase 1).
- `airflow-dags` (Python, 30MB) — data pipelines.

**How to apply:**
- Default mental model: Kotlin backend monolith-ish (`platform`) + Kotlin domain services (`lms`, `bank-transfer`) + TS BFF + RN mobile + HCL infra.
- When indexing strategy matters, treat `platform` as multi-service (per-subdir chunking) and `jupiter` as size-stress-test.
- Repo metadata cache lives at `~/jarvis/index/repos.json` on the remote box.
