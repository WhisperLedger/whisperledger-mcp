# Prompt: Generate CLAUDE.md for a Module Directory

**Context:** You are analysing the `{DIRECTORY}` directory inside the `{REPO}` repository. Maintaining team: `{TEAM}`.

Act as a Staff Engineer standardizing repository context for AI assistants and MCP servers.

Analyse this directory and write a `CLAUDE.md` file with two goals:
1. Deep, specific context for AI coding tasks (build quirks, architectural rules, spatial awareness).
2. Strictly formatted Markdown tables of network topology so an external MCP server can parse them programmatically.

**Hard boundary — zero transitive references:**
This CLAUDE.md must describe ONLY the code that lives inside `{DIRECTORY}/`. Every other module in this repository is an opaque black box. Concretely:
- You may NAME a sibling module as a dependency (e.g., "depends on `common`"). That is the only permitted reference.
- Do NOT say what a sibling module contains, provides, defines, or exposes (no class names, no enum names, no "accessed via X module", no "controllers live in Y").
- Do NOT describe how this module is consumed by other modules ("consumed by Z controller" is forbidden).
- In infrastructure tables, describe what THIS module does (e.g., "writes onboarding journeys to PostgreSQL"), not which other module provides the access layer.
- If an interface or class that this module calls is defined in another module, do NOT name that class — describe the capability this module uses in its own terms.
- **Configuration:** Do NOT detail or expand any config file anywhere in this document — no values, no keys, no port numbers from config. Config files that physically exist inside this module's directory must appear as a row in Section 4 (Important Files Reference) with their file path and purpose. Config files from outside this directory must not be referenced at all.

---

## Step 1: Files to Scan (read all of these before writing anything)

| Category | What to read |
|---|---|
| **Build** | Root `build.gradle.kts`, `settings.gradle.kts`, `buildSrc/.../Dependency.kt`, this module's `build.gradle.kts` |
| **Config & Env** | Check for config files physically inside this module's directory (e.g., `src/main/resources/application.conf`). If found, note the file path only — do not read or expand their contents. Ignore config files from the repository root or other modules. |
| **Startup** | `src/main/kotlin/**/starter/Main.kt` and every `*Starter.kt` in that directory |
| **Exposed APIs** | Every `*Server.kt` or `*Controller.kt` in `server/` or equivalent — read fully to extract every public method. Also look for OpenAPI spec files (`openapi.yaml`, `openapi.json`, `swagger.yaml`, etc.) |
| **Downstream APIs** | Every service interface file in `external-service/` (or equivalent) — read fully. Also look for OpenAPI spec files referencing external services |
| **Domain** | Key files in `domain/` for error hierarchy, shared enums, and context objects |
| **Data layer** | One representative repository implementation in `data/` to confirm JOOQ/ORM patterns |
| **State machine** | One sample partner config file in `src/main/resources/partner-state-language/` (or equivalent) |
| **Tests** | One representative test file to identify the testing framework |
| **Build metadata** | `gradle.properties`, root `build.gradle.kts` (for version), `package.json` if present |

---

## Step 2: Output Format

Write the file in exactly the sections described below. Apply these **global style rules**:
- No prose padding. Every sentence must carry a fact not expressed elsewhere.
- Use inline code for class names, file paths, method names, and literals.
- In tables, never leave the last column vague — it must state the concrete role or signature.

---

### Section 1 — Local Setup, Build & Startup

**Conditional:** First determine whether this module can be independently built, run, or tested (i.e., it has its own main entry point, standalone build target, or independent test suite). If it cannot, replace this entire section with:

> This module is built and run as part of the parent project. See the root `CLAUDE.md` for repository-wide build and run instructions.

If the module **can** be independently run, include:

- One-line **Prerequisites** row listing every required local service with its default port and any credential files.
- A single fenced bash block containing every command needed: full build (with mandatory ordering noted inline), run, all-tests, single-test, Docker build, and any schema/codegen targets.
- Below the block: short blockquote notes covering migration file location, generated-code location, and any "never edit manually" warnings.
- Include any daemon/cache-busting troubleshooting command if one exists.

