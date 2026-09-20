---
name: Never kill running processes / jobs / sessions without explicit permission
description: Don't run kill / pkill / systemctl stop / proc.terminate on any user-visible process (bash jobs, Claude subprocesses, in-flight HTTP jobs, services) without asking Rohit first
type: feedback
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
Rule: Before killing or terminating any running process — bash wrapper, Claude subprocess, in-flight HTTP fix/claudify job, systemd service, or anything else Rohit can see — pause and ask, even if it looks hung, slow, or about to bust a budget. Show the process state and ask whether to kill, wait, or extend. Default action is **wait, not kill**.

**Why:** Rohit set this rule on 2026-05-17 while a Jove-initiated `/api/v1/fix` job was running on a comprehensive Insurance Home V2 brief — a multi-minute, $5-budget job that produces high-value output. Killing it prematurely would waste both the spend already incurred and the chance of getting a real PR. The default of "wait and observe" preserves work; a hasty kill destroys it. This applies even when the process *looks* hung (e.g. 0-byte log file, 0% CPU snapshot, several minutes between visible activity) — agent loops are bursty by nature.

**How to apply:**
- Never invoke `kill`, `pkill`, `proc.terminate()`, `proc.kill()`, `systemctl stop`, or any other process-termination command on a job the user can see, without first showing them: (1) what process, (2) how long it's been running, (3) what the failure modes look like, (4) why I think killing would be the right call. Wait for explicit "kill" / "stop" / "terminate" response.
- Restarts of services (`systemctl restart jarvis-api` etc.) ALSO count when they would interrupt in-flight HTTP jobs — confirm no in-flight jobs first, or get explicit permission to interrupt them.
- Automatic timeouts that are part of the system design (e.g. the 900s asyncio.wait_for in `api/jobs.py`, the `--max-budget-usd` cap inside `jarvis_fix.sh`) DO fire automatically — they're not "me killing." But proactively flag to Rohit when one is about to fire and there might be value in extending, so he has the option to intervene.
- Does NOT apply to: idle background helper processes I myself spawned for monitoring (e.g. `Bash run_in_background` poll loops); cleaning up dead/zombie processes; finished workspaces being GC'd by the existing janitor.
