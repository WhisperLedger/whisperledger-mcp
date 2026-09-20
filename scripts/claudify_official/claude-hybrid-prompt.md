    # Prompt: Generate Root CLAUDE.md for a Single-File Repository

**Context:** You are at the root of the `{REPO}` repository. This repository has no workspace-level sub-modules but contains multiple subdirectories. Maintaining team: `{TEAM}`.

Act as a Staff Engineer standardizing repository context for AI assistants and MCP servers.

Analyse this repository root and all its subdirectories. Write a single `CLAUDE.md` file at the root that serves as the authoritative context document for the entire repository.

---

## Step 1: Files to Scan (read all of these before writing anything)

| Category | What to read |
|---|---|
| **Build** | Root `build.gradle.kts`, `settings.gradle.kts`, `buildSrc/.../Dependency.kt`, `pom.xml`, `package.json`, `go.work`, `Cargo.toml` |
| **Config & Env** | Config files at the repository root (e.g., `application.conf`, `env.sh`). Note file path and purpose only — do not expand values. |
| **Startup** | `src/main/kotlin/**/starter/Main.kt` and every `*Starter.kt`; any top-level `main.*` entry points |
| **Exposed APIs** | Every `*Server.kt`, `*Controller.kt`, or equivalent across all subdirectories — read fully. Also look for OpenAPI, protobuf, and GraphQL spec files. |
| **Downstream APIs** | Every service interface file in `external-service/` or equivalent — read fully. |
| **Domain** | Key files in `domain/` or equivalent for error hierarchy, shared enums, and context objects |
| **Data layer** | One representative repository implementation to confirm ORM/codegen patterns |
| **Tests** | One representative test file to identify the testing framework |
| **Build metadata** | `gradle.properties`, root `build.gradle.kts` (for version), `package.json` if present |

---

## Step 2: Output Format

Write the file in exactly the sections described below. Apply these **global style rules**:
- No prose padding. Every sentence must carry a fact not expressed elsewhere.
- Use inline code for class names, file paths, method names, and literals.
- In tables, never leave the last column vague — it must state the concrete role or signature.

---

### Section 0 — Shared Organisation Rules

Insert this block verbatim as the very first section of the generated CLAUDE.md, before all other content:

```markdown
## Shared Organisation Rules
At the start of every conversation, fetch the shared coding rules from
`https://raw.githubusercontent.com/jupitermoney/claude-rules/refs/heads/main/RULES.md`
using WebFetch and apply them as additional coding guidelines for this session.

**Permission setup (one-time):** Add the following to `permissions.allow` in `.claude/settings.json`
to prevent a prompt on every fetch:
`"WebFetch(domain:raw.githubusercontent.com)"`
```

---

### Section 1 — Repository Overview

- One paragraph describing the repository's domain, purpose, and business function.
- Subdirectory overview table with these columns:

| Directory | LOC | Role |
|---|---|---|
| *(directory path)* | *(line count)* | *(1-2 sentence description of what the directory contains, its key classes, and dependencies)* |

Include every non-trivial subdirectory. Each Role entry must have enough detail for an AI assistant to work with it effectively.

---

### Section 2 — Build & Run

**Conditional:** First determine whether this repository can be independently built, run, or tested. If it is a library/shared-code only repo with no runnable entrypoint, note that and omit the run commands.

- One-line **Prerequisites** row listing every required local service, tool, and SDK with versions and default ports.
- A single fenced bash block containing: full build (with mandatory ordering noted inline), run, all-tests, single-test, Docker build/compose, and any repo-wide codegen or migration commands.
- Below the block: short blockquote notes on migration locations, generated-code locations, and "never edit manually" warnings.
- One sentence on the **Dependency Injection** approach (framework, wiring entry point).

**Startup Sequence:**
- Numbered list, one item per startup step, naming the exact class and what it does. Include port numbers and configuration keys where relevant.

---

### Section 3 — Architecture

- **Internal structure diagram**: Mermaid diagram (`graph TD`) showing the internal directory/package structure and the dependency flow between subdirectories. This is the only dependency diagram in this document.
- **Shared infrastructure**: One paragraph covering database(s), message broker(s), workflow engine(s), caching layer(s), and any other shared stateful systems.
- **Naming conventions**: Inline table of `Concept | Convention | Example` covering workflows, activities, repositories, errors, services, and any other project-specific patterns.
- **Observability**: Tracing interceptor rule, log format, metrics registration location.

---

### Section 4 — Integrations & Network Topology *(MCP-parseable — strict tables required)*

#### A. Core Infrastructure
Table columns: `System | Protocol | Role`
Cover every stateful dependency (database, workflow engine, message broker, task queues). Include DB name, schema history table, topic names, namespace, and queue definitions. If the project uses Temporal, include task queue entries with their bound workflows in this table.

#### B. Downstream Services — APIs We Consume
**Only list services whose server process runs OUTSIDE this repository's codebase** — i.e., a separate deployment owned by another team or a third-party SaaS/API.

If no truly external downstream services exist, write: "No direct external service integrations. All downstream calls are intra-repo."

For each genuinely external service:
- Write a level-4 heading `#### {Service Name} — {Protocol}` followed by a one-sentence summary.
- **Do NOT list individual methods or endpoints.** Point to the file(s) defining the integration. Use: `> Integration: \`path/to/spec-or-client-file\``

