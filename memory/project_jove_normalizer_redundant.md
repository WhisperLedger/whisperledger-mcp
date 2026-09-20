---
name: jove_client.normalize_jove_response is now redundant
description: Client-side regex post-processor inserting \\n\\n at agent-step boundaries is no-op since Jove server-side fix (commit 5bed6d6, 2026-05-15). Safe to leave as defense-in-depth or remove on next deploy.
type: project
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
`scripts/agent/jove_client.py:normalize_jove_response()` was added on 2026-05-15 to client-side patch a Jove formatting bug where the buffered MCP/Slack consumers concatenated agent-step text blocks with no `\n\n` between them. Jove team root-fixed it the same day at commit `5bed6d6` — they added a `pending_separator` flag at the buffered-consumer boundary (`_do_jove_chat`, `_run_agent`) that flips on every `tool_call.done` event and gets consumed by the next text event, prepending `\n\n` before the chunk. Web frontend was unaffected because it renders deltas live; only buffered consumers needed it.

**Why:** Verified end-to-end on the original repro query (`What is the latest change in Web OB Journey for Loans` against PROD space): raw response now has 20 `\n\n` separators (was 0 before), 0 detectable run-on boundaries. Running `normalize_jove_response()` over the already-fixed text leaves it at 20 separators — confirmed idempotent.

**How to apply:**
- Function stays in place as defense-in-depth — costs ~100µs of regex per response; protects against any future Jove regression or bypass-the-buffer code path
- If decluttering on a future deploy, the call site is in `jove_client.ask()` (the `result["response"] = normalize_jove_response(...)` line). Remove that line + the function definition + the `_TRANSITION_PHRASES` / `_NORMALIZE_RE` module-level state. ~3 lines of net deletion
- Don't remove unless we have a reason; "extra layer that catches a returning bug silently" is worth ~100µs of CPU
