"""Smoke-test the callback feature on /api/v1/fix.

Fires a tiny fix on the jarvis repo (small budget, append a comment marker),
points callback_url at httpbin.org/post (which echoes any POST back), then
polls until terminal AND verifies the callback was logged in fix_callbacks.jsonl.
"""
import json
import os
import time
import urllib.request

API_BASE = "http://127.0.0.1:8081"
API_KEY = os.environ["JARVIS_API_KEY"]
CALLBACK_LOG = "/home/ubuntu/jarvis/logs/fix_callbacks.jsonl"

body = {
    "repo": "jarvis",
    "description": (
        "In scripts/api/server.py append the comment "
        "'# callback-smoke-tested 2026-05-18' as the very last line. "
        "Single-line change. Open as draft PR titled "
        "'chore: smoke-test marker for /api/v1/fix callback'."
    ),
    "max_budget_usd": 1.0,
    "callback_url": "https://httpbin.org/post",
}

print("POST /api/v1/fix with callback_url=httpbin.org/post ...")
req = urllib.request.Request(
    f"{API_BASE}/api/v1/fix",
    data=json.dumps(body).encode("utf-8"),
    method="POST",
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "X-Jarvis-Caller": "callback-smoke",
        "Content-Type": "application/json",
    },
)
with urllib.request.urlopen(req, timeout=30) as resp:
    ack = json.loads(resp.read())
print(json.dumps(ack, indent=2))
job_id = ack["job_id"]

# Snapshot the callback log BEFORE so we can detect the new entry
before_count = 0
if os.path.exists(CALLBACK_LOG):
    with open(CALLBACK_LOG) as f:
        before_count = sum(1 for _ in f)
print(f"\ncallback log line count before run: {before_count}")

print(f"\npolling /api/v1/fix/{job_id} every 20s, up to 8 min...")
deadline = time.time() + 480
while time.time() < deadline:
    time.sleep(20)
    r = urllib.request.Request(
        f"{API_BASE}/api/v1/fix/{job_id}",
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    with urllib.request.urlopen(r, timeout=10) as resp:
        st = json.loads(resp.read())
    print(f"  status={st['status']} elapsed={st.get('elapsed_sec')}s pr_url={st.get('pr_url')}")
    if st["status"] in ("completed", "failed"):
        break

# Give the callback a few seconds to fire after the job terminates
time.sleep(5)

print(f"\n--- callback log entries after run ---")
if os.path.exists(CALLBACK_LOG):
    with open(CALLBACK_LOG) as f:
        lines = f.readlines()
    new_lines = lines[before_count:]
    print(f"new entries: {len(new_lines)}")
    for line in new_lines:
        print(json.dumps(json.loads(line), indent=2))
else:
    print("(callback log does not exist — feature may not have fired)")
