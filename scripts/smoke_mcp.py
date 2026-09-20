"""Smoke test for jarvis-mcp Phase 1.

Spawns the MCP server as a subprocess over stdio, lists tools, invokes a few,
prints latencies + a brief sample of each response. Exit 0 on full pass.

Run on the box:
    cd /home/ubuntu/jarvis/scripts && ./indexer/.venv/bin/python smoke_mcp.py
"""
from __future__ import annotations
import asyncio
import json
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SCRIPTS_DIR = Path(__file__).resolve().parent
WRAPPER = SCRIPTS_DIR.parent / "scripts" / "run_mcp_server.sh"
# Some hosts launch us via different cwd; pin a known-good path.
SERVER_CMD = "/home/ubuntu/jarvis/scripts/run_mcp_server.sh"


def _short(s: str, n: int = 240) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "..."


async def main() -> int:
    params = StdioServerParameters(command="bash", args=[SERVER_CMD])
    print(f"[smoke] launching: bash {SERVER_CMD}", flush=True)
    async with stdio_client(params) as (read, write):
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
                 {"query": "stargate routing config", "repo": "gateway", "k": 3}),
                ("jarvis_find_repo", {"needles": ["bff-core"]}),
            ]

            ok = True
            for name, args in calls:
                t0 = time.time()
                try:
                    res = await session.call_tool(name, args)
                    ms = int((time.time() - t0) * 1000)
                    parts = []
                    for c in res.content:
                        # FastMCP returns TextContent objects
                        text = getattr(c, "text", None)
                        if text is not None:
                            parts.append(text)
                    body = "\n".join(parts)
                    # validate JSON
                    try:
                        parsed = json.loads(body)
                        head = json.dumps(parsed)[:240]
                    except Exception:
                        head = _short(body)
                    print(f"[smoke] {name:30s} {ms:5d}ms  {head}", flush=True)
                except Exception as e:
                    ok = False
                    print(f"[smoke] {name:30s} FAILED  {type(e).__name__}: {e}",
                          flush=True)
            print(f"[smoke] {'PASS' if ok else 'FAIL'}", flush=True)
            return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
