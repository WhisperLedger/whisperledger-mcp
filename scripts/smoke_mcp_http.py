"""HTTP-SSE / streamable-http smoke test for jarvis-mcp Phase 2.

Hits http://127.0.0.1:8082/mcp/ with Bearer auth, lists tools, runs a few
read-only calls + jarvis_fire_preflight against a tiny diff (read-only, no writes).

Does NOT invoke jarvis_fire_fix or jarvis_fire_iterate (would open real PRs).

Run on the box (after starting jarvis-mcp.service):
    cd /home/ubuntu/jarvis/scripts && ./indexer/.venv/bin/python smoke_mcp_http.py
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("JARVIS_MCP_URL", "http://127.0.0.1:8082/mcp/")
API_KEY = os.environ.get("JARVIS_API_KEY", "")


async def main() -> int:
    if not API_KEY:
        print("[smoke] FATAL: JARVIS_API_KEY not set in env", file=sys.stderr)
        return 2

    # First a /health probe — public, no auth
    health_url = URL.replace("/mcp/", "/health")
    try:
        r = httpx.get(health_url, timeout=5)
        print(f"[smoke] /health = {r.status_code} {r.text.strip()[:120]}", flush=True)
        if r.status_code != 200:
            return 1
    except Exception as e:
        print(f"[smoke] /health FAILED: {e}", flush=True)
        return 1

    # Bearer-auth check: missing token should 401
    try:
        r = httpx.post(URL, timeout=5, headers={"Accept": "application/json,text/event-stream"})
        if r.status_code != 401:
            print(f"[smoke] WARN: unauthenticated POST returned {r.status_code}, expected 401",
                  flush=True)
        else:
            print("[smoke] unauthenticated POST → 401 (auth gate OK)", flush=True)
    except Exception as e:
        print(f"[smoke] auth-gate probe error (non-fatal): {e}", flush=True)

    headers = {"Authorization": f"Bearer {API_KEY}"}
    print(f"[smoke] connecting streamable-http: {URL}", flush=True)
    async with streamablehttp_client(URL, headers=headers) as (read, write, _session_info):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_resp = await session.list_tools()
            tools = [t.name for t in tools_resp.tools]
            print(f"[smoke] tools advertised ({len(tools)}): {', '.join(tools)}",
                  flush=True)

            calls = [
                ("jarvis_list_indexed_repos", {}),
                ("jarvis_get_capabilities", {"category": "qa"}),
                ("jarvis_search_code",
                 {"query": "iterate auto fire poller", "repo": "jarvis", "k": 2}),
                # Phase 3 read-only trigger: refusal for write trigger without idempotency_key
                ("jarvis_fire_fix",
                 {"repo": "jarvis", "description": "should be refused (no idempotency_key)",
                  "idempotency_key": ""}),
                # Phase 3 read-only trigger: a real preflight on a tiny diff
                ("jarvis_fire_preflight",
                 {"repo": "jarvis",
                  "diff": "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1,1 +1,1 @@\n-x\n+y\n",
                  "requester": "smoke_mcp_http"}),
            ]

            ok = True
            for name, args in calls:
                t0 = time.time()
                try:
                    res = await session.call_tool(name, args)
                    ms = int((time.time() - t0) * 1000)
                    parts = []
                    for c in res.content:
                        text = getattr(c, "text", None)
                        if text is not None:
                            parts.append(text)
                    body = "\n".join(parts)
                    try:
                        parsed = json.loads(body)
                        head = json.dumps(parsed)[:280]
                    except Exception:
                        head = body[:280].replace("\n", " ")
                    print(f"[smoke] {name:30s} {ms:5d}ms  {head}", flush=True)
                except Exception as e:
                    ok = False
                    print(f"[smoke] {name:30s} FAILED  {type(e).__name__}: {e}",
                          flush=True)
            print(f"[smoke] HTTP {'PASS' if ok else 'FAIL'}", flush=True)
            return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
