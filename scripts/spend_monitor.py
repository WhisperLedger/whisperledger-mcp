"""Spend monitor — detects credit-balance failures + alerts on burn thresholds.

Runs from systemd timer every 15min. Also invoked from jarvis_claudify.sh
post-run for immediate credit-failure detection.

Behavior:
1. Scans every claudify_run.log that was modified in the last 24h. If any
   contains 'Credit balance is too low' AND we haven't already alerted on that
   run, DMs Rohit immediately with run details + DMs the affected requester.
2. Sums today's spend (qa_log + claudify workspace JSON + api_requests + fix_audit).
   DMs Rohit when crossing $20 / $40 / $60 / $100 thresholds (one DM per
   threshold per IST day, idempotent via state file).

State file: ~/jarvis/state/spend_alerts.json
   {
     "ist_date": "2026-05-14",
     "thresholds_alerted": [20, 40],
     "credit_failures_alerted": ["claudify-20260514-115852-ds-jm-financial-data-service", ...]
   }
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from slack_sdk import WebClient

ROHIT_USER_ID = "U0837N31T9C"
LOGS_DIR = Path("/home/ubuntu/jarvis/logs")
WORKSPACES_DIR = Path("/home/ubuntu/jarvis/workspaces")
STATE_FILE = Path("/home/ubuntu/jarvis/state/spend_alerts.json")
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

THRESHOLDS_USD = [20, 40, 60, 100]  # DM Rohit when today's spend crosses each
IST = timezone(timedelta(hours=5, minutes=30))


def to_ist(ts_str: str):
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(IST)
    except Exception:
        return None


def is_today_ist(ts_str: str) -> bool:
    dt = to_ist(ts_str)
    return bool(dt and dt.date() == datetime.now(IST).date())


def load_state() -> dict:
    today = datetime.now(IST).date().isoformat()
    if not STATE_FILE.exists():
        return {"ist_date": today, "thresholds_alerted": [], "credit_failures_alerted": []}
    try:
        s = json.loads(STATE_FILE.read_text())
        # Reset thresholds if it's a new IST day
        if s.get("ist_date") != today:
            s = {"ist_date": today, "thresholds_alerted": [],
                 "credit_failures_alerted": s.get("credit_failures_alerted", [])}
        return s
    except Exception:
        return {"ist_date": today, "thresholds_alerted": [], "credit_failures_alerted": []}


def save_state(s: dict) -> None:
    STATE_FILE.write_text(json.dumps(s, indent=2))


# --- Spend tally -----------------------------------------------------------

P_IN, P_OUT, P_CW, P_CR = 3.00 / 1e6, 15.00 / 1e6, 3.75 / 1e6, 0.30 / 1e6


def cost_from_tokens(r: dict) -> float:
    return ((r.get("input_tokens") or 0) * P_IN
            + (r.get("output_tokens") or 0) * P_OUT
            + (r.get("cache_creation_tokens") or 0) * P_CW
            + (r.get("cache_read_tokens") or 0) * P_CR)


def tally_spend_today() -> dict:
    qa = claudify = api = fix = 0.0
    # Q&A
    qa_log = LOGS_DIR / "qa_log.jsonl"
    if qa_log.exists():
        for line in qa_log.open():
            try:
                r = json.loads(line)
                if is_today_ist(r.get("ts", "")):
                    qa += cost_from_tokens(r)
            except Exception:
                pass
    # Claudify (from workspace cost JSONs)
    audit = LOGS_DIR / "claudify_audit.jsonl"
    if audit.exists():
        for line in audit.open():
            try:
                e = json.loads(line)
                if e.get("event") == "success" and is_today_ist(e.get("ts", "")):
                    cf = WORKSPACES_DIR / e["task_id"] / f"{e['repo']}_costs.json"
                    if cf.exists():
                        try:
                            claudify += json.load(cf.open()).get("total_cost_usd") or 0
                        except Exception:
                            pass
            except Exception:
                pass
    # API
    api_log = LOGS_DIR / "api_requests.jsonl"
    if api_log.exists():
        for line in api_log.open():
            try:
                r = json.loads(line)
                if is_today_ist(r.get("ts", "")):
                    api += r.get("cost_usd", 0) or 0
            except Exception:
                pass
    # Fix-mode (no cost data yet — placeholder)
    return {"qa": qa, "claudify": claudify, "api": api, "fix": fix,
            "total": qa + claudify + api + fix}


# --- Credit-failure detection ----------------------------------------------

def find_credit_failures() -> list[dict]:
    """Scan claudify run logs from the last 48h for 'Credit balance is too low'.
    Returns list of {task_id, repo, requester, log_path, count}."""
    out = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    audit = LOGS_DIR / "claudify_audit.jsonl"
    # Map task_id → requester from audit
    task_meta = {}
    if audit.exists():
        for line in audit.open():
            try:
                e = json.loads(line)
                tid = e.get("task_id")
                if tid and tid not in task_meta:
                    task_meta[tid] = {
                        "requester": e.get("requester", "?"),
                        "repo": e.get("repo", "?"),
                        "ts": e.get("ts", ""),
                    }
            except Exception:
                pass
    # Walk workspace dirs
    if not WORKSPACES_DIR.exists():
        return out
    for d in WORKSPACES_DIR.glob("claudify-*"):
        log = d / "claudify_run.log"
        if not log.exists():
            continue
        try:
            mtime = datetime.fromtimestamp(log.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                continue
            text = log.read_text(errors="replace")
            count = text.count("Credit balance is too low")
            if count > 0:
                tid = d.name
                meta = task_meta.get(tid, {"requester": "?", "repo": tid.split("-", 3)[-1] if "-" in tid else "?", "ts": mtime.isoformat()})
                out.append({
                    "task_id": tid,
                    "repo": meta["repo"],
                    "requester": meta["requester"],
                    "log_path": str(log),
                    "ts": meta["ts"],
                    "credit_failure_count": count,
                })
        except Exception:
            pass
    return out


# --- Slack DMs --------------------------------------------------------------

def make_client() -> WebClient | None:
    tok = os.environ.get("SLACK_BOT_TOKEN")
    if not tok:
        return None
    return WebClient(token=tok)


def dm(client: WebClient, user_id: str, text: str) -> None:
    try:
        convo = client.conversations_open(users=user_id)
        ch = convo["channel"]["id"]
        client.chat_postMessage(channel=ch, text=text, mrkdwn=True, unfurl_links=False)
    except Exception as e:
        print(f"  [warn] DM to {user_id} failed: {e}", file=sys.stderr)


def alert_credit_failure(client: WebClient, failure: dict) -> None:
    when_ist = "?"
    try:
        dt = to_ist(failure["ts"])
        if dt:
            when_ist = dt.strftime("%H:%M IST")
    except Exception:
        pass
    msg = (f":rotating_light: *Anthropic credit failure detected*\n"
           f"*Engineer:* {failure['requester']}\n"
           f"*Repo:* `{failure['repo']}`\n"
           f"*Time:* {when_ist}\n"
           f"*Failures in this run:* {failure['credit_failure_count']}\n"
           f"*Log:* `{failure['log_path']}`\n\n"
           f"Anthropic API balance is too low to continue. Top up at "
           f"https://console.anthropic.com/settings/billing — then ping the affected engineer to retry.\n\n"
           f"_(Auto-DM from jarvis spend_monitor; one alert per run, ever.)_")
    dm(client, ROHIT_USER_ID, msg)


def alert_threshold_crossed(client: WebClient, threshold: int, spend: dict) -> None:
    msg = (f":warning: *Daily Anthropic spend crossed ${threshold}*\n"
           f"Today's burn so far (IST):\n"
           f"  • Q&A: ${spend['qa']:.2f}\n"
           f"  • Claudify: ${spend['claudify']:.2f}\n"
           f"  • HTTP API: ${spend['api']:.2f}\n"
           f"  • *Total: ${spend['total']:.2f}*\n\n"
           f"_(Auto-DM from jarvis spend_monitor; one alert per threshold per IST day.)_")
    dm(client, ROHIT_USER_ID, msg)


# --- Main ------------------------------------------------------------------

def main():
    client = make_client()
    if not client:
        print("ERROR: SLACK_BOT_TOKEN not set", file=sys.stderr)
        return 1
    state = load_state()
    print(f"State: {state}")

    # 1. Credit-failure detection
    failures = find_credit_failures()
    new_failures = [f for f in failures if f["task_id"] not in state["credit_failures_alerted"]]
    print(f"Total credit failures found in last 48h: {len(failures)}")
    print(f"NEW (not yet alerted): {len(new_failures)}")
    for f in new_failures:
        print(f"  → DM Rohit about credit failure in {f['task_id']}")
        alert_credit_failure(client, f)
        state["credit_failures_alerted"].append(f["task_id"])

    # 2. Threshold-crossing
    spend = tally_spend_today()
    print(f"Today's spend (IST): {spend}")
    for thr in THRESHOLDS_USD:
        if spend["total"] >= thr and thr not in state["thresholds_alerted"]:
            print(f"  → DM Rohit: crossed ${thr}")
            alert_threshold_crossed(client, thr, spend)
            state["thresholds_alerted"].append(thr)

    save_state(state)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
