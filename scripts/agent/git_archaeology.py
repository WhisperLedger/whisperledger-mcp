"""Git archaeology tool — given a file:line, narrate the history.

Combines git blame + the PR that introduced the change + linked Jira ticket
+ reviewer comments into a single narrative. Answers the "why is this code
the way it is?" question that engineers ask all day.

Usage from agent.tools (registered as `why_was_this_changed`):
  why_was_this_changed(repo, path, line=NN)
returns markdown narrative + structured fields.
"""
from __future__ import annotations

import json
import re
import subprocess
from .repo_names import resolve_gh_org
from pathlib import Path

REPOS_DIR = Path("/home/ubuntu/jarvis/repos")
JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]+-[0-9]+)\b")


def _safe_run(cmd: list[str], cwd: str | None = None, timeout: int = 15) -> str:
    try:
        return subprocess.check_output(cmd, cwd=cwd, text=True, timeout=timeout,
                                       stderr=subprocess.DEVNULL)
    except Exception:
        return ""


def why_was_this_changed(repo: str, path: str, line: int | None = None) -> str:
    """Return JSON-string narrative about why a file:line is the way it is.

    Combines git blame (last commit that touched the line), the PR that
    introduced that commit, linked Jira ticket(s), and reviewer comments.
    """
    repo_path = REPOS_DIR / repo
    if not (repo_path / ".git").is_dir():
        return json.dumps({"error": f"repo not cloned: {repo}"})
    full_path = (repo_path / path).resolve()
    if not str(full_path).startswith(str(repo_path.resolve()) + "/"):
        return json.dumps({"error": "path traversal"})
    if not full_path.is_file():
        return json.dumps({"error": f"file not found: {repo}/{path}"})

    cwd = str(repo_path)

    # 1. Get the relevant commit. With line: blame that line. Without line:
    # last commit that touched the file.
    if line:
        blame = _safe_run(
            ["git", "blame", "-L", f"{line},{line}", "--porcelain", "--", path],
            cwd=cwd,
        )
        sha = blame.split(" ", 1)[0].strip() if blame else ""
    else:
        sha = _safe_run(["git", "log", "-1", "--format=%H", "--", path], cwd=cwd).strip()
    if not sha:
        return json.dumps({"error": "no commit found for that file/line",
                           "repo": repo, "path": path, "line": line})

    # 2. Commit metadata.
    commit_subj = _safe_run(["git", "log", "-1", "--format=%s", sha], cwd=cwd).strip()
    commit_body = _safe_run(["git", "log", "-1", "--format=%b", sha], cwd=cwd).strip()
    commit_author = _safe_run(["git", "log", "-1", "--format=%an <%ae>", sha], cwd=cwd).strip()
    commit_date = _safe_run(["git", "log", "-1", "--format=%ai", sha], cwd=cwd).strip()

    # 3. Identify the PR that introduced this commit. Use gh api.
    pr_meta = None
    pr_search = _safe_run(
        ["gh", "api", (lambda o=resolve_gh_org(repo): f"repos/{o[0]}/{o[1]}/commits/{sha}/pulls")()],
        cwd=cwd,
    )
    try:
        prs = json.loads(pr_search) if pr_search else []
        if isinstance(prs, list) and prs:
            p = prs[0]
            pr_meta = {
                "number": p.get("number"),
                "title": p.get("title"),
                "url": p.get("html_url"),
                "author": (p.get("user") or {}).get("login"),
                "merged_at": p.get("merged_at"),
                "body": (p.get("body") or "")[:1500],
            }
    except Exception:
        pass

    # 4. Find linked Jira tickets (in commit subject, body, or PR body).
    jira_keys = set(JIRA_KEY_RE.findall(commit_subj))
    jira_keys.update(JIRA_KEY_RE.findall(commit_body))
    if pr_meta:
        jira_keys.update(JIRA_KEY_RE.findall(pr_meta.get("title", "") or ""))
        jira_keys.update(JIRA_KEY_RE.findall(pr_meta.get("body", "") or ""))

    # 5. PR review comments — the WHY usually lives here.
    review_comments = []
    if pr_meta:
        pr_num = pr_meta["number"]
        reviews_raw = _safe_run(
            ["gh", "api", (lambda o=resolve_gh_org(repo): f"repos/{o[0]}/{o[1]}/pulls/{pr_num}/reviews")(),
             "--paginate"],
            cwd=cwd,
        )
        try:
            reviews = json.loads(reviews_raw) if reviews_raw else []
            for r in reviews:
                body_txt = (r.get("body") or "").strip()
                if body_txt:
                    review_comments.append({
                        "author": (r.get("user") or {}).get("login"),
                        "state": r.get("state"),
                        "body": body_txt[:500],
                    })
        except Exception:
            pass

    # 6. Permalink — commit-pinned.
    _org, _upstream = resolve_gh_org(repo)
    permalink = f"https://github.com/{_org}/{_upstream}/blob/{sha}/{path}"
    if line:
        permalink += f"#L{line}"

    return json.dumps({
        "repo": repo, "path": path, "line": line,
        "commit": {
            "sha": sha[:12],
            "subject": commit_subj,
            "body": commit_body[:1500],
            "author": commit_author,
            "date": commit_date,
        },
        "pr": pr_meta,
        "jira_tickets": sorted(jira_keys),
        "review_comments": review_comments[:8],
        "permalink": permalink,
    }, ensure_ascii=False)