**Startup Sequence** (within this section):
- Numbered list, one item per startup step, naming the exact class and what it does. Include port numbers and configuration keys where relevant.

---

### Section 2 — Module Topology & Architecture

- Mermaid diagram (`graph TD`) showing **only this module's internal package structure**. Do **not** include any other module's tree or package structure. Do **not** include a module-level dependency diagram — that belongs in the root `CLAUDE.md` only.
- One-line **Dependency list** naming only the sibling modules this module depends on (e.g., "Depends on: `common`, `dao`"). Do NOT describe what those modules contain, expose, or do. Do NOT describe which modules consume this one.
- If API spec definitions (protobuf, OpenAPI, GraphQL) that govern this module's interfaces live outside it, state only that fact and the spec file path — nothing else about the module that owns the spec.

---

### Section 3 — Integrations & Network Topology *(MCP-parseable — strict tables required)*

#### A. Core Infrastructure
Table columns: `System | Protocol | Role`
Cover every stateful dependency (database, workflow engine, message broker, task queues). Include DB name, schema history table, topic names, namespace, and queue definitions. If the project uses Temporal, include task queue entries with their bound workflows in this table — do not create a separate Temporal section.

#### B. Downstream Services — APIs We Consume
**Only list services whose server process runs OUTSIDE this repository's codebase** — i.e., a separate deployment owned by another team or a third-party SaaS/API. A service is NOT external just because its Feign client or interface wrapper lives in a different module within the same repo (e.g., `internal-services`, `external-services`). Those wrapper modules are intra-repo code and calling through them is an internal dependency, not an external integration.

If this module has no truly external downstream services, write: "No direct external service integrations. All downstream calls are routed through intra-repo service modules."

For each genuinely external service:
- Write a level-4 heading `#### {Service Name} — {Protocol}` followed by a one-sentence summary of what this service provides.
- **Do NOT list individual methods, API endpoints, or signatures.** Instead, point to the file(s) that define the integration — OpenAPI/protobuf/GraphQL spec files, Feign client interfaces, or service wrapper classes. Use: `> Integration: \`path/to/spec-or-client-file\``

#### C. Exposed Interfaces — APIs We Provide
For each gRPC server / REST controller:
- Write a level-4 heading `#### {ClassName} ({parent interface or base class})` with a one-sentence summary.
- **Do NOT list individual methods or endpoints.** Instead, point to the file(s) that define the interface — spec files or the server/controller source file. Use: `> Spec: \`path/to/spec-or-source-file\``
- Add a small table for any HTTP observability endpoints (health, metrics) with columns `Path | Description`.

---

### Section 4 — Important Files Reference

A table listing every configuration, build, and key source file in this module that an AI assistant should know about.

| File Path | Category | Purpose |
|---|---|---|
| *(list each file)* | `build` / `config` / `schema` / `entrypoint` / `test-config` / `spec` | *(concrete purpose)* |

Only include files that actually exist **inside this module's directory**. For `config` files, include only a file path + purpose — never expand their values. Categories:
- `build` — build scripts, dependency declarations, properties files
- `config` — application configuration, environment files (reference only — no value details)
- `schema` — database migrations, protobuf definitions, OpenAPI specs
- `entrypoint` — main classes, starter files
- `test-config` — test configuration, fixtures
- `spec` — API specifications (OpenAPI, protobuf, GraphQL)

---

### Section 5 — Maintenance

| Field | Value |
|---|---|
| Last Updated | *(today's date in YYYY-MM-DD format)* |
| Project Version | *(read from `gradle.properties` property `version`, falling back to `build.gradle.kts` `version =` declaration, falling back to `package.json` `"version"` field — use whichever exists first)* |
| Maintained By | {TEAM} |

---

## Step 3: Write the File

After completing Steps 1 and 2:

1. If `CLAUDE.md` already exists in this directory, read it first with your `Read` tool. Update only the sections whose underlying source facts have changed. Preserve sections that remain accurate.
2. Write the complete, final `CLAUDE.md` to this directory using your `Write` tool.

Do **not** output any preamble, explanation, or summary — just use the `Write` tool to create the file and then stop.
