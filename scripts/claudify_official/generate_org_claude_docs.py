#!/usr/bin/env python3
"""
generate_org_claude_docs.py

For every repository in a GitHub organisation:
  1. Clones the repo into a temporary directory.
  2. Creates a branch `base-claude-auto-generate`.
  3. Runs generate_claude_docs.py to write CLAUDE.md files.
  4. If CLAUDE.md files were produced, commits, pushes, and opens a PR.
  5. Cleans up the clone.

Usage:
    python generate_org_claude_docs.py jupitermoney [options]

Requirements:
    - gh CLI installed and authenticated  (gh auth status)
    - git CLI available globally
    - claude CLI installed and authenticated
    - generate_claude_docs.py and claude-prompt.md in the same directory as
      this script (or supply paths via --script / --prompt-file)
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────

BRANCH_NAME = "add-claude-md-docs"
DEFAULT_THRESHOLD = 50_000
DEFAULT_INNER_PARALLELISM = 10  # passed to generate_claude_docs.py --parallel

PR_TITLE = "Project Claudify: added CLAUDE.md files"

# Generation model label (for display in the PR)
GENERATION_MODEL = "claude-sonnet-4-6"


def _fmt_duration(ms: int) -> str:
    """Format milliseconds into a human-readable duration string."""
    total_s = int(ms / 1000)
    minutes, seconds = divmod(total_s, 60)
    return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"


def _build_pr_body(
    repo_name: str,
    n_files: int,
    wall_clock_secs: float,
    cost_data: dict,
) -> str:
    """
    Build a punchy PR description with Context, Changes, and generation details.

    n_files         — total CLAUDE.md files added (includes root).
    wall_clock_secs — elapsed wall-clock seconds for the full generate run.
    cost_data       — dict with keys 'paths', 'total_cost_usd', 'total_duration_ms'
                      as written by generate_claude_docs.py --costs-output.
                      May be empty if the costs file was not produced.
    """
    wc_min, wc_sec = divmod(int(wall_clock_secs), 60)
    wc_str = f"{wc_min}m {wc_sec}s" if wc_min else f"{wc_sec}s"

    total_cost_usd   = cost_data.get("total_cost_usd", 0.0)
    total_duration_ms = cost_data.get("total_duration_ms", 0)
    path_records: list[dict] = cost_data.get("paths", [])

    # ── Per-path breakdown table ──────────────────────────────────────────────
    path_table = ""
    if path_records:
        rows = "\n".join(
            f"| `{r['path']}` | ${r['cost_usd']:.4f} | {_fmt_duration(r['duration_ms'])} |"
            for r in sorted(path_records, key=lambda r: r["path"])
        )
        path_table = f"""
### Per-path breakdown

| Path | Cost | API duration |
|------|------|-------------|
{rows}
"""

    # ── Cost / duration summary ───────────────────────────────────────────────
    if cost_data:
        cost_line   = f"${total_cost_usd:.4f}"
        api_dur_line = _fmt_duration(total_duration_ms)
    else:
        cost_line    = "_unavailable_"
        api_dur_line = "_unavailable_"

    return f"""\
## Context

**Project Claudify** — equipping `{repo_name}` with machine-readable `CLAUDE.md` context \
files so Claude Code and MCP servers have full spatial awareness, network topology, and \
coding conventions from day one. No more re-explaining the repo on every AI session.

## Changes

- Adds `CLAUDE.md` to **{n_files} location(s)**: per-module files covering local setup, \
topology, integrations, and coding guidelines; plus a root file with the full repo map
- Files were generated automatically by `generate_claude_docs.py` using the \
`{GENERATION_MODEL}` model

## Review Checklist

- [ ] Scan for accuracy — network topology, task queues, exposed APIs
- [ ] Confirm no secrets, credentials, or internal hostnames leaked into generated content
- [ ] Redact or remove any inaccurate sections before merging

## Generation Details

