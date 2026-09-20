"""Smoke test for POST /api/v1/pr/iterate against PR #14141 (RECO-133)."""
import json
import os
import time
import urllib.request

API_BASE = "http://127.0.0.1:8081"
API_KEY = os.environ["JARVIS_API_KEY"]

body = {
    "repo": "jupiter",
    "pr_number": 14141,
    "max_budget_usd": 3.0,
}

print("POST /api/v1/pr/iterate ...")
req = urllib.request.Request(
    f"{API_BASE}/api/v1/pr/iterate",
    data=json.dumps(body).encode("utf-8"),
    method="POST",
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "X-Jarvis-Caller": "iterate-smoke",
        "Idempotency-Key": f"iterate-smoke-{int(time.time())}",
        "Content-Type": "application/json",
    },
)
with urllib.request.urlopen(req, timeout=30) as resp:
    ack = json.loads(resp.read())
print(json.dumps(ack, indent=2))
job_id = ack["job_id"]
status_url = ack["status_url"]

print(f"\npolling {status_url} every 30s, up to 25 min...")
deadline = time.time() + 1500
while time.time() < deadline:
    time.sleep(30)
    r = urllib.request.Request(
        f"{API_BASE}{status_url}",
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            st = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  poll HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}")
        continue
    print(f"  status={st['status']} elapsed={st.get('elapsed_sec')}s pr_url={st.get('pr_url')}")
    if st["status"] in ("completed", "failed"):
        print("\n--- final state ---")
        print(json.dumps(st, indent=2)[:3000])
        break
