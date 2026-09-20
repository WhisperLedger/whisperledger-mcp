#!/usr/bin/env python3
"""Nightly Jove Confluence refresh + drift monitor.

For each critical Confluence space:
  1. Trigger jove_client.refresh_space()
  2. Poll status until completed / failed / per-space timeout
  3. Record outcome in ~/jarvis/logs/jove_refresh.jsonl
  4. After all spaces done, check drift: latest_modified vs latest_indexed_at

If any refresh fails OR any space has drift > 48h, DM Rohit.

Spaces default: TECH, PROD. Override via env: JARVIS_JOVE_REFRESH_SPACES="TECH,PROD,DS,BP".
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/home/ubuntu/jarvis/scripts')
from agent import jove_client

LOG_PATH = Path('/home/ubuntu/jarvis/logs/jove_refresh.jsonl')
ROHIT_USER_ID = 'U0837N31T9C'
DEFAULT_SPACES = ['TECH', 'PROD']
PER_SPACE_TIMEOUT_SEC = 3 * 3600  # 3 hours per space — TECH full crawl at ~4500 pages takes ~2h+
POLL_INTERVAL_SEC = 15
POLL_MAX_TRANSIENT_ERRORS = 5  # give up after this many consecutive poll_errors on the same space
DRIFT_ALERT_HOURS = 48

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def _emit(record: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open('a') as f:
        f.write(json.dumps(record) + '\n')
    print(json.dumps(record), flush=True)

BUSY_RETRY_INTERVAL_SEC = 60
BUSY_RETRY_MAX_MIN = 30  # give up trying to acquire a slot after this long

def _acquire_run_id(space_key: str) -> tuple[str | None, str | None]:
    'Trigger a refresh, retrying while Jove reports it is busy with another indexer task.'
    deadline = time.time() + BUSY_RETRY_MAX_MIN * 60
    while True:
        try:
            trig = jove_client.refresh_space(space_key)
        except BaseException as e:
            cause = e.exceptions[0] if hasattr(e, 'exceptions') and e.exceptions else e
            return None, f'trigger_error: {type(cause).__name__}: {cause!r}'
        rid = (trig or {}).get('run_id')
        if rid:
            return rid, None
        # No run_id — likely 'already_running' at the Jove global-lock level.
        status = (trig or {}).get('status')
        note = (trig or {}).get('note', '')
        if status != 'already_running':
            return None, f'no_run_id: {trig}'
        if time.time() >= deadline:
            return None, f'busy_timeout: {note}'
        print(f'  Jove busy, backing off {BUSY_RETRY_INTERVAL_SEC}s (note: {note})', flush=True)
        time.sleep(BUSY_RETRY_INTERVAL_SEC)

def refresh_one(space_key: str) -> dict:
    started_at = _now_iso()
    print(f'[{started_at}] refreshing {space_key}...', flush=True)
    run_id, err = _acquire_run_id(space_key)
    if not run_id:
        return {'space': space_key, 'ok': False, 'reason': err,
                'started_at': started_at, 'finished_at': _now_iso()}
    deadline = time.time() + PER_SPACE_TIMEOUT_SEC
    last_st = None
    consecutive_poll_errors = 0
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SEC)
        try:
            st = jove_client.get_run_status(run_id) or {}
            consecutive_poll_errors = 0
        except BaseException as e:
            # Python 3.11 asyncio.TaskGroup wraps exceptions in ExceptionGroup.
            # Unwrap to a useful message; also treat transient poll errors as
            # retryable — Jove's refresh is likely still progressing server-side.
            cause = e
            if hasattr(e, 'exceptions') and e.exceptions:
                cause = e.exceptions[0]
            consecutive_poll_errors += 1
            print(f'  poll error #{consecutive_poll_errors} for {space_key}: {type(cause).__name__}: {cause!r}', flush=True)
            if consecutive_poll_errors < POLL_MAX_TRANSIENT_ERRORS:
                time.sleep(POLL_INTERVAL_SEC * 2)  # back off before retry
                continue
            return {'space': space_key, 'ok': False,
                    'reason': f'poll_error_persistent: {type(cause).__name__}: {cause!r}',
                    'run_id': run_id, 'last_status': last_st,
                    'started_at': started_at, 'finished_at': _now_iso()}
        last_st = st
        status = st.get('status')
        if status in ('completed', 'succeeded', 'success'):
            return {'space': space_key, 'ok': True, 'run_id': run_id,
                    'pages_discovered': st.get('pages_discovered'),
                    'pages_indexed': st.get('pages_indexed'),
                    'pages_failed': st.get('pages_failed'),
                    'started_at': started_at, 'finished_at': _now_iso()}
        if status in ('failed', 'error'):
            return {'space': space_key, 'ok': False, 'reason': f'jove_status={status}',
                    'run_id': run_id, 'jove_error': st.get('error_summary'),
                    'started_at': started_at, 'finished_at': _now_iso()}
    return {'space': space_key, 'ok': False, 'reason': 'timeout', 'run_id': run_id,
            'last_status': last_st, 'started_at': started_at, 'finished_at': _now_iso()}

def _parse_iso(s):
    if not s: return None
    try: return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception: return None

def drift_check(spaces: list[str]) -> list[dict]:
    'Return list of drifted spaces (latest_modified newer than latest_indexed_at by > DRIFT_ALERT_HOURS).'
    all_spaces = jove_client.list_spaces(force_refresh=True)
    now = datetime.now(timezone.utc)
    drifted = []
    for sk in spaces:
        meta = all_spaces.get(sk) or {}
        mod = _parse_iso(meta.get('latest_modified'))
        idx = _parse_iso(meta.get('latest_indexed_at'))
        if not mod or not idx:
            continue
        drift_h = (mod - idx).total_seconds() / 3600
        if drift_h > DRIFT_ALERT_HOURS:
            drifted.append({'space': sk, 'latest_modified': meta.get('latest_modified'),
                            'latest_indexed_at': meta.get('latest_indexed_at'),
                            'drift_hours': round(drift_h, 1),
                            'page_count': meta.get('page_count')})
    return drifted

def maybe_alert(results: list[dict], drifted: list[dict]) -> None:
    failures = [r for r in results if not r['ok']]
    if not failures and not drifted:
        return
    try:
        from slack_sdk import WebClient
        client = WebClient(token=os.environ['SLACK_BOT_TOKEN'])
    except Exception as e:
        print(f'[warn] slack client init failed: {e}', file=sys.stderr)
        return
    lines = [':warning: *Jove nightly refresh anomaly*']
    if failures:
        lines.append('*Refresh failures:*')
        for f in failures:
            lines.append(f"• `{f['space']}` — {f.get('reason','?')}")
    if drifted:
        lines.append('*Drift detected (latest_modified newer than latest_indexed_at):*')
        for d in drifted:
            lines.append(f"• `{d['space']}` — drift {d['drift_hours']}h  |  mod={d['latest_modified']}  |  idx={d['latest_indexed_at']}")
    lines.append('')
    lines.append('Full log: `~/jarvis/logs/jove_refresh.jsonl`')
    msg = '\n'.join(lines)
    channel = os.environ.get('JARVIS_ALERTS_CHANNEL')
    try:
        if channel:
            client.chat_postMessage(channel=channel, text=msg, mrkdwn=True, unfurl_links=False)
        else:
            convo = client.conversations_open(users=ROHIT_USER_ID)
            client.chat_postMessage(channel=convo['channel']['id'], text=msg, mrkdwn=True, unfurl_links=False)
    except Exception as e:
        print(f'[warn] alert DM failed: {e}', file=sys.stderr)

def main() -> int:
    spaces_env = os.environ.get('JARVIS_JOVE_REFRESH_SPACES', '').strip()
    spaces = [s.strip().upper() for s in spaces_env.split(',') if s.strip()] or DEFAULT_SPACES
    run_started = _now_iso()
    _emit({'event': 'run_start', 'ts': run_started, 'spaces': spaces})
    results = []
    for sk in spaces:
        r = refresh_one(sk)
        r['event'] = 'space_result'
        _emit(r)
        results.append(r)
    drifted = drift_check(spaces)
    _emit({'event': 'run_end', 'ts': _now_iso(), 'run_started': run_started,
           'results_summary': [(r['space'], r['ok']) for r in results],
           'drifted': drifted})
    maybe_alert(results, drifted)
    # Non-zero exit iff any refresh failed — systemd OnFailure will pick it up
    return 1 if any(not r['ok'] for r in results) else 0

if __name__ == '__main__':
    sys.exit(main())