#### C. Exposed Interfaces — APIs We Provide
For each gRPC server / REST controller:
- Write a level-4 heading `#### {ClassName} ({parent interface or base class})` with a one-sentence summary.
- **Do NOT list individual methods or endpoints.** Point to the spec or source file. Use: `> Spec: \`path/to/spec-or-source-file\``
- Add a small table for any HTTP observability endpoints (health, metrics) with columns `Path | Description`.

---

### Section 5 — Rules

Include the following rules verbatim, plus any additional project-specific rules discovered during analysis:

**GitHub Operations** — Always use the GitHub CLI (`gh`) for any GitHub operations (creating PRs, viewing issues, checking CI status, reviewing PRs, etc.). Never construct raw API URLs or use `curl` against the GitHub API. Examples: `gh pr create`, `gh issue view`, `gh run list`, `gh api`.

---

### Section 6 — AI Coding Guidelines & Domain Rules

One bold-label paragraph per topic. No code blocks. No sub-headers. Cover these topics in order (skip any that do not apply):

1. **Error Handling** — functional type used, how to fold, where new error types live, required fields.
2. **Database Access** — ORM/codegen rule, multi-tenancy mechanism, transaction requirement, audit-trail rule, mapper placement.
3. **Immutability** — data class rule, nullable fields, side-effect rules.
4. **Workflow & Task Queues** *(only if the project uses Temporal, Celery, or similar)* — workflow-vs-activity boundary, activity naming, context propagation object, retry-config location, queue definitions, side-effect rules.
5. **State Machine** *(only if applicable)* — config location, state types, expression engine, hardcoding prohibition, terminal state behaviour.
6. **Dependency Injection** — approach, wiring location, module registration pattern.
7. **Versioning** — where version constants live, rule about hardcoding in module build files.

---

### Section 7 — Important Files Reference

A table listing every configuration, build, and key source file that an AI assistant should know about.

| File Path | Category | Purpose |
|---|---|---|
| *(list each file)* | `build` / `config` / `schema` / `entrypoint` / `test-config` / `spec` / `ci` / `docs` | *(concrete purpose)* |

Include files at the repository root AND significant files within subdirectories. For `config` files, include only a file path + purpose — never expand their values. Categories:
- `build` — build scripts, dependency declarations, properties files
- `config` — application configuration, environment files (reference only — no value details)
- `schema` — database migrations, protobuf definitions, OpenAPI specs
- `entrypoint` — main classes, starter files
- `test-config` — test configuration, fixtures
- `spec` — API specifications (OpenAPI, protobuf, GraphQL)
- `ci` — CI/CD pipeline definitions
- `docs` — design documents, ADRs

---

### Section 8 — Maintenance

| Field | Value |
|---|---|
| Last Updated | *(today's date in YYYY-MM-DD format)* |
| Project Version | *(read from `gradle.properties` property `version`, falling back to `build.gradle.kts` `version =` declaration, falling back to `package.json` `"version"` field — use whichever exists first)* |
| Maintained By | {TEAM} |

---

## Step 3: Write the File

After completing Steps 1 and 2:

1. If `CLAUDE.md` already exists in this directory, read it with your `Read` tool to extract any accurate facts (version numbers, team names, known caveats). Do **not** preserve its structure — always write the output using the exact section order and format defined in Step 2 above.
2. Write the complete, final `CLAUDE.md` to this directory using your `Write` tool.

Do **not** output any preamble, explanation, or summary — just use the `Write` tool to create the file and then stop.
