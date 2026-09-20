---
name: Rollout-announcement style — credit validator only
description: When announcing a rollout/launch, name only the person who directly validated the deliverable; don't credit adjacent project leads or other contributors in the same area.
type: feedback
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
When posting rollout/launch announcements (Slack channels, broader comms), name *only* the person who directly validated the deliverable — e.g. "Shouvik validated the smoke test." Do not name other engineers as "project leads," "owners," or otherwise position them as adjacent validators, even if they've shipped most of the related work in that area.

**Why:** Rohit explicitly corrected this twice in one day (2026-05-14): (a) coached me away from over-gating on Ankita's offline approval before posting the "coming soon" claudify message, and (b) instructed me to remove her from the live "go for self-serve" announcement after I credited her as project lead. Pattern: he prefers tight, action-focused launch posts that name only the validator, not a chain of credit/permission.

**How to apply:** In future channel announcements where I'm tempted to credit multiple people for context (project leads, squad owners, prior contributors), default to mentioning *only* the direct validator. If I think a broader credit is warranted, ask first — don't add it speculatively. Same applies to `cc:` lines at the bottom of posts.
