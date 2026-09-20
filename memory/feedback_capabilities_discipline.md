---
name: Feature ship checklist — always update capabilities.py
description: On every Jarvis feature ship, update scripts/agent/capabilities.py manifest. The agent introspects this on meta-questions; stale manifest = wrong self-description.
type: feedback
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
When shipping any new Jarvis feature (slash command, HTTP endpoint, background subsystem, etc.), update `scripts/agent/capabilities.py` as part of the deploy. The agent's `get_capabilities()` tool reads from this manifest on self-referential questions ("what can you do", "are you good at X", "do you have Y feature").

**Why:** Rohit explicitly built this self-awareness layer on 2026-05-15 after Jarvis gave Vasanthakumar a generic answer about secure code review without mentioning the literally-1-hour-old `/jarvis review` feature — root cause was the system prompt being stale relative to what shipped. The capabilities manifest is the single source of truth; if it's not updated, the agent falls back to freelancing from prompt knowledge and misses the new feature.

**How to apply:** Treat `capabilities.py` as part of every feature ship's deploy package, alongside:
- The wrapper script (jarvis_X.sh / jarvis_X.py)
- format.py blocks (ack/progress/success/usage)
- app.py dispatcher
- Help text in `/jarvis help`
- Channel announcement post
- *capabilities.py manifest entry* — this is the new addition I keep forgetting

Each capability entry needs: command, name, category, summary, when_to_use, when_NOT_to_use, example, cost_range_usd, duration_typical, scope, limits, shipped (date IST). Pattern is clear from the existing entries — just copy-paste-edit a similar one.

If I forget this on a future feature ship, the symptom will be: agent gives stale answers to meta-questions even after the feature is live and announced. The smoke test is "ask Jarvis 'do you have X' where X is the just-shipped feature" — it should now CALL get_capabilities() and answer accurately.
