"""Compare the freshly built service registry against the previous run.

Wired into the nightly reindex (via build_service_registry.sh). DMs Rohit on:

  1. Total service count dropped by > DROP_FRACTION (default 10%) → suggests
     indexing breakage or a mass repo-clone failure.
  2. Any service explicitly listed in the operator overrides file is MISSING
     from the new registry → suggests the source repo was deleted, renamed,
     or its config drifted past the K8s/R53 regex.
  3. Any service in a small TRACKED_SERVICES list disappeared between runs
     (high-traffic services we've documented; extend over time).

Writes nothing to git; the registry JSON is runtime data. On any of the above
conditions, posts a single Slack DM to the operator (Rohit by default,
overridable via JARVIS_OWNER_UID env var). Routes via JARVIS_ALERTS_CHANNEL
if set (defaults to operator DM per CLAUDE.md alert routing rule).

Exit codes:
  0 — no drift, or drift detected and alert sent successfully
  1 — alert needed but Slack post failed (visible in service log)
  2 — registry file missing / unreadable (precondition violation)

Run after the registry builder. Idempotent: re-running with the same input
produces the same output (no state mutated except the previous-snapshot file).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

REGISTRY_PATH = Path("/home/ubuntu/jarvis/index/service_registry.json")
PREV_SNAPSHOT_PATH = Path("/home/ubuntu/jarvis/index/service_registry.prev.json")
OVERRIDES_PATH = Path("/home/ubuntu/jarvis/scripts/service_registry_overrides.json")

DROP_FRACTION = 0.10  # alert if new count < (1 - DROP_FRACTION) * prev count

# Small list of high-traffic services we've explicitly documented / Q&A-validated.
# Disappearance from the registry is a real regression signal worth a DM.
# Extend as more services get documented (don't grow recklessly — every entry
# is a paged-alert promise).
TRACKED_SERVICES = {
    "bullet-ms",
    "lending-lifecycle-manager-ms",
    "deposit-platform-blostem",
    "deposit-manager-ms",
    "platform-auth-ms",
    "bff-core",
}


def load_registry(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"[drift] WARN: failed to parse {path}: {e}", flush=True)
        return {}


def load_overrides() -> set[str]:
    if not OVERRIDES_PATH.is_file():
        return set()
    try:
        data = json.loads(OVERRIDES_PATH.read_text())
        return set((data.get("services") or {}).keys())
    except Exception as e:
        print(f"[drift] WARN: failed to parse overrides: {e}", flush=True)
        return set()


def detect_drift(prev: dict, curr: dict, overrides: set[str]) -> list[str]:
    """Return a list of human-readable drift findings (empty = no drift)."""
    findings: list[str] = []
    prev_services = set((prev.get("services") or {}).keys())
    curr_services = set((curr.get("services") or {}).keys())
    prev_n = len(prev_services)
    curr_n = len(curr_services)

    if prev_n > 0 and curr_n < int(prev_n * (1 - DROP_FRACTION)):
        delta = prev_n - curr_n
        findings.append(
            f":chart_with_downwards_trend: service count dropped sharply: "
            f"{prev_n} → {curr_n} (−{delta}, "
            f"{(delta / prev_n * 100):.0f}% below previous nightly)"
        )

    # Tracked-service disappearances (only count what was actually in the previous run).
    tracked_lost = sorted((TRACKED_SERVICES & prev_services) - curr_services)
    if tracked_lost:
        findings.append(
            f":eye: tracked services missing from new registry: {', '.join(tracked_lost)}"
        )

    # Overrides-listed services that aren't in the new registry.
    # (overrides ARE merged into the registry by the builder, so an override
    # entry that doesn't appear in `services` means the builder skipped/failed it.)
    overrides_lost = sorted(overrides - curr_services)
    if overrides_lost:
        findings.append(
            f":pushpin: overrides entries not in registry "
            f"(builder skipped them?): {', '.join(overrides_lost)}"
        )

    return findings


def send_dm(findings: list[str], curr: dict) -> bool:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("[drift] ERROR: SLACK_BOT_TOKEN unset; cannot alert", flush=True)
        return False
    target = os.environ.get("JARVIS_ALERTS_CHANNEL") or os.environ.get("JARVIS_OWNER_UID", "U0837N31T9C")
    meta = curr.get("_meta") or {}
    n_services = len((curr.get("services") or {}))
    body_lines = [
        ":warning: *Service registry drift detected* (nightly build)",
        "",
        f"*Registry size:* {n_services} services",
        f"*Generated at:* {meta.get('generated_at_utc', 'unknown')}",
        "",
        "*Findings:*",
    ] + [f"• {f}" for f in findings] + [
        "",
        "Investigate: `ssh ubuntu@3.6.202.121` → "
        "`diff <(jq -r '.services | keys[]' /home/ubuntu/jarvis/index/service_registry.prev.json) "
        "<(jq -r '.services | keys[]' /home/ubuntu/jarvis/index/service_registry.json)`",
    ]
    payload = {
        "channel": target,
        "text": "Service registry drift detected on nightly build",
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(body_lines)}}],
    }
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[drift] ERROR: Slack post failed: {e}", flush=True)
        return False
    if not resp.get("ok"):
        print(f"[drift] ERROR: Slack returned not-ok: {resp}", flush=True)
        return False
    print(f"[drift] alert sent to {target} ({len(findings)} finding(s))", flush=True)
    return True


def main() -> int:
    curr = load_registry(REGISTRY_PATH)
    if not curr:
        print(f"[drift] ERROR: current registry missing/unreadable: {REGISTRY_PATH}", flush=True)
        return 2
    prev = load_registry(PREV_SNAPSHOT_PATH)
    overrides = load_overrides()

    if not prev:
        # First run after deploy — no prior snapshot. Just write the snapshot and exit.
        PREV_SNAPSHOT_PATH.write_text(REGISTRY_PATH.read_text())
        print("[drift] no previous snapshot — baseline saved, no alert", flush=True)
        return 0

    findings = detect_drift(prev, curr, overrides)
    if not findings:
        # Healthy — roll the snapshot forward.
        PREV_SNAPSHOT_PATH.write_text(REGISTRY_PATH.read_text())
        n = len((curr.get("services") or {}))
        print(f"[drift] no drift ({n} services); snapshot rolled forward", flush=True)
        return 0

    print(f"[drift] {len(findings)} finding(s):", flush=True)
    for f in findings:
        print(f"  - {f}", flush=True)
    ok = send_dm(findings, curr)
    # We DO roll the snapshot forward even on drift — otherwise a stable degraded
    # state would page every night. The DM is the one-shot notification; if the
    # condition persists across multiple nights, the operator already knows.
    PREV_SNAPSHOT_PATH.write_text(REGISTRY_PATH.read_text())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
