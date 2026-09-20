---
name: Channel posts: prefer top-level over threaded for usage info
description: Usage / how-to / can-and-can't framings should be top-level channel posts, not thread replies. Threading hides them from people scrolling the channel.
type: feedback
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
When posting usage / how-to / capability framings ("here's how to use X, here's what it can/can't do") to the team Slack channel, post as a *top-level channel message*, not a thread reply on a related parent post.

**Why:** Rohit corrected this on 2026-05-15. I had posted Confluence ad-hoc-fetch instructions as a thread reply under the related "Confluence deprioritised" post (logical from a context-chain perspective), but he flagged that thread replies get missed by people scrolling the channel — only those subscribed to the thread see them. For usage/how-to content where reach matters more than context-chaining, top-level wins.

**How to apply:**
- Usage instructions, can/can't framings, capability launches, "how to use X" → *top-level channel post*
- Even if there's a related parent post, repeat the relevant context inline + post fresh top-level rather than threading
- Reserve threading for: actual back-and-forth conversation under a specific post, replies to user questions in a thread they started, follow-ups to a specific decision in a parent
- If I've already threaded something that should have been top-level, delete the threaded reply (`chat.delete`) and repost top-level — better than leaving duplicate content
