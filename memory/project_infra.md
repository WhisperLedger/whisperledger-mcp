---
name: Jarvis infra — remote dev box
description: All Jarvis work runs on a remote Ubuntu EC2 box, not Rohit's local machine
type: project
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
All Jarvis development and indexing happens on a remote Ubuntu host, not locally.

- **Host:** `3.6.202.121`
- **User:** `ubuntu`
- **SSH key:** `~/Downloads/data-science.pem` (on Rohit's local Mac)
- **Connect:** `ssh -i ~/Downloads/data-science.pem ubuntu@3.6.202.121`

**Why:** Indexing 150+ repos + Confluence requires non-trivial disk/CPU/memory and a stable always-on environment; doing it on a laptop is not viable. Also keeps secrets/clones off personal machines.

**How to apply:**
- Default to running setup, clones, indexing jobs, and any long-running work on the remote box via SSH.
- Local Mac is for orchestration / editing only — don't `git clone` Jupiter repos to `/Users/rohitpandey/Projects/jarvis` unless explicitly asked.
- Before picking pilot repos, SSH to the box and use `gh` there (Rohit will provision GitHub access on the remote).
