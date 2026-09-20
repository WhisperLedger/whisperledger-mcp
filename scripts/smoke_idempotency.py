"""Smoke-test idempotency-key dedup + early audit-log write."""
import json
import os
import time
import urllib.request

API_BASE = "http://127.0.0.1:8081"
API_KEY = os.environ["JARVIS_API_KEY"]
AUDIT_LOG = "/home/ubuntu/jarvis/logs/fix_audit.jsonl"

key = f"smoke-idem-{int(time.time())}"
body = {
    "repo": "jarvis",
    "description": ("noop-style test: edit scripts/api/server.py to add a single "
                    "comment line at end. Used for idempotency smoke; will be closed."),
    "max_budget_usd": 0.50,
}


def post(idem):
    req = urllib.request.Request(
        f"{API_BASE}/api/v1/fix",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "X-Jarvis-Caller": "idem-smoke",
            "Content-Type": "application/json",
            **({"Idempotency-Key": idem} if idem else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, json.loads(resp.read())


def count_received_events_for(job_id):
    if not os.path.exists(AUDIT_LOG):
        return 0
    n = 0
    with open(AUDIT_LOG) as f:
        for line in f:
            try:
                e = json.loads(line)
                if e.get("event") == "received" and e.get("job_id") == job_id:
                    n += 1
            except Exception:
                pass
    return n


print(f"Idempotency key for this run: {key}\n")

print("--- 1. POST with idempotency_key — expect new job (202) ---")
s1, p1 = post(key)
print(f"status={s1}  job_id={p1['job_id']}")
job_id_1 = p1["job_id"]

print("\n--- 2. POST again with SAME idempotency_key — expect SAME job_id back ---")
s2, p2 = post(key)
print(f"status={s2}  job_id={p2['job_id']}")
job_id_2 = p2["job_id"]
same = job_id_1 == job_id_2

print("\n--- 3. POST with different key (or none) — expect a NEW job ---")
s3, p3 = post(None)
print(f"status={s3}  job_id={p3['job_id']}")
job_id_3 = p3["job_id"]
diff = job_id_3 != job_id_1

print("\n--- 4. Verify fix_audit.jsonl has 'received' event for job_id_1 ---")
received_count_1 = count_received_events_for(job_id_1)
print(f"received events found for {job_id_1}: {received_count_1}")

print("\n========== RESULTS ==========")
results = [
    ("idempotent reuse returned same job_id", same),
    ("unkeyed POST produced different job_id", diff),
    ("early 'received' event written for job 1", received_count_1 >= 1),
]
for label, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
print()

if all(ok for _, ok in results):
    print("ALL CHECKS PASSED")
else:
    print("SOME CHECKS FAILED")

print("\nspawned jobs (these will run + open small PRs; we'll close them after):")
print(f"  - {job_id_1}  (idempotent — first POST)")
print(f"  - {job_id_3}  (unkeyed POST)")
