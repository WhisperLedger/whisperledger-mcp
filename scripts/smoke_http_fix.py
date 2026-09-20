"""Smoke test for POST /api/v1/fix → GET /api/v1/fix/{id}.

Usage: run on the box where the jarvis-api is reachable.
   JARVIS_API_KEY=... python smoke_http_fix.py
"""
from __future__ import annotations
import os
import sys
import time

import urllib.request
import urllib.error
import json

API_BASE = os.environ.get("JARVIS_API_BASE", "http://127.0.0.1:8081")
API_KEY = os.environ.get("JARVIS_API_KEY")
if not API_KEY:
    sys.exit("JARVIS_API_KEY env var must be set")


def _req(method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "X-Jarvis-Caller": "smoke-test",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, {"error": body}


def main() -> int:
    print(f"smoke-testing {API_BASE}/api/v1/fix")
    # Target: jarvis itself (safest — self-modify, easy revert if anything goes wrong).
    # Tiny task — bumps the API version comment in server.py so it actually has to
    # touch a file but the diff is trivially reviewable.
    body = {
        "repo": "jarvis",
        "description": (
            "In scripts/api/server.py, append the comment "
            "'# smoke-tested via /api/v1/fix on <today UTC date>' "
            "as the very last line of the file. Nothing else. "
            "Single-line change. Open as draft PR titled "
            "'chore: smoke-test marker for /api/v1/fix endpoint'."
        ),
        "max_budget_usd": 1.50,
    }

    print("POST /api/v1/fix ...")
    status, payload = _req("POST", "/api/v1/fix", body)
    print(f"  → {status}: {json.dumps(payload, indent=2)[:600]}")
    if status != 202:
        return 1
    job_id = payload["job_id"]
    status_url = payload["status_url"]

    print(f"polling {status_url} every 15s (up to 12min)...")
    deadline = time.time() + 720
    while time.time() < deadline:
        time.sleep(15)
        s, p = _req("GET", status_url)
        if s != 200:
            print(f"  ! {s}: {p}")
            return 1
        elapsed = p.get("elapsed_sec")
        print(f"  status={p['status']}  elapsed={elapsed}s  pr_url={p.get('pr_url')}")
        if p["status"] in ("completed", "failed"):
            print("\nfinal:")
            print(json.dumps(p, indent=2)[:2000])
            return 0 if p["status"] == "completed" and p.get("pr_url") else 1
    print("timed out polling")
    return 1


if __name__ == "__main__":
    sys.exit(main())
