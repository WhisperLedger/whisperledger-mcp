---
name: Never grep /proc/*/environ with broad patterns — leaks secrets
description: When verifying env vars in running processes, always grep for the SPECIFIC variable name. Broad patterns like `API_KEY=` will print every API key in the process environ verbatim into tool output (and the session transcript). Established 2026-05-19 after ANTHROPIC/VOYAGE/OPSGENIE keys leaked while verifying JARVIS_WRITE_ALLOWED_REPOS.
type: feedback
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
**Rule:** When verifying an env var is present in a running process, use an EXACT variable name pattern in the grep, never a category pattern. `grep WRITE_ALLOWED_REPOS` is fine; `grep API_KEY=` is a footgun.

**Why:** On 2026-05-19, while verifying that `JARVIS_WRITE_ALLOWED_REPOS` had propagated to the `jarvis-api` process env, I used `grep -E "WRITE_ALLOW|API_KEY="` to capture both that var and (intent) the redacted `JARVIS_API_KEY`. The pattern also matched `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`, and `OPSGENIE_API_KEY` — all printed verbatim into the bash tool output, which is now in the conversation context, the terminal scrollback, and the session transcript on disk. Required a three-key rotation.

**How to apply:**
- For env verification, name the exact var: `tr '\0' '\n' < /proc/$PID/environ | grep '^JARVIS_WRITE_ALLOWED_REPOS='`.
- If you must scan multiple vars, list them explicitly: `grep -E '^(JARVIS_WRITE_ALLOWED_REPOS|JARVIS_API_KEY)='` and pipe through a redaction step that masks anything matching `_KEY=` or `_TOKEN=` before display.
- Never use `_API_KEY` or `_TOKEN` as a substring pattern when output goes to a chat/log surface.
- The same applies to `printenv`, `env`, `cat .env`, `docker inspect`, `kubectl describe pod`, anywhere env-shaped data flows.

**Recovery if it happens again:**
1. Flag immediately to the user — don't hide it.
2. Identify which keys leaked (by name).
3. Recommend rotation (internal-only leak ≠ urgent, but a 24h window is prudent).
4. Be more careful with the next grep pattern in the same session.
