---
name: User-facing flows DM Jarvis, not Rohit directly
description: When drafting any user-facing message that asks people to take an action (provide creds, ask questions, request access, file issues), the DM target is the Jarvis bot, not Rohit's personal user ID. Rohit is the approver behind Jarvis, not the front door. Established 2026-05-21 after the jarvis-mcp beta announcement incorrectly told engineers to "DM <@U0837N31T9C>".
type: feedback
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---

**Rule:** Any user-facing Slack copy (announcements, error messages, help text, onboarding instructions) directs users to DM **Jarvis bot** (`<@U0B37LWBP98>` / `@Jarvis`), NOT Rohit's personal user (`<@U0837N31T9C>`). Even when Rohit is the human approver behind the scenes, Jarvis is the front door.

**Why:**
- Engineers shouldn't need to know who runs Jarvis to use it.
- Rohit's personal DM queue isn't the right routing layer for engineering-team requests at scale.
- Jarvis can validate / queue / forward / log, then ping Rohit on the operator DM channel for the actual approval. The user-facing interaction stays one-hop.
- Established 2026-05-21 when the jarvis-mcp beta announcement told engineers to "DM <@U0837N31T9C>" with their SSH keys + transport choice. Rohit pushed back: "They can DM you, why me".

**How to apply:**
- Default to `<@U0B37LWBP98>` (Jarvis bot Slack user ID) for any "DM us with..." line in user-facing copy.
- If the underlying flow requires Rohit's approval (e.g. SSH key addition, anything touching production state), Jarvis handles the inbound, parses/validates the request, and pings Rohit on his operator DM with the structured request. Engineer sees one interaction (with Jarvis); Rohit sees one approval moment (in his Jarvis DM).
- If Jarvis doesn't yet have a skill for the flow, BUILD the auto-forwarder before announcing — or at minimum, an auto-responder that says "got it, Rohit will reply" so the engineer isn't met with silence.
- Edge case: ad-hoc 1:1 collaboration questions (e.g. SRE bot integration design discussions) can still DM Rohit directly. But anything scaled to "DM someone with X" in a channel post defaults to Jarvis.

**Don't:**
- Don't include `<@U0837N31T9C>` in any channel post or broad-audience message.
- Don't build flows that assume Rohit is reachable / responsive within hours. Jarvis is the SLA buffer.

**Related:**
- `feedback_slack_permission.md` — Jarvis still needs per-message approval to send anything, so even Jarvis-as-front-door doesn't bypass Rohit's approval; it just shifts WHERE approval happens (Rohit's Jarvis-DM queue, not his personal DMs).
- `feedback_alert_routing_default.md` — system alerts default to operator DM; that's about MACHINE-generated alerts, distinct from this rule which is about USER-facing copy.