| Field | Value |
|-------|-------|
| Model | `{GENERATION_MODEL}` |
| Wall-clock duration | {wc_str} |
| Total API cost | {cost_line} |
| Total API duration | {api_dur_line} |
{path_table}
🤖 Generated with [Claude Code](https://claude.com/claude-code)
"""


# ──────────────────────────────────────────────────────────────
# Shell helpers
# ──────────────────────────────────────────────────────────────

def run(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command, capture stdout/stderr, raise CalledProcessError on failure."""
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


def gh_api_paginated(path: str) -> list[dict]:
    """Fetch all pages from a gh api GET endpoint and return the combined list."""
    results = []
    page = 1
    while True:
        r = run(["gh", "api", f"{path}?per_page=100&page={page}"])
        batch = json.loads(r.stdout)
        if not batch:
            break
        results.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return results


def get_repo_team(full_name: str) -> str:
    """
    Fetch the 'Team' custom property from GitHub repository properties.
    Returns the team name, or 'Unknown' if the property is not set or the
    API call fails.
    """
    try:
        r = run(["gh", "api", f"repos/{full_name}/properties/values"], check=False)
        if r.returncode != 0:
            return "Unknown"
        props = json.loads(r.stdout)
        if isinstance(props, list):
            for prop in props:
                if prop.get("property_name") == "Team":
                    val = prop.get("value")
                    return val if val else "Unknown"
        return "Unknown"
    except (json.JSONDecodeError, KeyError, TypeError):
        return "Unknown"


# ──────────────────────────────────────────────────────────────
# Per-repo steps
# ──────────────────────────────────────────────────────────────

def clone_repo(full_name: str, dest: Path) -> bool:
    """Shallow-clone via `gh repo clone` (handles auth automatically)."""
    try:
        run(["gh", "repo", "clone", full_name, str(dest), "--", "--depth=1"])
        return True
    except subprocess.CalledProcessError as e:
        print(f"    [error] clone failed:\n{e.stderr}", file=sys.stderr)
        return False


def branch_exists_remotely(repo_dir: Path) -> bool:
    r = run(
        ["git", "ls-remote", "--heads", "origin", BRANCH_NAME],
        cwd=str(repo_dir),
        check=False,
    )
    return r.returncode == 0 and BRANCH_NAME in r.stdout


def create_branch(repo_dir: Path) -> bool:
    try:
        run(["git", "checkout", "-b", BRANCH_NAME], cwd=str(repo_dir))
        return True
    except subprocess.CalledProcessError as e:
        print(f"    [error] branch creation failed:\n{e.stderr}", file=sys.stderr)
        return False


def run_generate(
    script: Path,
    prompt_file: Path,
    root_prompt_file: Path,
    repo_dir: Path,
    threshold: int,
    parallelism: int,
    skip_root: bool,
    team: str = "Unknown",
    costs_output: Path | None = None,
    dry_run: bool = False,
    hybrid_prompt_file: Path | None = None,
) -> bool:
    """
    Run generate_claude_docs.py for repo_dir.
    Uses -u flag for unbuffered Python output so logs stream live.
    Returns True if the script exited 0.
    """
    cmd = [
        sys.executable, "-u", str(script),
        str(repo_dir),
        "--threshold", str(threshold),
        "--parallel", str(parallelism),
        "--prompt-file", str(prompt_file),
        "--root-prompt-file", str(root_prompt_file),
        "--team", team,
    ]
    if hybrid_prompt_file:
        cmd.extend(["--hybrid-prompt-file", str(hybrid_prompt_file)])
    if skip_root:
        cmd.append("--skip-root-update")
    if costs_output:
        cmd.extend(["--costs-output", str(costs_output)])
    if dry_run:
        cmd.append("--dry-run")
    result = subprocess.run(cmd)   # no capture — live output
    return result.returncode == 0


def _find_claude_md_files(repo_dir: Path) -> list[Path]:
    """Return all CLAUDE.md files under repo_dir, excluding .git/."""
    return [
        p for p in repo_dir.rglob("CLAUDE.md")
        if ".git" not in p.parts
    ]


def any_changes(repo_dir: Path) -> bool:
    r = run(["git", "status", "--porcelain"], cwd=str(repo_dir))
    if r.stdout.strip():
        return True
    # CLAUDE.md may be gitignored — check for the files directly.
    return bool(_find_claude_md_files(repo_dir))


def count_claude_md_files(repo_dir: Path) -> int:
    """Count how many CLAUDE.md files exist in the working tree."""
    return len(_find_claude_md_files(repo_dir))


def commit_and_push(repo_dir: Path) -> bool:
    try:
        # Force-add CLAUDE.md files so they are committed even if gitignored.
        claude_files = [
            str(p.relative_to(repo_dir)) for p in _find_claude_md_files(repo_dir)
        ]
        if claude_files:
            run(["git", "add", "--force", "--"] + claude_files, cwd=str(repo_dir))
        # Also stage any other non-ignored changes (e.g. updated existing files).
        run(["git", "add", "."], cwd=str(repo_dir))
        run(
            ["git", "commit", "-m",
             "chore: add AI-assistant context files (CLAUDE.md)\n\n"
             "Generated by generate_claude_docs.py"],
            cwd=str(repo_dir),
        )
        run(["git", "push", "-u", "origin", BRANCH_NAME], cwd=str(repo_dir))
        return True
    except subprocess.CalledProcessError as e:
        print(f"    [error] commit/push failed:\n{e.stderr}", file=sys.stderr)
        return False


def ensure_label(full_name: str, label: str) -> None:
    """Create the label in the repo if it doesn't already exist."""
    r = run(["gh", "label", "list", "--repo", full_name, "--json", "name"], check=False)
    if r.returncode == 0:
        names = {entry["name"] for entry in json.loads(r.stdout)}
        if label in names:
            return
    run(
        ["gh", "label", "create", label, "--repo", full_name, "--color", "0075ca"],
        check=False,
    )


def open_pr(repo_dir: Path, default_branch: str, pr_body: str) -> str | None:
    """Create the PR and return its URL, or None on failure."""
    try:
        r = run(
            [
                "gh", "pr", "create",
                "--title", PR_TITLE,
                "--body", pr_body,
                "--base", default_branch,
                "--head", BRANCH_NAME,
                "--label", "Claudify",
            ],
            cwd=str(repo_dir),
        )
        return r.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"    [error] PR creation failed:\n{e.stderr}", file=sys.stderr)
        return None


