# Prompt: Generate Root CLAUDE.md for Repository

**Context:** You are at the root of the `{REPO}` repository. Maintaining team: `{TEAM}`.

This repository has CLAUDE.md context files in the following sub-directories:
{NESTED_FILES}

All modules in this repository (including those without their own CLAUDE.md):
{ALL_MODULES}

---

## Instructions

1. Read each of the child CLAUDE.md files listed above using your Read tool.
2. Write a root `CLAUDE.md` in the current directory following the output format below.

**Critical rule:** Do NOT duplicate information that already exists in child CLAUDE.md files. For modules that have their own CLAUDE.md, provide a brief pointer — do not restate their content. The root file covers repository-wide concerns and fills gaps for modules that lack their own CLAUDE.md.

---

## Output Format

Apply these **global style rules**:
- No prose padding. Every sentence must carry a fact not expressed elsewhere.
- Use inline code for class names, file paths, method names, and literals.
- In tables, never leave the last column vague — it must state the concrete role.

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
- Module overview table with these columns:

| Module | LOC | CLAUDE.md | Role |
|---|---|---|---|
| *(module path)* | *(line count)* | *(link to child CLAUDE.md if it exists, or "—")* | *(For modules WITH a child CLAUDE.md: 5-word summary + "See [CLAUDE.md](path)". For modules WITHOUT a child CLAUDE.md: 1-2 sentence description of what the module does, its key classes, and dependencies)* |

Modules without their own CLAUDE.md should have enough detail in the Role column that an AI assistant can work with them effectively.

---

### Section 2 — Build & Run (Repository-Wide)

- One-line **Prerequisites** row listing every required local service, tool, and SDK with versions and default ports.
- A single fenced bash block containing: full build, full test suite, Docker compose up/down, and any repo-wide codegen or migration commands.
- Below the block: short blockquote notes on migration locations, generated-code locations, and "never edit manually" warnings.
- One sentence on the **Dependency Injection** approach used across the repo (framework, wiring entry point).

---

### Section 3 — Cross-Cutting Architecture

- **Dependency flow diagram**: Mermaid diagram (`graph TD`) showing how modules depend on each other (direction of dependencies). This is the ONLY place in the CLAUDE.md hierarchy where a module dependency diagram should appear — child CLAUDE.md files must NOT include one.
- **Shared infrastructure**: One paragraph covering database(s), message broker(s), workflow engine(s), caching layer(s), and any other shared stateful systems.
- **Naming conventions**: Inline table of `Concept | Convention | Example` covering workflows, activities, repositories, errors, services, and any other project-specific patterns.
- **Observability**: Tracing interceptor rule, log format, metrics registration location.

---

### Section 4 — CLAUDE.md Hierarchy

This repository uses a hierarchy of CLAUDE.md files to organize context:

| File | Scope |
|---|---|
| `CLAUDE.md` (this file) | Repository-wide: build, architecture, cross-cutting concerns, rules, coding guidelines |
| *(list each child CLAUDE.md)* | *(what that child file covers)* |

**Rule:** When adding or updating documentation, information should be placed in the CLAUDE.md file closest to the code it describes. The root file should be updated only when no child CLAUDE.md file is closer to the change path. Module-specific build instructions, integrations, and file references belong in the child CLAUDE.md, not here.

---

### Section 5 — Rules

Include the following rules verbatim, plus any additional project-specific rules discovered during analysis:

**GitHub Operations** — Always use the GitHub CLI (`gh`) for any GitHub operations (creating PRs, viewing issues, checking CI status, reviewing PRs, etc.). Never construct raw API URLs or use `curl` against the GitHub API. Examples: `gh pr create`, `gh issue view`, `gh run list`, `gh api`.

---

### Section 6 — AI Coding Guidelines & Domain Rules

One bold-label paragraph per topic. No code blocks. No sub-headers. Cover these topics in order (skip any that do not apply to this repository):

1. **Error Handling** — functional type used, how to fold, where new error types live, required fields.
2. **Database Access** — ORM/codegen rule, multi-tenancy mechanism, transaction requirement, audit-trail rule, mapper placement.
3. **Immutability** — data class rule, nullable fields, side-effect rules.
4. **Workflow & Task Queues** *(only if the project uses Temporal, Celery, or similar)* — workflow-vs-activity boundary, activity naming, context propagation object, retry-config location, queue definitions, side-effect rules.
5. **State Machine** *(only if applicable)* — config location, state types, expression engine, hardcoding prohibition, terminal state behaviour.
6. **Dependency Injection** — approach, wiring location, module registration pattern.
7. **Versioning** — where version constants live, rule about hardcoding in module build files.

---

### Section 7 — Important Files Reference (Root-Level Only)

**Strict scoping rule:** This table must ONLY contain files whose path is at the repository root (not inside any module directory). If a file lives under a module that has its own child CLAUDE.md, it MUST NOT appear here — it belongs in that child's Important Files section. Before adding any file, check: does its path start with a module directory that has a child CLAUDE.md? If yes, skip it entirely.

| File Path | Category | Purpose |
|---|---|---|
| *(only root-level files)* | `build` / `config` / `schema` / `ci` / `docs` | *(concrete purpose)* |

**Configuration files:** Any config files at the repository root (e.g., `env.sh`, `application.conf`) must be listed here with a link. Child modules that reference repository-level config should point to this section rather than detailing config values inline.

Do NOT add rows for child CLAUDE.md files here — they are already listed in Section 4 (CLAUDE.md Hierarchy). Adding them here would be a duplicate.

---

### Section 8 — Maintenance

| Field | Value |
|---|---|
| Last Updated | *(today's date in YYYY-MM-DD format)* |
| Project Version | *(read from `gradle.properties` property `version`, falling back to `build.gradle.kts` `version =` declaration, falling back to `package.json` `"version"` field — use whichever exists first)* |
| Maintained By | {TEAM} |

---

## Write Rules

- If a root `CLAUDE.md` already exists, read it first with your Read tool and update only what has changed. Preserve sections that remain accurate.
- Write the file using your Write tool. Do not output any preamble or summary — just write the file and stop.
