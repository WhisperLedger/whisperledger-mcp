"""Single source of truth for what the platform can/can't do.

Updated whenever a feature ships. The agent calls `get_capabilities()` (defined
in tools.py) to introspect this manifest when answering meta-questions like
"what can you do", "are you good at X", "do you have a Y feature".

Goal: avoid the failure mode where the system prompt is stale and the agent
freelances about its own capabilities. Update THIS FILE on every feature ship.
The implementation is deliberately org-agnostic so it can be deployed by any
company without rewriting the capability model.
"""
from __future__ import annotations

# Each entry has a stable schema:
#   command          — exact slash-command string (or "n/a" for ambient features)
#   name             — short human-readable name
#   category         — qa | code-review | code-edit | docs | meta | infra
#   summary          — one-sentence what-it-does
#   when_to_use      — bulleted plain-text guidance (use when …)
#   when_NOT_to_use  — bulleted guidance (use OTHER tools when …)
#   example          — one concrete invocation
#   cost_range_usd   — typical $ range per invocation
#   duration_typical — typical end-to-end time
#   scope            — what's covered (repos, branches, etc.)
#   limits           — bulleted hard limits
#   shipped          — date in IST
#   shipped_by       — feature attribution

CAPABILITIES = [
    {
        "command": "(agent tool) search_multi",
        "name": "Parallel multi-query code search",
        "category": "retrieval",
        "summary": (
            "Runs up to 5 search_code queries in parallel (ThreadPoolExecutor), "
            "deduplicates hits by chunk identity, returns merged results sorted by score. "
            "Collapses N sequential search_code round-trips into 1 iteration — "
            "primary lever for reducing agent loop latency on multi-faceted questions."
        ),
        "when_to_use": [
            "Question spans multiple sub-topics (e.g. 'payment flow' needs both state machine + controller + entry point)",
            "You already know you'll need 2+ search angles before seeing results",
            "Replacing sequential search_code calls that would otherwise add 1 iteration each",
        ],
        "when_NOT_to_use": [
            "Single focused lookup — use search_code directly",
            "Exact symbol lookup — use lookup_symbol (deterministic, sub-second)",
        ],
        "example": 'search_multi(["payment state machine", "PaymentController handler", "payment flow entry"])',
        "cost_range_usd": "~$0.003 × N queries (same per-query cost as search_code, run concurrently)",
        "duration_typical": "2-4s regardless of N (parallel, not sequential)",
    },
    {
        "command": "(implicit) ACL-gated restricted repos",
        "name": "Restricted repo access (risk / bureau / feature-store)",
        "category": "meta",
        "summary": (
            "Some repos live in a separate Qdrant collection with an allowlist. "
            "Slack + MCP surfaces gate on caller identity; HTTP API (Jove / SRE bot / JPE) "
            "is BLIND to restricted collections regardless of caller. Fail-closed: "
            "unrecognized callers see only the public corpus."
        ),
        "when_to_use": [
            "Ask about ds-jm-risk-* repos — only visible if you are on the allowlist",
            "Same /jarvis and MCP tools; restricted content just fans in automatically",
        ],
        "when_NOT_to_use": [
            "Do NOT try to route restricted questions through /api/v1/ask — services see nothing from restricted collections",
        ],
        "example": "/jarvis how does the CIBIL API compute account_mod_limit for BL loans?",
        "cost_range_usd": "same as /jarvis",
        "duration_typical": "same as /jarvis",
        "scope": "Restricted collections defined in ~/.config/jarvis/restricted_acl.json (currently: jarvis_restricted_risk with 4 ds-jm-risk-* repos)",
        "limits": [
            "Slack: gated on Slack user ID",
            "MCP: gated on per-engineer bearer token → caller email",
            "HTTP API: not searchable — services always see only jarvis_code",
            "PR descriptions from restricted repos are NOT indexed (v1)",
            "Hybrid BM25 not built for restricted collection (falls back to vector-only)",
            "All hits into a restricted collection audited to ~/jarvis/logs/restricted_access.jsonl",
        ],
    },
    {
        "command": "/jarvis <natural-language question>",
        "name": "Q&A across the indexed code corpus",
        "category": "qa",
        "summary": (
            "Semantic search + reasoning across all indexed repos in the configured org + "
            "recent PR descriptions + commit history. Cites file:line for every claim."
        ),
        "when_to_use": [
            "How does X work? Where does Y live? Why was Z built this way?",
            "End-to-end flow questions across multiple services",
            "'Find me the canonical state machine for X' / 'what enum values exist for Y'",
            "Multi-turn investigations — same /jarvis within 10 min continues the conversation",
        ],
        "when_NOT_to_use": [
            "Real-time production data (no live system access)",
            "Confluence-only content without a page URL — Jarvis has no global Confluence search",
            "Anything that requires running tests or compiling code",
        ],
        "example": "/jarvis explain the personal loan application creation flow end-to-end",
        "cost_range_usd": "0.05-0.60",
        "duration_typical": "30-90s (multi-iteration investigations longer)",
        "scope": "All 261 indexed jupitermoney repos · default branches · last ~6 months of PR descriptions",
        "limits": [
            "Static corpus only (no live data)",
            "Indexed repos only (~115 of 376 not indexed: *-prod.internal, claude-plugins, github-metadata, etc.)",
            "No global Confluence search; `/api/v1/ask/stream` can use one pasted page URL as live source context",
        ],
        "shipped": "2026-05-09",
    },
    {
        "command": "POST /api/v1/ask/stream with a Confluence URL in `question`",
        "name": "Direct Confluence-page grounded streaming Q&A",
        "category": "qa",
        "summary": (
            "When the caller pastes one Jupiter Atlassian Confluence page URL into the existing "
            "question, Jarvis detects it and asks Jove to fetch the live page directly. Jarvis then "
            "answers using that supplied document alongside its normal code-retrieval tools."
        ),
        "when_to_use": [
            "A Merlin user asks a code or architecture question about one linked RFC, runbook, RCA, or PRD",
            "The page may be newer than Jove's index and you need its current text",
            "You want an answer that relates the supplied document to the indexed codebase",
        ],
        "when_NOT_to_use": [
            "Searching Confluence generally or choosing between several documents — v1 reads one explicit page only",
            "Live production-state questions — a document is reference material, not current system state",
        ],
        "example": "POST /api/v1/ask/stream {question: 'Does this RCA match the controller code? https://jupitermoney.atlassian.net/wiki/spaces/TECH/pages/12345/...'}",
        "cost_range_usd": "Jarvis Q&A cost plus a small live-document input increment (up to 10K characters); no indexing required",
        "duration_typical": "Adds one Jove/Confluence read before Jarvis's normal 30-90s streaming investigation",
        "scope": "One HTTPS Atlassian Confluence page URL found in `question`; Jove validates the configured tenant and page id",
        "limits": [
            "Only `/api/v1/ask/stream` supports this in v1; Slack and non-streaming `/api/v1/ask` are unchanged",
            "No client-specific request field: Merlin remains a pass-through orchestrator",
            "The content is capped by Jove at 10K characters; Jarvis discloses truncation when relevant",
            "If Jove cannot read the supplied page, Jarvis returns 502 rather than answering without the source",
        ],
        "shipped": "2026-08-12",
    },
    {
        "command": "/jarvis fix <repo>: <task description>",
        "name": "Open a draft PR for a small fix",
        "category": "code-edit",
        "summary": (
            "Investigates the repo, drafts a fix for a small task, opens a DRAFT PR for "
            "human review. Delegates to Claude Code in an isolated workspace, $2/run cap."
        ),
        "when_to_use": [
            "Small contained fixes (one-file changes, header additions, simple refactors)",
            "When you want a starting point you can iterate on rather than writing from scratch",
            "Allowlisted repos only: bff-core, jupiter, jarvis",
        ],
        "when_NOT_to_use": [
            "Anything multi-file or architectural — humans should design the change",
            "Anything that requires understanding live business state",
            "Repos NOT in the allowlist — ping Rohit to expand if needed",
        ],
        "example": "/jarvis fix bff-core: add x-trace-id to standard-headers.ts",
        "cost_range_usd": "0.50-2.00 (hard cap $2)",
        "duration_typical": "1-3 min",
        "scope": "Allowlisted repos: bff-core, jupiter, jarvis",
        "limits": [
            "Always opens DRAFT — never auto-merges",
            "Cannot edit .github/workflows, package.json deps, build.gradle, or *.lock files",
            "Cannot delete files or force-push",
            "$2 hard budget cap per run",
            "Per-user concurrent limit: 1 active task at a time",
            "Refuses with JARVIS_FIX_REFUSED=insufficient_brief when the Jira ticket lacks a clear symptom or any code pointer (file/class/endpoint/repro/screenshot)",
        ],
        "shipped": "2026-05-13",
    },
    {
        "command": "/jarvis claudify <repo>",
        "name": "Add Project Claudify CLAUDE.md files",
        "category": "docs",
        "summary": (
            "Runs the official `generate_org_claude_docs.py` orchestrator: generates "
            "per-module + root CLAUDE.md files for the repo, opens a draft PR with "
            "the `Claudify` label and cost+duration table in the body."
        ),
        "when_to_use": [
            "A repo doesn't yet have CLAUDE.md and your squad wants AI-tool context",
            "You want the official 5/8-section Project Claudify structure (MCP-parseable tables)",
        ],
        "when_NOT_to_use": [
            "A repo that already has an open `add-claude-md-docs` PR (Jarvis will safety-skip)",
            "A repo where your squad has actively committed engineer-time to write CLAUDE.md manually",
        ],
        "example": "/jarvis claudify card-mandates",
        "cost_range_usd": "0.50-15.00 (depends on module count; multi-module Kotlin/Gradle repos are most expensive)",
        "duration_typical": "3-15 min",
        "scope": "Any repo in jupitermoney/ org (no allowlist — additive markdown only)",
        "limits": [
            "Branch fixed to `add-claude-md-docs`; PR label fixed to `Claudify`",
            "Skips if open Claudify PR or branch already exists on remote",
            "Per-user daily-cap: $100/day org-wide claudify spend (configurable)",
        ],
        "shipped": "2026-05-14",
    },
    {
        "command": "/jarvis review <PR-URL> | /jarvis review <repo>#<num>",
        "name": "Cross-repo PR impact review",
        "category": "code-review",
        "summary": (
            "Posts ONE review comment on a PR with cross-repo impact analysis. Searches "
            "all 261 indexed repos for consumers of changed symbols (OpenAPI paths, Kotlin "
            "classes/enums, Kafka topics, Stargate routes, gRPC services). Cites file:line "
            "for every claim. THIS IS THE JARVIS-SPECIFIC DIFFERENTIATION — Cursor / Copilot "
            "/ CodeRabbit only see the PR's own repo."
        ),
        "when_to_use": [
            "PR touches a shared library (platform-models, platform-commons, *-spi)",
            "PR changes an OpenAPI spec, gRPC service, Kafka topic, or Stargate route",
            "You're reviewing someone else's contract-surface PR and want cross-team impact",
            "Sanity check before merging anything that other repos consume",
            "Security review of cross-service contract changes (auth filters, headers, etc.)",
        ],
        "when_NOT_to_use": [
            "Trivial PRs (typo fixes, comment-only diffs)",
            "Pure intra-service refactors with no public-contract changes",
            "'Is this code well-written' style review — use Cursor / Copilot / human review for that",
            "SAST/security scanning — use Semgrep / Snyk / Bandit (Jarvis is reasoning-only)",
        ],
        "example": "/jarvis review gateway#8027",
        "cost_range_usd": "0.20-0.50",
        "duration_typical": "30-90s",
        "scope": "All 261 indexed jupitermoney repos · cross-repo consumer detection · cited file:line",
        "limits": [
            "Indexed repos only (~115 of 376 not visible)",
            "Doesn't run tests or build affected repos",
            "ONE summary comment per invocation (no inline diff comments yet — v2)",
            "Opt-in only via slash command (no auto-trigger on every PR yet — v2)",
            "Per-user concurrent limit shared with fix/claudify: 1 active at a time",
        ],
        "shipped": "2026-05-15",
    },
    {
        "command": "/jarvis nitpick <PR-URL>",
        "name": "Kotlin/Java intra-repo correctness review",
        "category": "code-review",
        "summary": (
            "Posts a GitHub review (with inline comments + REQUEST_CHANGES/COMMENT state) "
            "applying the Jupiter Kotlin checklist: null safety (`!!` blockers), JOOQ/JPA "
            "patterns, Temporal workflow rules, BOM version overrides, Java 21 migration checks, "
            "architecture conventions from CLAUDE.md. Complements `/jarvis review` which handles "
            "cross-repo impact."
        ),
        "when_to_use": [
            "PR touches Kotlin/Java source in any jupitermoney repo",
            "You want intra-repo correctness: null safety, ORM patterns, Temporal rules",
            "Pair with /jarvis review for full coverage (cross-repo + intra-repo)",
        ],
        "when_NOT_to_use": [
            "Pure infra / YAML / config-only PRs with no Kotlin/Java changes",
            "Cross-repo impact analysis — use /jarvis review for that",
        ],
        "example": "/jarvis nitpick https://github.com/jupitermoney/p2p-custodian/pull/316",
        "cost_range_usd": "0.05-0.25",
        "duration_typical": "30-90s",
        "scope": "Any jupitermoney repo · uses CLAUDE.md conventions + Jarvis service contract index",
        "limits": [
            "Diff truncated at 10k chars for very large PRs",
            "Gradle dep check requires ARTIFACTORY_USER + ARTIFACTORY_PASSWORD in env",
            "Per-user concurrent limit shared with fix/claudify/review: 1 active at a time",
        ],
        "shipped": "2026-05-20",
    },
    {
        "command": "/jarvis refresh <space>",
        "name": "Refresh a Confluence space's index in Jove",
        "category": "qa",
        "summary": (
            "Trigger Jove to re-pull all pages of a Confluence space from the live Confluence "
            "API and re-embed them. Use when a Confluence page was edited recently and you want "
            "Jove's answers to reflect the change. Friendly names work (Technology / tech / TECH "
            "all resolve to TECH). Time scales linearly: ~1 second per page, so ~74 minutes for "
            "TECH (4439 pages), ~24 seconds for GrowthX (24 pages). For small spaces Jarvis shows "
            "inline progress; for big spaces Jarvis returns immediately and DMs you on milestones "
            "+ on completion."
        ),
        "when_to_use": [
            "Just edited a Confluence page and want Jove to pick up the change",
            "Suspect Jove's answer is stale relative to the latest doc state",
            "Want to warm the index before a deep investigation",
        ],
        "when_NOT_to_use": [
            "If you just want to ask a question — try `/jarvis ask <space>: <question>` first; only refresh if the answer is clearly stale",
            "Don't trigger refresh of TECH/PROD/DS casually — each takes 60+ minutes",
            "Indexer runs ONE job at a time — you'll be told if someone else's refresh is in flight",
        ],
        "example": "/jarvis refresh GrowthX  (or  /jarvis refresh Technology)",
        "cost_range_usd": "0 (Jove handles the embedding cost; no Anthropic spend on Jarvis side)",
        "duration_typical": "~1 sec per page (small spaces seconds, big spaces 60+ min)",
        "scope": "Per-space refresh; 108 spaces available",
        "limits": [
            "Indexer is single-task; concurrent requests get 'already_running' response",
            "Hard timeout 90 min on Jarvis polling (covers TECH with margin)",
            "Doesn't run synchronously inside /jarvis ask — separate command (intentional latency contract)",
        ],
        "shipped": "2026-05-15",
    },
    {
        "command": "/jarvis ask <space>: <question>  (friendly names + optional `refresh` modifier)",
        "name": "Confluence-scoped Q&A via Jove",
        "category": "qa",
        "summary": (
            "Ask Jove (the Jupiter product-expert agent) any question scoped to a specific "
            "Confluence space (TECH, PROD, DS, etc.). Engineer specifies the space; Jarvis "
            "validates it against Jove's index (108 spaces, 23k+ pages) and routes the call. "
            "Use `refresh` modifier to force Jove to re-fetch from live Confluence if its "
            "index is stale relative to recent edits."
        ),
        "when_to_use": [
            "Product / architecture / RFC / runbook / spec questions where the answer lives in Confluence",
            "When you know which Confluence space to look in (TECH for engineering, PROD for product, DS for data science, etc.)",
            "Cross-checking code intent vs documented design (combine with /jarvis Q&A on code)",
            "Looking up existing ADRs, decisions, post-mortems",
        ],
        "when_NOT_to_use": [
            "Pure code questions — use plain `/jarvis <question>` instead, which already searches the 261 indexed repos",
            "When you don't know which space to look in — Jove search isn't a global Confluence search; you must scope",
            "Questions about live production state (Confluence is documentation, not state)",
        ],
        "example": "/jarvis ask TECH: what's our standard auth pattern for new microservices?",
        "cost_range_usd": "Variable — Jove handles its own LLM cost; Jarvis pays only the orchestration overhead (~$0.00). Jove typically charges $0.10-0.50 per query depending on agent iterations.",
        "duration_typical": "20-90s",
        "scope": "108 indexed Confluence spaces (TECH, PROD, DS, PM, CE, CS, etc.) — top 6 cover ~14k pages",
        "limits": [
            "Engineer must specify the space-key — Jarvis won't auto-detect",
            "Read-only — Jove cannot edit/create Confluence pages",
            "Jove's index has a freshness lag (typically <24h); use `refresh` if the page was edited recently",
            "Per-user concurrent task limit (shared with fix/claudify/review): 1 active at a time",
        ],
        "shipped": "2026-05-15",
    },
    {
        "command": "/jarvis investigate <alert-description-or-opsgenie-url-or-id>",
        "name": "Production-incident investigation (with OpsGenie context)",
        "category": "qa",
        "summary": (
            "Slack-callable structured runbook for production alerts and customer-reported "
            "issues. Returns: source (which repo+file emits the metric), complete code flow, "
            "log statements with format strings (for Kibana grep), and ranked failure modes "
            "with concrete investigation commands. Accepts free-form text OR a real OpsGenie "
            "alert/incident URL or UUID — when given an OpsGenie reference, pre-fetches the "
            "alert/incident detail (message, priority, status, responders, recent notes) live "
            "from OpsGenie and inlines that real context before running the code investigation. "
            "Same reasoning Sumith's SRE bot already gets via the HTTP API."
        ),
        "when_to_use": [
            "Prometheus / Grafana / OpsGenie alert just fired — paste the OpsGenie URL",
            "Active SEV-1/2 incident — paste the OpsGenie incident URL for unified context + code analysis",
            "Customer reports an issue and you need to identify code paths involved",
            "On-call debugging at 3am — you want a runbook, not a code-search session",
            "p99 latency spiking, error rate climbing, success rate dropping — anything alert-like",
        ],
        "when_NOT_to_use": [
            "Pure Q&A about how something works — use `/jarvis <question>` instead",
            "Investigating live production state — no live DB / metrics / log access; OpsGenie "
            "context gives alert state + responder timeline only, not live metrics",
            "When the alert is about a service Jarvis doesn't index (~115 of 376 unindexed)",
        ],
        "example": (
            "/jarvis investigate https://jupitermoney.app.opsgenie.com/incident/detail/<uuid>  "
            "OR  /jarvis investigate CMS New Card success rate less than 90 percent"
        ),
        "cost_range_usd": "0.30-1.00 (depends on agent iterations to find the right entry point)",
        "duration_typical": "30-90s (+ ~0.5s for OpsGenie fetch if URL/ID provided)",
        "scope": "All 261 indexed jupitermoney repos · OpsGenie alerts + incidents (read-only)",
        "limits": [
            "Static code-level reasoning only — no live DB / metrics / log access",
            "OpsGenie integration is read-only — cannot ack, mute, route, or close alerts",
            "Indexed repos only — alerts about unindexed services are guesswork",
            "OpsGenie fetch is best-effort: if API is down or alert not found, falls back to free-form mode",
        ],
        "shipped": "2026-05-15 (code investigation) · 2026-05-17 (OpsGenie context)",
    },
    {
        "command": "n/a (HTTP API at http://3.6.202.121:8081)",
        "name": "HTTP API for SRE bot integration",
        "category": "infra",
        "summary": (
            "Programmatic interface for internal automations (currently used by Sumith's "
            "SRE bot for production alert analysis)."
        ),
        "when_to_use": [
            "Building an internal automation that needs Jarvis-quality answers",
            "Production alert webhooks (Prometheus, PagerDuty, etc.)",
        ],
        "when_NOT_to_use": [
            "Anything human-interactive — use the Slack /jarvis command instead",
        ],
        "example": "POST /api/v1/alert-analysis with body {alert_name, service, extra_context}",
        "cost_range_usd": "0.10-2.00 per request (depends on agent iterations)",
        "duration_typical": "30-120s",
        "scope": "Same agent + same indexed corpus as Slack /jarvis",
        "limits": [
            "Bearer token auth required (JARVIS_API_KEY)",
            "No rate limit yet — add if abused",
            "Audit log at ~/jarvis/logs/api_requests.jsonl",
        ],
        "shipped": "2026-05-13",
    },
    {
        "command": "POST /api/v1/plan/stream",
        "name": "Repository-grounded implementation-plan API",
        "category": "infra",
        "summary": (
            "Standalone SSE planning agent for coding clients such as Merlin. It turns "
            "versioned chat context into a structured, file-level implementation plan and "
            "discovers the owning repository itself."
        ),
        "when_to_use": [
            "A coding UI needs a plan before applying changes",
            "The task needs complete data lineage or multiple domain branches verified",
            "A plan starts mid-chat and needs the current conversation context carried forward",
            "Feedback should refine a previous plan instead of starting an unrelated investigation",
        ],
        "when_NOT_to_use": [
            "General codebase questions — use POST /api/v1/ask or Slack /jarvis",
            "A task that requires applying code changes — plan mode is read-only",
        ],
        "example": (
            'POST /api/v1/plan/stream with '
            '{"intent":"create","sessionSummary":"…",'
            '"currentPrompt":"Add a repayment breakdown API"}'
        ),
        "cost_range_usd": "$0.25-5.00; caller may lower maxCostUsd but never raise it above $5",
        "duration_typical": "2-5 min for broad plans; source collection is parallel and adaptive research is bounded",
        "scope": "Jarvis resolves scope through retrieval, then verifies every reported repo and file against source evidence",
        "limits": [
            "Bearer authentication required",
            "Read-only plan generation; cannot compile, test, or write files",
            "Returns SSE, not a synchronous JSON response",
            "The caller must provide the current planning prompt; session summary, recent turns, prior plan, and feedback are optional",
            "Conversation context informs intent only; code facts are always revalidated through retrieval",
            "Returns source-backed file evidence, requirement coverage, and API-parameter attribution",
            "Hard plan budget is $5.00; maxCostUsd may only lower it",
        ],
        "shipped": "2026-07-31",
    },
    {
        "command": "POST /api/v1/fix  (async)",
        "name": "HTTP fix-mode endpoint — programmatic draft-PR creation",
        "category": "infra",
        "summary": (
            "HTTP-callable equivalent of Slack `/jarvis fix`. Takes a repo + free-form "
            "instructions, spawns the same jarvis_fix.sh that Slack uses, returns an "
            "async job_id immediately; caller polls GET /api/v1/fix/{job_id} for the "
            "draft PR URL. Built for JPE/Jove so PRD-driven mobile/backend implementations "
            "can produce real PRs without copy-paste."
        ),
        "when_to_use": [
            "JPE/Jove orchestrator turning a PRD into a concrete draft PR",
            "Any internal automation that needs a programmatic draft-PR surface",
        ],
        "when_NOT_to_use": [
            "Human-interactive fix requests — use Slack `/jarvis fix` instead",
            "CLAUDE.md doc generation — that is a different code path (jarvis_claudify.sh, "
            "Slack-only today)",
        ],
        "example": (
            "POST /api/v1/fix with body {repo, description, max_budget_usd, "
            "attachments?, companion_pr?, regression_test?}, header X-Jarvis-Caller: jove "
            "→ 202 + job_id → poll GET /api/v1/fix/{job_id}. Phase 3 (2026-05-19) added "
            "attachments + companion_pr; Phase B (2026-05-20) added regression_test (TDD mode)."
        ),
        "cost_range_usd": "0.50-5.00 per run (per-request hard cap via max_budget_usd; HTTP ceiling $5)",
        "duration_typical": "120-600s",
        "scope": "Repos on JARVIS_WRITE_ALLOWED_REPOS only · uses Claude Code headless under the hood",
        "limits": [
            "Bearer token auth (same JARVIS_API_KEY as /ask)",
            "max_budget_usd default 2.00, HTTP ceiling $5.00 (env JARVIS_FIX_HTTP_BUDGET_CAP_USD)",
            "Global concurrent-jobs cap of 3 (env JARVIS_FIX_HTTP_CONCURRENCY)",
            "Hard timeout 900s per job (env JARVIS_FIX_HTTP_TIMEOUT_SEC)",
            "In-memory job store — lost on jarvis-api restart; callers should re-submit",
            "Audit log at ~/jarvis/logs/fix_audit.jsonl with source=http_api + caller",
            "Brief-sufficiency gate (Haiku) refuses with JARVIS_FIX_REFUSED=insufficient_brief + structured missing:[] when the Jira ticket is too thin to act on; override with JARVIS_SKIP_BRIEF_GATE=1",
        ],
        "shipped": "2026-05-17",
    },
    {
        "command": "POST /api/v1/pr/iterate  (async)",
        "name": "HTTP iterate-on-PR endpoint — address review comments on a Jarvis-opened PR",
        "category": "infra",
        "summary": (
            "HTTP-callable. Given a {repo, pr_number}, Jarvis fetches the PR's "
            "review comments via gh api, clones a fresh checkout of the PR's "
            "branch, runs Claude to address each comment, commits + pushes "
            "(NEVER force-pushes) so the existing draft PR auto-updates. Built "
            "for Jove/JPE so review-comment iteration loops are automated, not "
            "manually orchestrated. Same async + callback + idempotency surface "
            "as /api/v1/fix."
        ),
        "when_to_use": [
            "A Jove-opened draft PR has received review comments (standards-bot OR human)",
            "Want the same agent that opened the PR to address its own comments",
        ],
        "when_NOT_to_use": [
            "PR opened by a human (Jarvis-iterate assumes the branch belongs to Jarvis)",
            "Review comments require architectural decisions a human should make (iterate "
            "is for mechanical / pattern-matching fixes; major redesigns belong to humans)",
            "PR is closed or from a fork (head must be on jupitermoney/<repo>)",
        ],
        "example": (
            "POST /api/v1/pr/iterate body {repo: 'jupiter', pr_number: 14141, max_budget_usd: 3}, "
            "header X-Jarvis-Caller: jove → 202 + job_id → poll GET /api/v1/pr/iterate/{job_id}"
        ),
        "cost_range_usd": "0.30-3.00 per run (smaller than /api/v1/fix; fewer comments = cheaper)",
        "duration_typical": "180-600s",
        "scope": "Repos on JARVIS_WRITE_ALLOWED_REPOS; PR must be open + on a non-fork branch",
        "limits": [
            "Always fresh-clones the branch (no workspace reuse) — picks up any human pushes",
            "Never force-pushes or rebases (preserves reviewer's diff history)",
            "If no comments / reviews exist, aborts cleanly (nothing to iterate)",
            "Same auth + budget + concurrency caps as /api/v1/fix",
            "Audit log at ~/jarvis/logs/iterate_audit.jsonl with source=http_api + caller",
        ],
        "shipped": "2026-05-19",
    },

    {
        "command": "POST /api/v1/preflight  (sync)",
        "name": "Local pre-push PR review — engineer-facing CLI",
        "category": "code-review",
        "summary": (
            "Synchronous endpoint. Takes a unified diff (no PR exists yet) and "
            "returns structured JSON findings: severity-graded (critical/high/medium/low/info), "
            "categorised (cross_repo_breakage / parallel_drift / ci_security / breaking_change / "
            "config_propagation / standards / deploy_risk / missing_tests), each with "
            "file:line citations. Designed to be called by `bin/jarvis-preflight` from "
            "an engineer's local machine via SSH tunnel before `git push`."
        ),
        "when_to_use": [
            "Local pre-push gate (called by the bin/jarvis-preflight CLI)",
            "CI pipeline early gate (--no-color --json mode)",
            "Any diff that touches contract surface, design-system tokens, or .github/workflows/",
        ],
        "when_NOT_to_use": [
            "Already-pushed PRs — use /jarvis review for those",
            "Pure intra-repo style review (Cursor / Copilot handle that)",
            "Validation that the code BUILDS or tests pass (preflight is reasoning-only)",
        ],
        "example": (
            "POST /api/v1/preflight body {repo: 'jupiter', diff: '<unified-diff>', requester: 'eng@team'}, "
            "header Authorization: Bearer JARVIS_API_KEY → 200 with PreflightResponse"
        ),
        "cost_range_usd": "0.01-0.20 per run (smaller than /jarvis review since output is structured-only)",
        "duration_typical": "5-30s for diffs <500 lines",
        "scope": "All 261 indexed repos · structured findings · NO writes to GitHub",
        "limits": [
            "Indexed repos only (~115 of 376 not visible)",
            "Bearer-only auth (shared JARVIS_API_KEY for now; per-engineer keys deferred)",
            "Same agent loop as /jarvis review — doesn't run tests or build",
            "Returns findings; does NOT post to GitHub (engineer hasn't pushed)",
        ],
        "shipped": "2026-05-20",
    },
    {
        "command": "bin/jarvis-preflight  (CLI)",
        "name": "Local CLI wrapping POST /api/v1/preflight",
        "category": "code-review",
        "summary": (
            "Shell script in jupitermoney/jarvis bin/. Engineers install it on their "
            "local machine and run `jarvis-preflight` inside a repo before `git push`. "
            "Auto-detects repo from origin remote, base branch from main/master/develop, "
            "captures `git diff origin/<base>...HEAD`, POSTs to /api/v1/preflight, "
            "renders findings inline with terminal colours + ANSI severity tags. Exit "
            "code reflects highest severity (0/1/2) so it's CI-friendly."
        ),
        "when_to_use": [
            "Before `git push` on any non-trivial PR — catches contract / drift issues in <30s",
            "Inside CI as an early gate before standards-bot",
        ],
        "when_NOT_to_use": [
            "When you have no network access to the box (requires SSH tunnel to jarvis-api:8081)",
            "On generated code / lock-file-only diffs (the agent will likely return zero findings + LOW)",
        ],
        "example": (
            "jarvis-preflight                  # diff vs origin/main, inline coloured output\n"
            "jarvis-preflight --base develop   # explicit base branch\n"
            "jarvis-preflight --json           # raw JSON for scripting\n"
            "jarvis-preflight --no-color       # plain text for CI logs"
        ),
        "cost_range_usd": "same as /api/v1/preflight (0.01-0.20)",
        "duration_typical": "5-30s total round-trip",
        "scope": "Same as /api/v1/preflight — no writes, structured findings",
        "limits": [
            "Requires SSH tunnel: `ssh -L 8081:localhost:8081 ubuntu@3.6.202.121`",
            "First run prompts for endpoint + bearer; stored in ~/.jarvis/config (chmod 600)",
            "Only resolves repos under jupitermoney/<name> from the origin remote",
        ],
        "shipped": "2026-05-20",
    },
    {
        "command": "n/a (MCP server — stdio + streamable-http transports)",
        "name": "jarvis-mcp — Jarvis as MCP tools for local Claude Code / Cursor / Claude Desktop / remote bots",
        "category": "qa",
        "summary": (
            "MCP server exposing 15 tools: 10 read-only retrieval (search_code, read_file, "
            "search_prs, git_history, list_repo_files, list_indexed_repos, find_repo, "
            "fetch_jira_ticket, fetch_pr_diff, get_capabilities) PLUS 5 trigger-shim tools "
            "(fire_fix, fire_iterate, fire_preflight, get_fix_status, get_iterate_status) "
            "that POST to the existing HTTP API on :8081. Two transports: stdio (per-session, "
            "spawned via SSH by local MCP clients) and streamable-http on 127.0.0.1:8082 "
            "(long-running systemd service, Bearer-auth, intended for tunneled or in-process "
            "MCP clients). All 3 phases of the hybrid MCP+HTTP architecture now shipped."
        ),
        "when_to_use": [
            "Engineer wants cross-repo search/read AND/OR fix/iterate/preflight triggers from inside their local Claude Code / Cursor / Claude Desktop session",
            "Bot or in-process agent wants programmatic access to Jarvis without going through Slack",
            "Use alongside per-repo CLAUDE.md — local CLAUDE.md describes the repo, jarvis-mcp gives cross-repo brain + write triggers",
        ],
        "when_NOT_to_use": [
            "Slack-driven flows — use /jarvis in Slack instead",
            "Jove → /api/v1/fix integration — keep using the existing HTTP API (no migration needed)",
            "Write triggers without an Idempotency-Key — fire_fix and fire_iterate REFUSE without one (prevents duplicate spend)",
        ],
        "example": (
            "STDIO (local MCP client config — Claude Desktop / Cursor / Claude Code):\n"
            "  mcpServers: {\n"
            "    jarvis: {\n"
            "      command: ssh,\n"
            "      args: [\"-i\", \"~/Downloads/data-science.pem\",\n"
            "             \"ubuntu@3.6.202.121\",\n"
            "             \"/home/ubuntu/jarvis/scripts/run_mcp_server.sh\"]\n"
            "    }\n"
            "  }\n"
            "\n"
            "HTTP (tunneled — same client, different transport):\n"
            "  1) ssh -L 8082:localhost:8082 ubuntu@3.6.202.121\n"
            "  2) mcpServers: { jarvis: { url: 'http://localhost:8082/mcp/',\n"
            "                              headers: { Authorization: 'Bearer $JARVIS_API_KEY' } } }"
        ),
        "cost_range_usd": (
            "Retrieval tools: $0 directly (Jarvis side); trigger tools: same as the underlying "
            "HTTP API endpoint they shim (fire_fix $0.50-2.00, fire_iterate $0.30-2.00, "
            "fire_preflight $0.01-0.20)"
        ),
        "duration_typical": (
            "Retrieval: <10ms cache / 200-500ms semantic. Trigger tools: <100ms to return job_id "
            "(fix/iterate are async); fire_preflight 5-30s (sync)."
        ),
        "scope": (
            "Retrieval — full indexed corpus (261 repos + 8.4k PR descriptions). "
            "Writes — same JARVIS_WRITE_ALLOWED_REPOS allowlist as HTTP API "
            "(bff-core, jupiter, jarvis, jupiter-design-system)."
        ),
        "limits": [
            "HTTP transport bound to 127.0.0.1 only — engineers tunnel via SSH. Flip to 0.0.0.0 when ready to expose to remote bots.",
            "fire_fix and fire_iterate REFUSE without a non-empty idempotency_key (prevents double-spend; see project_http_fix_endpoint.md).",
            "Trigger tools never bypass the HTTP API — they POST to localhost:8081 so allowlist/budget/audit all flow through the same code path.",
            "Audit log at ~/jarvis/logs/mcp_audit.jsonl (separate from qa_log.jsonl / api_requests.jsonl / fix_audit.jsonl).",
            "Smoke tests: scripts/smoke_mcp.py (stdio) + scripts/smoke_mcp_http.py (HTTP). Run after any edit to jarvis_mcp/.",
        ],
        "shipped": "2026-05-21",
    },
    {
        "command": "n/a (background)",
        "name": "Spend monitor + cost safety net",
        "category": "infra",
        "summary": (
            "Background systemd timer (every 15min) + post-run hook in the claudify wrapper. "
            "Auto-DMs Rohit on Anthropic credit failures + threshold crossings. Daily cap "
            "of $100/day claudify spend enforced in the wrapper."
        ),
        "when_to_use": ["n/a — runs automatically, no user-facing surface"],
        "when_NOT_to_use": ["n/a"],
        "example": "n/a",
        "cost_range_usd": "0 (only Slack DMs)",
        "duration_typical": "<1s per check",
        "scope": "All claudify runs + qa_log + api_requests",
        "limits": [
            "Detects 'Credit balance is too low' string in claudify run logs",
            "Threshold alerts at $20/$40/$60/$100 (one DM per threshold per IST day)",
            "Daily cap: $100/day org-wide claudify spend (rejects new runs cleanly past cap)",
        ],
        "shipped": "2026-05-14",
    },
    {
        "command": "n/a (background, daily 03:00 UTC)",
        "name": "Jove Confluence refresh + drift monitor",
        "category": "infra",
        "summary": (
            "Nightly systemd timer that triggers Jove to re-crawl critical Confluence "
            "spaces (TECH + PROD by default; override via JARVIS_JOVE_REFRESH_SPACES env), "
            "then checks index freshness. If any refresh fails or a space's latest_modified "
            "is more than 48h ahead of Jove's latest_indexed_at, DMs Rohit. Belt-and-"
            "suspenders on top of Jove's own 02:00 UTC crawler, which has been silently "
            "skipping large spaces (TECH was 3 months stale before this shipped)."
        ),
        "when_to_use": ["n/a — runs automatically at 03:00 UTC daily"],
        "when_NOT_to_use": ["n/a"],
        "example": "n/a  (manual dry-run: JARVIS_JOVE_REFRESH_SPACES=BP ./run_jove_scheduled_refresh.sh)",
        "cost_range_usd": "0 (Jove-owned indexing infra)",
        "duration_typical": "~1-2h per full run (TECH dominates at ~4500 pages)",
        "scope": "Confluence spaces named in JARVIS_JOVE_REFRESH_SPACES (default TECH,PROD)",
        "limits": [
            "Sequential per-space refresh — Jove has a global indexer lock so parallelism would fail",
            "Retries up to 30 min per space if Jove returns 'already_running' (busy)",
            "Per-space timeout: 30 min after acquiring the run_id",
            "Drift alert threshold: 48h between latest_modified and latest_indexed_at",
            "Alerts route via JARVIS_ALERTS_CHANNEL if set, else DM Rohit",
            "Audit log: ~/jarvis/logs/jove_refresh.jsonl (append-only, per-event JSONL)",
        ],
        "shipped": "2026-07-05",
    },
    {
        "command": "/jarvis migrate <repo1>,<repo2>,...: <task>  (also: POST /api/v1/migrate, MCP jarvis_fire_migrate)",
        "name": "Migrate — same task across N repos, one approval",
        "category": "code-edit",
        "summary": (
            "Cross-repo rollout: apply the same task across a list of Jupiter repos, each "
            "producing its own draft PR. Wraps fix-mode mechanics under a single approval, "
            "combined budget, and one summary message. Specifically built for JFrog→GHP "
            "migrations, dependency bumps, CI workflow updates, security patches, codemods."
        ),
        "when_to_use": [
            "Cross-repo rollout where the SAME change applies uniformly (no per-repo nuance)",
            "JFrog/registry migrations, dep version bumps, CI YAML rewrites, license header adds",
            "When the operator wants ONE approval + ONE budget + ONE result summary across N repos",
        ],
        "when_NOT_to_use": [
            "Single-repo bug fix → use /jarvis fix (cheaper, simpler)",
            "Iterating on a Jarvis-opened PR → use /jarvis iterate (already exists)",
            "Repos that need per-repo nuanced changes — migrate applies the same task to each",
        ],
        "example": "/jarvis migrate bff-core,jupiter: bump @types/node to ^22.0.0",
        "cost_range_usd": "0.50-1.50 per repo (per-repo cap 5.00; default 1.50). Total batch cap 100.",
        "duration_typical": "1-3 min per repo, serial",
        "scope": (
            "Repos must appear in JARVIS_MIGRATE_ALLOWED_REPOS (separate from fix's allowlist — "
            "operator-curated, broader scope). Initial value: jarvis only; expand as use cases "
            "land. Each child run further restricts JARVIS_WRITE_ALLOWED_REPOS to just its own "
            "repo, defense-in-depth."
        ),
        "limits": [
            "Always opens DRAFT PRs — never auto-merges (inherited from fix-mode)",
            "Per-repo budget hard cap 5.00 USD (default 1.50). Total batch cap 100.00 USD.",
            "Serial execution — one repo at a time (v1). Parallel may come if real throughput "
            "needs demand it.",
            "One failure does NOT abort the batch by default — pass stop_on_failure for strict mode",
            "Per-user concurrency lock shared with fix/iterate/claudify (one writable task per "
            "user at a time)",
            "Idempotency-Key header (HTTP) / argument (MCP) prevents double-spend on retries",
            "Audit log at ~/jarvis/logs/migrate_audit.jsonl",
            "MCP tool jarvis_fire_migrate is the recommended surface for in-editor users; "
            "Slack /jarvis migrate posts a final summary (no live progress streaming v1)",
        ],
        "shipped": "2026-06-03",
    },
    {
        "command": "n/a (background, daily 04:00 UTC / 09:30 IST)",
        "name": "Open-PR aging digest → #technology-team",
        "category": "infra",
        "summary": (
            "Daily Slack digest of open PRs across all jupitermoney/* repos, grouped "
            "by product (Cards, Lending, Cards, Investments, etc.) with stuck-count + "
            "stuck-rate per product. Surfaces the top 10 stale repos and top 20 oldest "
            "stuck PRs. Excludes Jarvis-fired PRs and bot-authored PRs."
        ),
        "when_to_use": ["n/a — runs automatically"],
        "when_NOT_to_use": ["n/a"],
        "example": "n/a (autoposted to #technology-team daily)",
        "cost_range_usd": "0 (gh API + Slack only — no LLM calls)",
        "duration_typical": "~30s end-to-end",
        "scope": (
            "All ~370 jupitermoney/* repos via REST search/issues (paginated). "
            "Repo → product mapping in scripts/repo_products.json — operator-curated, "
            "evolves over time via the unmapped-repos appendix in each digest."
        ),
        "limits": [
            "Public version omits individual-author rankings (privacy + culture concern); "
            "PR links + repo names only.",
            "GitHub REST search caps at 1000 results; if Jupiter ever ships >1000 open PRs the "
            "tail will be undercounted (today: 838).",
            "Bot-authored PRs (dependabot etc.) excluded from public aggregates.",
            "Jarvis-fired PRs excluded — they're tool output, not engineer work signal.",
            "Snapshot at ~/jarvis/index/open_pr_snapshot.json rolls forward on successful post "
            "to enable day-over-day deltas in future digests.",
        ],
        "shipped": "2026-06-02",
    },
    {
        "command": "n/a (agent + MCP tool: jarvis_lookup_service + HTTP: GET /api/v1/services/{name})",
        "name": "Service-discovery registry lookup",
        "category": "qa",
        "summary": (
            "Sub-second lookup of any Jupiter microservice (~200 services) by canonical "
            "name, alias, or human name. Returns K8s in-cluster URL, Route53 cross-cluster "
            "URL, namespace, port, source repo, OpenAPI spec file, exposed paths, consumer "
            "repos, sample Feign client. Built nightly by build_service_registry.py from "
            "K8s + Route53 grep across all cloned repos, with operator-curated overrides "
            "for cross-cluster URLs that aren't hardcoded in code."
        ),
        "when_to_use": [
            "'Where is service X deployed' / 'what's the URL for Y' / 'what port'",
            "'What endpoints does Z expose' (returns the canonical OpenAPI paths)",
            "'Who calls W' (returns the list of consumer repos)",
            "Cross-cluster routing questions — registry has Route53 *.lending-v2prod.internal and *.investmentprod.internal where applicable",
            "Acronym + alias resolution (LLM → lending-lifecycle-manager-ms, bullet → bullet-ms)",
        ],
        "when_NOT_to_use": [
            "Live deployment state (status, replica count, recent rollouts) — registry is code-grep, not the K8s API",
            "Services whose URLs are NOT hardcoded in any indexed repo (rare; usually a brand-new service awaiting nightly rebuild + override)",
        ],
        "example": "Where is bullet-ms deployed and what HTTP paths does it expose?",
        "cost_range_usd": "0.04-0.10 (one agent tool call)",
        "duration_typical": "<1s for the lookup itself; agent end-to-end 20-40s",
        "scope": "All services discoverable via K8s URL (*.svc.cluster.local) or Route53 URL (*.internal) grep across the ~250 cloned repos, plus operator overrides in scripts/service_registry_overrides.json",
        "limits": [
            "Registry refreshes nightly (Phase 3 — not yet wired into reindex_all.sh; manual rebuild via scripts/build_service_registry.py for now)",
            "Cross-cluster URLs not in any code (e.g. confirmed only over Slack) need an explicit overrides entry",
            "Stale Feign clients on the consumer side may declare paths the server no longer exposes — the registry pulls from the SERVER-side OpenAPI spec, which is authoritative",
        ],
        "shipped": "2026-05-31",
    },
    {
        "command": "n/a",
        "name": "Developer Portal (per-engineer API keys + admin dashboard)",
        "category": "meta",
        "summary": (
            "Self-service web portal where @jupiter.money engineers sign in with Google, "
            "generate per-engineer jrv_ API keys for IDE/MCP integration, and admins track "
            "per-user usage and grant write access."
        ),
        "when_to_use": [
            "When an engineer wants to use Jarvis from their local IDE (Claude Desktop / Cursor / Claude Code)",
            "When generating or rotating a personal Jarvis API key",
            "When Rohit needs to review per-engineer usage or grant write access to an engineer",
        ],
        "when_NOT_to_use": [
            "For Slack-based /jarvis commands — those need no key",
            "For SRE bot / JPE / Jove integrations — they use the shared JARVIS_API_KEY",
        ],
        "example": "ssh -L 8083:localhost:8083 ubuntu@3.6.202.121  →  open http://localhost:8083",
        "cost_range_usd": "0",
        "duration_typical": "<1s (portal UI)",
        "scope": "Portal runs on the Jarvis box; engineers tunnel via SSH on port 8083",
        "limits": [
            "SSH tunnel required (127.0.0.1:8083 only — not exposed to internet)",
            "Only @jupiter.money Google accounts are accepted",
            "New accounts are read-only by default; write access (fire_fix etc.) requires admin approval",
            "Full API key shown once at creation — cannot be recovered, only revoked and regenerated",
        ],
        "shipped": "2026-06-09",
        "shipped_by": "Mithun Tantri",
    },
    {
        "command": "n/a (HTTP API at http://3.6.202.121:8081)",
        "name": "HTTP AutoSupport endpoints — async investigate + sync drift-check",
        "category": "http_api",
        "summary": (
            "Three endpoints designed against Ritheesh Urankar's AutoSupport "
            "(Intelligent Resolution Orchestration Platform) contract. "
            "POST /api/v1/autosupport/investigate is async (returns 202 + "
            "investigation_id, ~85s avg); GET /api/v1/autosupport/investigate/{id} "
            "is the poll fallback (mirrors the callback payload); POST "
            "/api/v1/autosupport/sync is sync drift-check on recommended actions."
        ),
        "example": (
            "POST /api/v1/autosupport/investigate with body "
            "{request_id, channel, user_id, issue_description, callback_url?} "
            "+ optional Idempotency-Key header"
        ),
        "details": [
            "Two-pass: Sonnet agent investigates + retrieves; Haiku 4.5 structurer extracts the strict callback JSON contract from the investigation prose.",
            "Confidence ordinals (findings_confidence / actions_confidence / api_confidence) are derived DETERMINISTICALLY in Python from the evidence enumerated by the agent + cross-checked against the service registry. The LLM does NOT pick the label.",
            "Confidence reasons enum: registry_confirmed (service in registry) > spec_hit (openapi yaml evidence) > multiple_code_hits (>=3 distinct code files) > weak_hit.",
            "auto_executable is hardcoded to false on every recommended_action. AutoSupport + SRE desk decide automation.",
            "database_contexts always carry note=sre_execution_required (no SQL safety variables). SRE manual desk handles all SQL.",
            "Anti-silent-failure: payload validators enforce root_cause_hypothesis >=10 chars + recommended_actions non-empty. On agent failure / unparseable output, Jarvis emits a structurally valid SRE escalation payload (status=FAILED).",
            "Idempotency-Key (any opaque <=128-char string) dedups identical requests within 5min.",
            "callback_url (optional) gets POSTed the terminal callback payload once with X-Jarvis-Investigation-Id + X-Jarvis-Event=investigation.completed|failed headers; 10s timeout, single attempt.",
            "Fallback path always: GET /api/v1/autosupport/investigate/{id} returns the same callback payload once status=COMPLETED|FAILED.",
            "Audit: ~/jarvis/logs/autosupport_audit.jsonl (received/start/completed) + ~/jarvis/logs/autosupport_callbacks.jsonl (delivery attempts).",
        ],
        "shipped": "2026-06-15",
        "shipped_for": "AutoSupport platform (Ritheesh Urankar, SRE team)",
    },
    {
        "command": "/jarvis <question about a specific user>",
        "name": "Investigate handoff — Slack /jarvis routes user-level debugging questions to /api/v1/autosupport/investigate",
        "category": "qa",
        "summary": (
            "When a Slack /jarvis question is shaped like 'why did user U123 fail at "
            "VKYC' / 'trace customer X's onboarding journey' / 'debug user Y's "
            "prefunding failure', Jarvis routes the question to the structured "
            "AutoSupport investigation pipeline instead of the lightweight Q&A agent. "
            "The investigation produces deterministic confidence ordinals + literal "
            "log strings + Loki/DB/Amplitude queries, rendered as Slack blocks for "
            "copy-paste execution."
        ),
        "example": (
            "/jarvis why did user U06BN5VADTN unexpectedly land on VKYC during "
            "onboarding?  →  Jarvis fires /api/v1/autosupport/investigate, posts "
            "structured findings + recommended log/Amplitude queries back to the same "
            "ephemeral Slack message."
        ),
        "details": [
            "Intent classification by Haiku 4.5 (scripts/agent/investigate_intent.py). Returns is_investigate_request=true only at medium+ confidence; false positives downgraded if no user_id detected.",
            "Falls back gracefully to ask() if classifier returns false, low confidence, or fails to fire.",
            "Investigation runs ~90-150s (Sonnet agent + Haiku structurer). User sees a 'Investigating user X…' placeholder during.",
            "Result rendered as Slack blocks (header + hypothesis + per-action queries in code blocks). NOT raw JSON.",
            "Audit: ~/jarvis/logs/investigate_intent.jsonl (every classification) + ~/jarvis/logs/autosupport_audit.jsonl (every fired investigation) + qa_log.jsonl entry marked 'routed_to': 'autosupport_investigate'.",
            "Amplitude lookups are now first-class in the agent prompt — when logs+code cannot answer a 'why did the user end up on screen Y' question, the agent recommends a User Activity API query as a database_context with target_db='amplitude'.",
            "Shipped 2026-06-20 in response to Tushar Chawla feedback (production-debugging UX gap).",
        ],
        "shipped": "2026-06-20",
        "shipped_for": "Tushar Chawla feedback — production user-journey debugging",
    },
    {
        "command": "janus_user_journey (agent tool, MCP, HTTP)",
        "name": "janus_user_journey — Amplitude event stream via Janus",
        "category": "qa",
        "summary": (
            "Inline Amplitude user-event-stream lookup via Janus MCP (stdio, local). "
            "Use for product-event debugging — 'why did user X end up on screen Y', "
            "'how did user Z bypass step W', 'what triggered this state transition' — "
            "questions logs+code cannot answer because the cause is a deeplink, "
            "push notification, A/B variant, marketing campaign, or screen navigation."
        ),
        "example": (
            "janus_user_journey(user_id='3e15d7c9-...', lookback_hours=48, "
            "event_filter=['Deeplink Opened', 'Screen Viewed', 'Push Notification Clicked'], "
            "caller_id='slack:U06BN5VADTN')"
        ),
        "details": [
            "Transport: MCP stdio. Wrapper at /home/ubuntu/test_databricks_connection/run_janus_amplitude_mcp.sh sources Amplitude creds locally; no JANUS_API_BASE / JANUS_SERVICE_TOKEN on Jarvis side.",
            "caller_id is the per-call audit key (Janus-side audit) — Jarvis threads slack:<asker_uid> through.",
            "Defaults: lookback_hours=24, max_events=200. Hard ceiling 168h (7d) — anything more returns error_code=invalid_request.",
            "Returns {user_id, amplitude_user_id, lookback_window, events[], summary, audit_ref} on success, or {error_code, error} on failure (invalid_request, user_not_found, upstream_error).",
            "Timestamps are UTC — agent prompted to convert to IST (+5:30) when surfacing to engineers.",
            "Replaces the prior database_context: amplitude pattern (agent emitting query for caller to execute) for live Slack/HTTP-API answers. Autosupport callbacks still emit database_context for AutoSupport platform execution.",
            "Audit: ~/jarvis/logs/janus_audit.jsonl (user_id_prefix, lookback, event_filter, events_returned, error_code, caller_id).",
            "End-to-end validated 2026-06-24 with synthetic uuid (user_not_found path) + real query + agent-routed call ($0.03, 30s, 1 tool call).",
        ],
        "shipped": "2026-06-24",
        "shipped_for": "Tushar Chawla feedback — production user-journey debugging gap (filled Path B from 2026-06-19 design call)",
    },
    {
        "command": "janus_event_count (agent tool, MCP, HTTP)",
        "name": "janus_event_count — fleet-wide Amplitude event count via Janus",
        "category": "qa",
        "summary": (
            "Fleet-wide count for ONE Amplitude event over a trailing window via Janus MCP. "
            "Use for 'how many users did X in the last N days' simple-count questions "
            "(not multi-step funnels — those still belong to Amplitude dashboards). "
            "Returns unique_users, total_events, a daily series, and optionally top_values[] "
            "when group_by is set."
        ),
        "example": (
            "janus_event_count(event_type='bottom-sheet-viewed', lookback_days=7, "
            "filter_property='action', filter_value='vpn-detected', caller_id='slack:U06BN5VADTN')"
        ),
        "details": [
            "Same MCP/stdio transport as janus_user_journey — no Jarvis-side env required.",
            "filter_property is an EVENT property paired with filter_value. For built-in dimensions like platform / country / app_version, use group_by instead.",
            "Defaults: lookback_days=7, max 90. Beyond 90 currently clamps silently on Janus side (was expected to return invalid_request — flagged to Janus team).",
            "audit_ref is None on event_count responses today (user_journey returns it correctly — flagged to Janus team).",
            "End-to-end validated 2026-06-24: real data returned (8362 unique users, 95480 events for bottom-sheet-viewed in 7d), filter test confirms 0 events when filter_property is wrong (which is exactly when the property-name nudge below should kick in).",
        ],
        "shipped": "2026-06-24",
        "shipped_for": "Tushar Chawla feedback follow-up — fleet-wide count gap (engineer asked for vpn-detected count, bot deflected to dashboard — now answered inline)",
    },
    {
        "command": "@jarvis fix this  /  DM Jarvis 'fix this bug ...'",
        "name": "Conversation-to-fix — extract a brief from a thread/DM and offer a draft PR",
        "category": "code-edit",
        "summary": (
            "Free-text path to fix-mode. Triggered when an allowlisted user either @-mentions "
            "Jarvis in a channel with a fix-shaped phrase (`fix this`, `draft a PR`, `raise a "
            "PR`, etc.) OR DMs Jarvis with the same phrasing. Haiku 4.5 reads the thread "
            "(or single message) and extracts a structured brief: repo, task, file pointers, "
            "confidence, missing_info. Jarvis posts the brief and waits for `go` (or `cancel`, "
            "or an edited task line) before firing the same /api/v1/fix that `/jarvis fix` "
            "would have. Same budget caps, brief-gate, and write-allowlist apply."
        ),
        "when_to_use": [
            "Engineer is already discussing a bug in a thread — wants Jarvis to take over without copy-pasting into /jarvis fix",
            "Engineer wants conversational back-and-forth on the brief BEFORE firing the fix (vs. the all-at-once slash command)",
        ],
        "when_NOT_to_use": [
            "Brief is already a clean Jira ticket — `/jarvis fix <ticket-url>` is one fewer round trip",
            "Bug is in a repo outside JARVIS_WRITE_ALLOWED_REPOS — fix will refuse",
            "FE bug with no images/video — iterate refuses (JARVIS_ITERATE_REFUSED=fe_no_visuals)",
        ],
        "example": "@jarvis fix this  (in a thread describing a bug, with file pointers in earlier messages)",
        "cost_range_usd": "~$0.005 Haiku brief + ~$0.50-$2 if user confirms with `go`",
        "duration_typical": "~3-5s to post brief; ~5min for the fix run on `go`",
        "scope": (
            "Channel @-mentions require channel in JARVIS_ALLOWED_CHANNELS. DM fix-intent "
            "requires user in JARVIS_ALLOWED_DM_USERS. Same write-allowlist as /jarvis fix."
        ),
        "limits": [
            "Single-message DMs work too — no thread required",
            "Pending fix has 10-min TTL; expired pending requests need a fresh trigger",
            "Only the original requester can `go`/`cancel`/edit",
            "Haiku may reject if the regex hit but the message isn't really a fix ask — channel falls through to Q&A, DM posts a nudge toward `/jarvis fix` / `/jarvis <q>`",
        ],
        "shipped": "2026-06-12 (@-mention in channel); 2026-06-25 (DM fix-intent)",
    },
]



