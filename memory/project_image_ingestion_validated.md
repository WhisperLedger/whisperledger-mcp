---
name: Image + video ingestion — DEPLOYED in iterate, end-to-end validated 2026-05-19
description: Multimodal ingestion (images + ffmpeg-keyframed videos from PR comments and Jira attachments) is live in jarvis_iterate.sh and confirmed correct by a real reviewer on a hard visual bug. /api/v1/fix still missing the same plumbing — productize next.
type: project
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---

**Status:** Deployed in `jarvis_iterate.sh` and end-to-end validated.

**Validation:** On 2026-05-19, PR #14140 (RECO-1259 Contact Bottom Sheet) went through 3 confidently-wrong text-only iterations. v4 (with image from Prasanna's review comment + 10 keyframes from the original Jira screen recording) identified the correct root cause — percentage snap-point recalculation under `keyboardBehavior="interactive"` shrinking when the keyboard opens. Prasanna confirmed on a real device that the resulting commit `821ee18f39` fixed the original bug.

**What's in iterate today:**
- Auto-extracts image/video URLs from PR comments via regex (`grep -oE 'https?://[^"<> )]+(\.png|\.jpg|\.jpeg|\.gif|\.mp4|\.mov|\.webm|/user-attachments/assets/[a-z0-9-]+|atlassian\.net/rest/api/[0-9]/attachment/content/[0-9]+)'`)
- Auth resolution by hostname: `github.com/user-attachments` → `gh auth token`; `*.atlassian.net` → `$CONFLUENCE_EMAIL:$CONFLUENCE_API_TOKEN` basic auth
- Video → image: `ffmpeg -vf "fps=${FPS}"` extracts N keyframes (default 10) per video, each passed as separate image content block
- `ITERATE_EXTRA_ATTACHMENTS` env var lets the caller add URLs not present in PR comments
- Pipes stream-json `user` message to `claude --print --verbose --input-format stream-json --output-format stream-json` (note: `--verbose` is required by the CLI when both `--print` and `--output-format=stream-json` are set)
- Cost capture from `total_cost_usd` in the final stream-json `result` event

**Per-iteration cost shape (observed 2026-05-19):**
- text-only iteration: ~$1.50, often confidently wrong on visual bugs
- iteration with image + video: ~$0.75, materially better-targeted
- One multimodal iteration is cheaper AND more accurate than three text-only ones — the math is unambiguous when visuals are available

**Still missing — next productization pass (~half day):**
- Add `attachments: list[str]` to `FixRequest` schema in `scripts/api/server.py` and propagate into `scripts/jarvis_fix.sh`. Today only `/api/v1/pr/iterate` has multimodal; `/api/v1/fix` (used by Jove for PRD-to-PR) still text-only.
- For Jove caller: add `attachments` field documentation to the handoff doc; recommend they pass Jira ticket attachment URLs along with the brief.
- Optional: detect when a Jira ticket is referenced in the input and auto-fetch attachments from the Jira API (currently caller must supply URLs explicitly).

**Companion rule:** see `feedback_frontend_ticket_needs_visuals.md` — frontend bugs without images/videos should be FLAGGED before firing iterate/fix, because text-only on visual bugs has a high cost-of-wrong-answer.
