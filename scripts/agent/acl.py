"""ACL for restricted Qdrant collections + restricted repos.

Design:
- Config lives at ~/.config/jarvis/restricted_acl.json (or JARVIS_ACL_FILE).
- Every restricted collection lists (a) the repos it holds and (b) its
  allowlists — both slack_users and emails.
- Retrieval callers set the ACL contextvar via set_caller(caller_id) at the
  top of the request; every downstream retrieval tool reads it via get_caller().
- Fail-closed: unknown / missing caller sees only the public collection and
  cannot open restricted repos on disk.

Caller-id shape (matches what agent.ask already receives):
    'slack:<slack_user_id>'      — Slack surface
    '<email>'                    — HTTP portal engineer, MCP per-engineer token
    'api:anonymous', None        — service caller (Jove/SRE/JPE) -> public only

Never leak repo names in error messages when the caller can't see them —
that itself is metadata about who is on the allowlist.
"""
from __future__ import annotations

import contextvars
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

# Public collection name — hard-coded to avoid a cycle with indexer.config.
PUBLIC_COLLECTION = 'jarvis_code'

_ACL_PATH = Path(
    os.environ.get(
        'JARVIS_ACL_FILE',
        str(Path.home() / '.config' / 'jarvis' / 'restricted_acl.json'),
    )
)

_AUDIT_LOG = Path.home() / 'jarvis' / 'logs' / 'restricted_access.jsonl'

_ACL_CACHE: dict | None = None
_ACL_LOCK = threading.Lock()

_CURRENT_CALLER: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    '_JARVIS_ACL_CALLER', default=None,
)


def _load() -> dict:
    global _ACL_CACHE
    with _ACL_LOCK:
        if _ACL_CACHE is not None:
            return _ACL_CACHE
        try:
            _ACL_CACHE = json.loads(_ACL_PATH.read_text())
        except FileNotFoundError:
            _ACL_CACHE = {'restricted_collections': {}}
        except Exception:
            _ACL_CACHE = {'restricted_collections': {}}
        return _ACL_CACHE


def reload() -> None:
    global _ACL_CACHE
    with _ACL_LOCK:
        _ACL_CACHE = None


def set_caller(caller_id: str | None) -> contextvars.Token:
    return _CURRENT_CALLER.set(caller_id)


def reset_caller(token: contextvars.Token) -> None:
    _CURRENT_CALLER.reset(token)


def get_caller() -> str | None:
    return _CURRENT_CALLER.get()


def _match(caller_id: str | None, allowlist: dict) -> bool:
    if not caller_id:
        return False
    slack_users = set(allowlist.get('slack_users') or [])
    emails = set(allowlist.get('emails') or [])
    if caller_id.startswith('slack:'):
        return caller_id[len('slack:'):] in slack_users
    return caller_id in emails


def collections_for_caller(caller_id: str | None) -> list[str]:
    out = [PUBLIC_COLLECTION]
    for name, acl in (_load().get('restricted_collections') or {}).items():
        if _match(caller_id, acl):
            out.append(name)
    return out


def restricted_repos_visible_to(caller_id: str | None) -> set[str]:
    out: set[str] = set()
    for _name, acl in (_load().get('restricted_collections') or {}).items():
        if _match(caller_id, acl):
            out.update(acl.get('repos') or [])
    return out


def all_restricted_repos() -> set[str]:
    out: set[str] = set()
    for _name, acl in (_load().get('restricted_collections') or {}).items():
        out.update(acl.get('repos') or [])
    return out


def collection_for_repo(repo: str) -> str:
    for name, acl in (_load().get('restricted_collections') or {}).items():
        if repo in (acl.get('repos') or []):
            return name
    return PUBLIC_COLLECTION


def can_access_repo(repo: str, caller_id: str | None = None) -> bool:
    if repo not in all_restricted_repos():
        return True
    caller_id = caller_id if caller_id is not None else get_caller()
    return repo in restricted_repos_visible_to(caller_id)


def audit_restricted_access(
    caller_id: str | None,
    tool: str,
    collection: str | None = None,
    repo: str | None = None,
    hits: list[dict] | None = None,
    denied: bool = False,
    reason: str | None = None,
) -> None:
    try:
        _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOG.open('a') as f:
            f.write(
                json.dumps({
                    'ts': datetime.now(timezone.utc).isoformat(),
                    'caller': caller_id,
                    'tool': tool,
                    'collection': collection,
                    'repo': repo,
                    'denied': denied,
                    'reason': reason,
                    'hits': [
                        {'repo': h.get('repo'), 'path': h.get('path'),
                         'score': h.get('score')}
                        for h in (hits or [])[:20]
                    ] if hits else None,
                }) + '\n'
            )
    except Exception:
        pass