def has_open_pr(full_name: str) -> bool:
    r = run(
        ["gh", "pr", "list",
         "--repo", full_name,
         "--head", BRANCH_NAME,
         "--state", "open",
         "--json", "number"],
        check=False,
    )
    if r.returncode != 0:
        return False
    return bool(json.loads(r.stdout or "[]"))


# ──────────────────────────────────────────────────────────────
# Orchestration
# ──────────────────────────────────────────────────────────────

def process_repo(
    repo: dict,
    script: Path,
    prompt_file: Path,
    root_prompt_file: Path,
    threshold: int,
    parallelism: int,
    skip_root: bool,
    work_dir: Path,
    skip_existing_pr: bool,
    dry_run: bool = False,
    hybrid_prompt_file: Path | None = None,
) -> str:
    """
    Process a single repository end-to-end.
    Returns one of: 'pr_created' | 'no_changes' | 'skipped' | 'error' | 'dry_run'
    """
    name: str = repo["name"]
    full_name: str = repo["full_name"]
    default_branch: str = repo.get("default_branch", "main")

    print(f"\n{'─' * 60}")
    print(f"[repo]  {full_name}  (default branch: {default_branch})")

    if repo.get("archived") or repo.get("disabled"):
        print("  [skip]  archived or disabled")
        return "skipped"

    if skip_existing_pr and has_open_pr(full_name):
        print(f"  [skip]  open PR already exists for '{BRANCH_NAME}'")
        return "skipped"

    # Fetch team custom property
    team = get_repo_team(full_name)
    print(f"  [team]  {team}")

    repo_dir = work_dir / name

    # 1. Clone
    print(f"  [clone] {full_name}")
    if not clone_repo(full_name, repo_dir):
        return "error"

    try:
        if dry_run:
            # Dry-run: show what generate_claude_docs.py would generate, nothing else.
            print(f"  [dry]   Running generate_claude_docs.py --dry-run …")
            run_generate(
                script, prompt_file, root_prompt_file, repo_dir,
                threshold, parallelism, skip_root,
                team=team,
                dry_run=True,
                hybrid_prompt_file=hybrid_prompt_file,
            )
            return "dry_run"

        # 2. Branch guard
        if branch_exists_remotely(repo_dir):
            print(f"  [skip]  branch '{BRANCH_NAME}' already exists remotely")
            return "skipped"
        if not create_branch(repo_dir):
            return "error"

        # 3. Generate (timed)
        print(f"  [gen]   Running generate_claude_docs.py …")
        costs_file = work_dir / f"{name}_costs.json"
        gen_start = time.monotonic()
        run_generate(
            script, prompt_file, root_prompt_file, repo_dir,
            threshold, parallelism, skip_root,
            team=team,
            costs_output=costs_file,
            hybrid_prompt_file=hybrid_prompt_file,
        )
        gen_duration = time.monotonic() - gen_start
        # We continue even on non-zero exit — some dirs may have been written successfully.

        # Read actual cost data written by generate_claude_docs.py
        cost_data: dict = {}
        if costs_file.exists():
            try:
                cost_data = json.loads(costs_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        # 4. Check for output
        if not any_changes(repo_dir):
            print("  [none]  No CLAUDE.md files generated (no qualifying directories)")
            return "no_changes"

        n_files = count_claude_md_files(repo_dir)

        # 5. Commit + push
        print(f"  [push]  Committing and pushing '{BRANCH_NAME}' …")
        if not commit_and_push(repo_dir):
            return "error"

        # 6. PR
        print(f"  [pr]    Opening PR → {default_branch} …")
        ensure_label(full_name, "Claudify")
        pr_body = _build_pr_body(name, n_files, gen_duration, cost_data)
        url = open_pr(repo_dir, default_branch, pr_body)
        if url:
            print(f"  [ok]    {url}")
            return "pr_created"
        return "error"

    finally:
        # 7. Always clean up
        print(f"  [clean] removing {repo_dir.name}/")
        shutil.rmtree(repo_dir, ignore_errors=True)


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    here = Path(__file__).parent.resolve()
    parser = argparse.ArgumentParser(
        description=(
            "Generate CLAUDE.md context files for every repository in a GitHub org "
            "and open pull requests with the results."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "org",
        help="GitHub organisation name (e.g. jupitermoney)",
    )
    parser.add_argument(
        "--threshold", "-t",
        type=int, default=DEFAULT_THRESHOLD, metavar="LOC",
        help="Upper source-LOC bound passed to generate_claude_docs.py",
    )
    parser.add_argument(
        "--parallel", "-j",
        type=int, default=DEFAULT_INNER_PARALLELISM, metavar="N",
        help="Per-repo parallelism passed to generate_claude_docs.py --parallel",
    )
    parser.add_argument(
        "--script",
        default=str(here / "generate_claude_docs.py"), metavar="PATH",
        help="Path to generate_claude_docs.py",
    )
    parser.add_argument(
        "--prompt-file", "-p",
        default=str(here / "claude-prompt.md"), metavar="PATH",
        help="Path to the per-directory claude-prompt.md template",
    )
    parser.add_argument(
        "--root-prompt-file",
        default=str(here / "claude-root-prompt.md"), metavar="PATH",
        help="Path to the root CLAUDE.md prompt template (claude-root-prompt.md)",
    )
    parser.add_argument(
        "--hybrid-prompt-file",
        default=str(here / "claude-hybrid-prompt.md"), metavar="PATH",
        help=(
            "Path to the hybrid CLAUDE.md prompt template used when the entire repo "
            "fits in a single file but has multiple subdirectories "
            "(claude-hybrid-prompt.md). Skipped if the file does not exist."
        ),
    )
    parser.add_argument(
        "--work-dir",
        default=None, metavar="PATH",
        help="Parent directory for temporary clones (default: auto-created temp dir)",
    )
    parser.add_argument(
        "--repos",
        nargs="+", metavar="REPO",
        help="Process only these repo names (space-separated); omit to process all",
    )
    parser.add_argument(
        "--skip-root-update",
        action="store_true",
        help="Pass --skip-root-update to generate_claude_docs.py (skip root CLAUDE.md)",
    )
    parser.add_argument(
        "--skip-existing-pr",
        action="store_true",
        help="Skip repos that already have an open PR on branch base-claude-auto-generate",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Clone each repo and run generate_claude_docs.py --dry-run to show "
            "which directories would get CLAUDE.md files, without writing any files "
            "or opening PRs."
        ),
    )
    return parser.parse_args()


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    script = Path(args.script).resolve()
    prompt_file = Path(args.prompt_file).resolve()
    root_prompt_file = Path(args.root_prompt_file).resolve()

    for label, path in [
        ("--script", script),
        ("--prompt-file", prompt_file),
        ("--root-prompt-file", root_prompt_file),
    ]:
        if not path.exists():
            print(f"Error: {label} path not found: '{path}'", file=sys.stderr)
            sys.exit(1)

    # Hybrid prompt is optional — skip gracefully if the file doesn't exist.
    hybrid_prompt_path = Path(args.hybrid_prompt_file).resolve()
    hybrid_prompt_file: Path | None = hybrid_prompt_path if hybrid_prompt_path.exists() else None

    print(f"Organisation  : {args.org}")
    print(f"Script        : {script}")
    print(f"Prompt        : {prompt_file}")
    print(f"Root prompt   : {root_prompt_file}")
    print(f"Hybrid prompt : {hybrid_prompt_file or '(not found — child prompt used for single-file repos)'}")
    print(f"Threshold     : {args.threshold:,} LOC")
    print(f"Parallelism   : {args.parallel} workers/repo")
    if args.dry_run:
        print("Mode          : DRY RUN")
    print()

    # Fetch all org repos
    print(f"Fetching repositories for org '{args.org}' …")
    try:
        repos = gh_api_paginated(f"orgs/{args.org}/repos")
    except subprocess.CalledProcessError as e:
        print(f"Error: gh api call failed:\n{e.stderr}", file=sys.stderr)
        sys.exit(1)
    print(f"Found {len(repos)} repositories.\n")

    # Optional filter
    if args.repos:
        keep = set(args.repos)
        repos = [r for r in repos if r["name"] in keep]
        print(f"Filtered to {len(repos)}: {', '.join(r['name'] for r in repos)}\n")

    if not repos:
        print("No repositories to process.")
        return

    # Work directory
    own_work_dir = args.work_dir is None
    if own_work_dir:
        work_dir = Path(tempfile.mkdtemp(prefix="claude-docs-"))
    else:
        work_dir = Path(args.work_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
    print(f"Work dir      : {work_dir}\n")

    stats: dict[str, int] = {"pr_created": 0, "no_changes": 0, "skipped": 0, "error": 0, "dry_run": 0}
    try:
        for repo in repos:
            result = process_repo(
                repo, script, prompt_file, root_prompt_file,
                args.threshold, args.parallel,
                args.skip_root_update, work_dir,
                args.skip_existing_pr,
                dry_run=args.dry_run,
                hybrid_prompt_file=hybrid_prompt_file,
            )
            stats[result] = stats.get(result, 0) + 1
    finally:
        if own_work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)

    print(f"\n{'═' * 60}")
    print("Done.")
    if args.dry_run:
        print(f"  Analysed     : {stats['dry_run']}")
        print(f"  Skipped      : {stats['skipped']}")
        print(f"  Errors       : {stats['error']}")
    else:
        print(f"  PRs created  : {stats['pr_created']}")
        print(f"  No changes   : {stats['no_changes']}")
        print(f"  Skipped      : {stats['skipped']}")
        print(f"  Errors       : {stats['error']}")


if __name__ == "__main__":
    main()