# Quick-lookup helpers ──────────────────────────────────────────────

from agent.config import BOT_NAME, SLASH_COMMAND, GITHUB_ORG, COMPANY_NAME

def _format_value(val):
    if isinstance(val, str):
        return val.replace("Jarvis", BOT_NAME).replace("jarvis", BOT_NAME.lower()).replace("/jarvis", SLASH_COMMAND).replace("jupitermoney", GITHUB_ORG).replace("Jupiter", COMPANY_NAME)
    elif isinstance(val, list):
        return [_format_value(item) for item in val]
    elif isinstance(val, dict):
        return {k: _format_value(v) for k, v in val.items()}
    return val

def _get_formatted_capabilities():
    return [_format_value(c) for c in CAPABILITIES]

def list_capabilities(category: str | None = None) -> list[dict]:
    """Return capabilities, optionally filtered by category."""
    caps = _get_formatted_capabilities()
    if category is None:
        return caps
    return [c for c in caps if c.get("category") == category]


def find_capability(query: str) -> list[dict]:
    """Find capabilities by case-insensitive substring match against name/command/summary."""
    q = query.lower()
    caps = _get_formatted_capabilities()
    return [c for c in caps
            if q in c.get("name", "").lower()
            or q in c.get("command", "").lower()
            or q in c.get("summary", "").lower()]


def categories() -> list[str]:
    """List distinct categories."""
    caps = _get_formatted_capabilities()
    return sorted({c.get("category", "?") for c in caps})
