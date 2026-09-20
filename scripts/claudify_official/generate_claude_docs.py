#!/usr/bin/env python3
"""
generate_claude_docs.py

Walks a repository depth-first, generates a CLAUDE.md for every declared
sub-module (or qualifying folder in repos without a workspace definition).
Modules/folders whose LOC exceeds threshold are split recursively; all others
get a single CLAUDE.md. A root CLAUDE.md is generated last to tie them together.

If the entire repository's source LOC is at or below threshold, a single
CLAUDE.md is generated at the repository root.

Usage:
    python generate_claude_docs.py /path/to/repo [options]

Requirements:
    claude CLI must be installed and authenticated (https://claude.ai/code)
"""

import glob as _glob
import json
import os
import re
import sys
import subprocess
import argparse
import concurrent.futures
import threading
from pathlib import Path

# ─────────────────────────────────────────────
# Per-run cost tracking (thread-safe)
# ─────────────────────────────────────────────

_cost_records: list[dict] = []   # {"path": str, "cost_usd": float, "duration_ms": int}
_cost_lock = threading.Lock()


def _record_cost(rel_path: str, cost_usd: float, duration_ms: int) -> None:
    with _cost_lock:
        _cost_records.append({
            "path": rel_path,
            "cost_usd": cost_usd,
            "duration_ms": duration_ms,
        })

# ─────────────────────────────────────────────
# Defaults (all overridable via CLI flags)
# ─────────────────────────────────────────────
DEFAULT_THRESHOLD = 50_000          # upper LOC bound; modules above this are split recursively

# Directories skipped everywhere — build outputs, VCS metadata, generated code
DEFAULT_EXCLUDE_DIRS: set[str] = {
    ".git", ".github", ".gradle", ".idea", ".vscode", ".kotlin", ".claude",
    "build", "dist", "out", "target", "__pycache__", "node_modules",
    ".next", ".nuxt", "vendor",
    "code-gen-jooq",  # Gradle JOOQ-generated classes — never hand-authored
}

# File extensions treated as "code" for LOC counting.
# Config/schema/spec files (.yaml, .yml, .json, .proto, .xml, .toml, .conf)
# are intentionally excluded — a module containing only those files is
# considered empty and gets a single child CLAUDE.md.
SOURCE_EXTENSIONS: set[str] = {
    # JVM
    ".kt", ".java", ".scala", ".groovy",
    # Python / Ruby / PHP
    ".py", ".rb", ".php",
    # JS/TS
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    # Systems
    ".go", ".rs", ".c", ".cpp", ".h", ".hpp", ".cs", ".swift",
    # SQL (migration scripts are code)
    ".sql",
    # Shell
    ".sh", ".bash",
}


# ─────────────────────────────────────────────
# .gitignore handling
# ─────────────────────────────────────────────

def get_gitignored_dirs(repo_root: Path) -> set[str]:
    """
    Use `git ls-files` to discover which directories are ignored by .gitignore.
    Returns a set of directory paths (relative to repo_root, no trailing slash)
    that should be excluded from scanning.
    Falls back to an empty set if not a git repo or git is unavailable.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--ignored",
             "--exclude-standard", "--directory"],
            cwd=repo_root,
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return set()
        ignored = set()
        for line in result.stdout.splitlines():
            stripped = line.rstrip("/").strip()
            if stripped:
                ignored.add(stripped)
        return ignored
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()


# ─────────────────────────────────────────────
# Build-tool-agnostic sub-module detection
# ─────────────────────────────────────────────

def _parse_gradle_modules(repo_root: Path) -> list[str] | None:
    """Gradle: settings.gradle.kts / settings.gradle — include(":module-a")"""
    for fname in ("settings.gradle.kts", "settings.gradle"):
        f = repo_root / fname
        if f.exists():
            content = f.read_text(encoding="utf-8", errors="ignore")
            modules: list[str] = []
            for match in re.finditer(r'include\s*\(([^)]+)\)', content):
                for name_match in re.finditer(r'"([^"]+)"', match.group(1)):
                    path = name_match.group(1).lstrip(":").replace(":", os.sep)
                    modules.append(path)
            return modules if modules else None
    return None


def _parse_maven_modules(repo_root: Path) -> list[str] | None:
    """Maven: pom.xml — <modules><module>name</module></modules>"""
    pom = repo_root / "pom.xml"
    if not pom.exists():
        return None
    content = pom.read_text(encoding="utf-8", errors="ignore")
    modules = re.findall(r'<module>\s*([^<\s]+)\s*</module>', content)
    return modules if modules else None


def _parse_npm_workspaces(repo_root: Path) -> list[str] | None:
    """npm/yarn workspaces: package.json — "workspaces": ["packages/*"]"""
    pkg = repo_root / "package.json"
    if not pkg.exists():
        return None
    try:
        data = json.loads(pkg.read_text(encoding="utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return None
    workspaces = data.get("workspaces", [])
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages", [])
    if not workspaces:
        return None
    modules: list[str] = []
    for pattern in workspaces:
        # Expand glob patterns relative to repo_root
        for match in _glob.glob(str(repo_root / pattern)):
            p = Path(match)
            if p.is_dir():
                modules.append(str(p.relative_to(repo_root)))
    return modules if modules else None


def _parse_go_workspace(repo_root: Path) -> list[str] | None:
    """Go workspaces: go.work — use ./module"""
    gowork = repo_root / "go.work"
    if not gowork.exists():
        return None
    content = gowork.read_text(encoding="utf-8", errors="ignore")
    modules = re.findall(r'^\s*use\s+\./([^\s]+)', content, re.MULTILINE)
    return modules if modules else None


def _parse_cargo_workspace(repo_root: Path) -> list[str] | None:
    """Rust Cargo workspace: Cargo.toml — [workspace] members = ["crate-a"]"""
    cargo = repo_root / "Cargo.toml"
    if not cargo.exists():
        return None
    content = cargo.read_text(encoding="utf-8", errors="ignore")
    # Only look inside the [workspace] section
    ws_match = re.search(r'\[workspace\](.*?)(?=^\[|\Z)', content, re.DOTALL | re.MULTILINE)
    if not ws_match:
        return None
    ws_section = ws_match.group(1)
    members_match = re.search(r'members\s*=\s*\[([^\]]+)\]', ws_section, re.DOTALL)
    if not members_match:
        return None
    modules = re.findall(r'"([^"]+)"', members_match.group(1))
    # Expand glob patterns
    expanded: list[str] = []
    for pattern in modules:
        matched = _glob.glob(str(repo_root / pattern))
        if matched:
            for m in matched:
                p = Path(m)
                if p.is_dir():
                    expanded.append(str(p.relative_to(repo_root)))
        else:
            expanded.append(pattern)
    return expanded if expanded else None


# Priority order: try each build tool in sequence, use the first that yields modules.
_BUILD_TOOL_PARSERS = [
    ("Gradle",   _parse_gradle_modules),
    ("Maven",    _parse_maven_modules),
    ("npm/yarn", _parse_npm_workspaces),
    ("Go",       _parse_go_workspace),
    ("Cargo",    _parse_cargo_workspace),
]


def parse_build_modules(repo_root: Path) -> tuple[str, list[str]] | None:
    """
    Detect sub-modules from whatever build/workspace tool is in use.
    Returns (tool_name, [module_path, ...]) or None if no workspace definition found.
    """
    for tool_name, parser in _BUILD_TOOL_PARSERS:
        result = parser(repo_root)
        if result is not None:
            return tool_name, result
    return None


# ─────────────────────────────────────────────
# LOC counting
# ─────────────────────────────────────────────

def is_source_file(path: Path) -> bool:
    return path.suffix.lower() in SOURCE_EXTENSIONS


def count_file_lines(path: Path) -> int:
    """Non-empty lines in a file; returns 0 on binary or read error."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return sum(1 for line in fh if line.strip())
    except Exception:
        return 0


def _is_ignored(rel_path: str, gitignored: set[str]) -> bool:
    """Check if a relative path (or any of its parents) is in the gitignored set."""
    parts = rel_path.split(os.sep)
    for i in range(1, len(parts) + 1):
        prefix = os.sep.join(parts[:i])
        if prefix in gitignored:
            return True
    return False


def count_dir_loc(
    dir_path: Path,
    exclude_dirs: set[str],
    repo_root: Path | None = None,
    gitignored: set[str] | None = None,
) -> int:
    """
    Recursively count source LOC under dir_path (DFS via os.walk).
    This is the number used to decide whether a directory gets a CLAUDE.md.
    """
    total = 0
    for root, dirs, files in os.walk(dir_path, topdown=True):
        # Filter excluded directory names
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        # Filter gitignored directories
        if gitignored and repo_root:
            dirs[:] = [
                d for d in dirs
                if not _is_ignored(
                    str((Path(root) / d).relative_to(repo_root)),
                    gitignored,
                )
            ]
        for fname in files:
            fp = Path(root) / fname
            if is_source_file(fp):
                total += count_file_lines(fp)
    return total


# ─────────────────────────────────────────────
# CLAUDE.md generation via claude CLI
# ─────────────────────────────────────────────

def _run_claude(prompt: str, cwd: Path) -> tuple[bool, dict]:
    """
    Run `claude --print --dangerously-skip-permissions --output-format json`
    with the given prompt at cwd.  The prompt is expected to instruct Claude
    to write a file directly.

    Returns (success, cost_info) where cost_info has keys:
      - cost_usd   : actual API cost in USD (float), 0.0 if unavailable
      - duration_ms: API duration in milliseconds (int), 0 if unavailable
    """
    cost_info: dict = {"cost_usd": 0.0, "duration_ms": 0}
    try:
        result = subprocess.run(
            [
                "claude", "--print", "--dangerously-skip-permissions",
                "--output-format", "json",
                prompt,
            ],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
        )
        # Parse cost/duration from the JSON result object.
        try:
            data = json.loads(result.stdout)
            cost_info["cost_usd"] = float(data.get("total_cost_usd") or 0.0)
            cost_info["duration_ms"] = int(data.get("duration_ms") or 0)
            # Echo Claude's text response so generation progress is still visible.
            if data.get("result"):
                print(data["result"], flush=True)
        except (json.JSONDecodeError, ValueError, TypeError):
            # Fallback: raw output (e.g. older CLI version without JSON support).
            if result.stdout:
                print(result.stdout, flush=True)

        if result.returncode != 0:
            print(f"  [error] claude exited {result.returncode}", file=sys.stderr, flush=True)
            return False, cost_info
        return True, cost_info
    except FileNotFoundError:
        print(
            "  [error] 'claude' CLI not found. Install Claude Code: https://claude.ai/code",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)


def generate_claude_md(
    dir_path: Path,
    repo_root: Path,
    prompt_template: str,
    team: str = "Unknown",
) -> bool:
    """
    Run the claude CLI at dir_path using prompt_template.
    The template must instruct Claude to write CLAUDE.md itself.
    Returns True if CLAUDE.md exists after the run.
    """
    rel_dir = dir_path.relative_to(repo_root)
    prompt = (
        prompt_template
        .replace("{DIRECTORY}", str(rel_dir))
        .replace("{REPO}", repo_root.name)
        .replace("{TEAM}", team)
    )
    ok, cost_info = _run_claude(prompt, dir_path)
    success = ok and (dir_path / "CLAUDE.md").exists()
    if success:
        _record_cost(
            str(dir_path.relative_to(repo_root)),
            cost_info["cost_usd"],
            cost_info["duration_ms"],
        )
    return success


def generate_root_claude_md(
    repo_root: Path,
    generated: list[tuple[Path, int]],
    all_modules: list[tuple[Path, int]],
    root_prompt_template: str,
    team: str = "Unknown",
) -> None:
    """
    Generate the root CLAUDE.md using the root prompt template.
    The prompt lists all sub-CLAUDE.md paths and all modules so Claude can
    document the entire repository, pointing to child files where they exist.
    """
    nested_files = "\n".join(
        f"- {(dp / 'CLAUDE.md').relative_to(repo_root)}"
        for dp, _ in sorted(generated, key=lambda x: str(x[0]))
    )
    if not nested_files:
        nested_files = "(none — no child CLAUDE.md files were generated)"

    all_modules_text = "\n".join(
        f"- {dp.relative_to(repo_root)} ({loc:,} LOC)"
        + (" [has CLAUDE.md]" if (dp / "CLAUDE.md").exists() else "")
        for dp, loc in sorted(all_modules, key=lambda x: str(x[0]))
    )
    if not all_modules_text:
        all_modules_text = "(no sub-modules found)"

    prompt = (
        root_prompt_template
        .replace("{REPO}", repo_root.name)
        .replace("{NESTED_FILES}", nested_files)
        .replace("{ALL_MODULES}", all_modules_text)
        .replace("{TEAM}", team)
    )
    print("\n[root]   Generating root CLAUDE.md …", flush=True)
    ok, cost_info = _run_claude(prompt, repo_root)
    if ok and (repo_root / "CLAUDE.md").exists():
        print("  [ok]    Written → CLAUDE.md", flush=True)
        _record_cost(".", cost_info["cost_usd"], cost_info["duration_ms"])
    else:
        print("  [error] Root CLAUDE.md was not written.", file=sys.stderr, flush=True)


# ─────────────────────────────────────────────
# DFS directory collection
# ─────────────────────────────────────────────

def collect_qualifying_dirs(
    repo_root: Path,
    threshold: int,
    exclude_dirs: set[str],
    gitignored: set[str] | None = None,
) -> tuple[list[tuple[Path, int]], list[tuple[Path, int]]]:
    """
    DFS walk: return two lists:
      1. Qualifying dirs — highest-level directories whose recursive source LOC
         is <= threshold (no lower bound; empty dirs included).
      2. All dirs — every non-root directory, for the root prompt.

    Two-pass strategy for qualifying dirs
    --------------------------------------
    Pass 1 — collect every non-root dir.
    Pass 2 — filter to LOC <= threshold, then keep the HIGHEST ANCESTOR in
             range.  For each dir in range, drop it if any of its ancestors is
             also in range (that ancestor is a better, more cohesive target).
    """
    all_dirs: list[tuple[Path, int]] = []
    qualifying_candidates: list[tuple[Path, int]] = []

    def _walk(dir_path: Path) -> None:
        try:
            children = sorted(
                [d for d in dir_path.iterdir()
                 if d.is_dir()
                 and d.name not in exclude_dirs
                 and not (gitignored and _is_ignored(
                     str(d.relative_to(repo_root)), gitignored
                 ))],
                key=lambda d: d.name,
            )
        except PermissionError:
            return

        for child in children:
            _walk(child)                    # recurse first → depth-first order

        if dir_path != repo_root:
            loc = count_dir_loc(dir_path, exclude_dirs, repo_root, gitignored)
            all_dirs.append((dir_path, loc))
            qualifying_candidates.append((dir_path, loc))

    _walk(repo_root)

    # Filter to LOC <= threshold
    candidates = [(dp, loc) for dp, loc in qualifying_candidates if loc <= threshold]

    # Deduplicate: keep highest ancestor in range.
    candidate_paths = {dp for dp, _ in candidates}
    qualifying = [
        (dp, loc) for dp, loc in candidates
        if not any(
            dp != ancestor and dp.is_relative_to(ancestor)
            for ancestor in candidate_paths
        )
    ]

    return qualifying, all_dirs


# ─────────────────────────────────────────────
# Recursive module generation
# ─────────────────────────────────────────────

def generate_module_recursive(
    module_path: Path,
    repo_root: Path,
    threshold: int,
    exclude_dirs: set[str],
    gitignored: set[str] | None,
    prompt_template: str,
    root_prompt_template: str,
    team: str,
    parallel: int,
    overwrite: bool,
    dry_run: bool,
) -> list[tuple[Path, int]]:
    """
    Generate CLAUDE.md files for a single Gradle sub-module.

    If the module's LOC exceeds the threshold, it is treated as a mini-repo:
    qualifying sub-directories get child CLAUDE.md files, then a root-style
    CLAUDE.md is generated for the module itself.

    If the module's LOC is within range, a single child-style CLAUDE.md is
    generated.

    Returns list of (path, loc) for all dirs that got a CLAUDE.md.
    """
    module_loc = count_dir_loc(module_path, exclude_dirs, repo_root, gitignored)
    rel = module_path.relative_to(repo_root)

    if module_loc <= threshold:
        # Small module → single child-style CLAUDE.md
        if dry_run:
            print(f"  [target] {rel}  ({module_loc:,} LOC — single CLAUDE.md)", flush=True)
            return [(module_path, module_loc)]

        if (module_path / "CLAUDE.md").exists() and not overwrite:
            print(f"  [skip]   {rel}  (already has CLAUDE.md)", flush=True)
            return [(module_path, module_loc)]

        print(f"  [gen]    {rel}  ({module_loc:,} LOC) …", flush=True)
        ok = generate_claude_md(module_path, repo_root, prompt_template, team=team)
        if ok:
            print(f"  [ok]     {rel}/CLAUDE.md", flush=True)
            return [(module_path, module_loc)]
        else:
            print(f"  [error]  CLAUDE.md not written for {rel}", flush=True)
            return []

    # Large module → recursive: find qualifying sub-dirs, generate children,
    # then generate a root-style CLAUDE.md for the module itself.
    print(f"  [large]  {rel}  ({module_loc:,} LOC > {threshold:,} — recursive split)", flush=True)

    # Find qualifying sub-directories within this module
    sub_qualifying, sub_all = collect_qualifying_dirs(
        module_path, threshold, exclude_dirs, gitignored,
    )

    generated_children: list[tuple[Path, int]] = []

    if dry_run:
        for dp, loc in sorted(sub_qualifying, key=lambda x: str(x[0])):
            sr = dp.relative_to(repo_root)
            print(f"    [target] {sr}  ({loc:,} LOC)", flush=True)
        return sub_qualifying

    # Generate child CLAUDE.md files in parallel
    to_generate = []
    for dp, loc in sub_qualifying:
        if (dp / "CLAUDE.md").exists() and not overwrite:
            sr = dp.relative_to(repo_root)
            print(f"    [skip]   {sr}  (already has CLAUDE.md)", flush=True)
            generated_children.append((dp, loc))
        else:
            to_generate.append((dp, loc))

    if to_generate:
        def _gen_worker(item: tuple[Path, int]) -> tuple[Path, int, bool]:
            dp, loc = item
            sr = dp.relative_to(repo_root)
            print(f"    [gen]    {sr}  ({loc:,} LOC) …", flush=True)
            ok = generate_claude_md(dp, repo_root, prompt_template, team=team)
            if ok:
                print(f"    [ok]     {(dp / 'CLAUDE.md').relative_to(repo_root)}", flush=True)
            else:
                print(f"    [error]  CLAUDE.md not written for {sr}", flush=True)
            return dp, loc, ok

        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {pool.submit(_gen_worker, item): item for item in to_generate}
            for future in concurrent.futures.as_completed(futures):
                dp, loc, ok = future.result()
                if ok:
                    generated_children.append((dp, loc))

    # Generate root-style CLAUDE.md for the module itself
    generate_root_claude_md(
        module_path, generated_children, sub_all, root_prompt_template,
        team=team,
    )

    all_generated = generated_children[:]
    if (module_path / "CLAUDE.md").exists():
        all_generated.append((module_path, module_loc))
    return all_generated


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate CLAUDE.md files for repository sub-directories whose source "
            "LOC is within the target range, and generate a root CLAUDE.md that ties "
            "all sub-CLAUDE.md files together."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "repo_path",
        help="Absolute or relative path to the repository root.",
    )
    parser.add_argument(
        "--threshold", "-t",
        type=int,
        default=DEFAULT_THRESHOLD,
        metavar="LOC",
        help=(
            f"Upper LOC bound per CLAUDE.md (default {DEFAULT_THRESHOLD:,}). "
            f"Modules/dirs above this are split recursively into smaller targets."
        ),
    )
    parser.add_argument(
        "--prompt-file", "-p",
        default=None,
        metavar="PATH",
        help=(
            "Path to the per-directory claude-prompt.md template. "
            "Defaults to <repo_path>/claude-prompt.md."
        ),
    )
    parser.add_argument(
        "--root-prompt-file",
        default=None,
        metavar="PATH",
        help=(
            "Path to the root CLAUDE.md prompt template. "
            "Defaults to <repo_path>/claude-root-prompt.md, then "
            "<script_dir>/claude-root-prompt.md."
        ),
    )
    parser.add_argument(
        "--hybrid-prompt-file",
        default=None,
        metavar="PATH",
        help=(
            "Path to the hybrid CLAUDE.md prompt template used when the entire repo "
            "fits in a single file but has multiple subdirectories. "
            "Defaults to <repo_path>/claude-hybrid-prompt.md, then "
            "<script_dir>/claude-hybrid-prompt.md. Falls back to the child prompt "
            "if not found."
        ),
    )
    parser.add_argument(
        "--exclude", "-e",
        nargs="*",
        default=[],
        metavar="DIR",
        help="Additional directory names to exclude (appended to built-in list).",
    )
    parser.add_argument(
        "--team",
        default="Unknown",
        metavar="NAME",
        help="Team name for the Maintenance section (e.g., from GitHub custom property).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Regenerate CLAUDE.md files that already exist. Default: skip existing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print qualifying directories without calling the API or writing any files.",
    )
    parser.add_argument(
        "--parallel", "-j",
        type=int,
        default=4,
        metavar="N",
        help="Number of directories to generate in parallel.",
    )
    parser.add_argument(
        "--skip-root-update",
        action="store_true",
        help="Do not generate a root CLAUDE.md after generating sub-directory files.",
    )
    parser.add_argument(
        "--costs-output",
        default=None,
        metavar="PATH",
        help=(
            "Write actual API cost and duration data as JSON to this path. "
            "Intended for use by generate_org_claude_docs.py."
        ),
    )
    return parser.parse_args()


# ─────────────────────────────────────────────
# Cost summary writer
# ─────────────────────────────────────────────

def _write_costs(output_path: str | None) -> None:
    """Write per-path cost records to output_path as JSON (if provided)."""
    if not output_path or not _cost_records:
        return
    total_cost_usd = sum(r["cost_usd"] for r in _cost_records)
    total_duration_ms = sum(r["duration_ms"] for r in _cost_records)
    data = {
        "paths": _cost_records,
        "total_cost_usd": total_cost_usd,
        "total_duration_ms": total_duration_ms,
    }
    try:
        Path(output_path).write_text(json.dumps(data, indent=2))
    except OSError as exc:
        print(f"  [warn]  Could not write costs file: {exc}", file=sys.stderr, flush=True)


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    repo_root = Path(args.repo_path).resolve()
    if not repo_root.is_dir():
        print(f"Error: '{repo_root}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    # Per-directory prompt
    prompt_file = Path(args.prompt_file) if args.prompt_file else repo_root / "claude-prompt.md"
    if not prompt_file.exists():
        # Fall back to script directory
        prompt_file = Path(__file__).parent.resolve() / "claude-prompt.md"
    if not prompt_file.exists():
        print(
            f"Error: template file not found.\n"
            "Provide a path with --prompt-file or place claude-prompt.md in the repo root.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Root prompt
    root_prompt_source = None  # track which file we loaded for logging
    if args.root_prompt_file:
        root_prompt_source = Path(args.root_prompt_file)
        if not root_prompt_source.exists():
            print(f"Error: root prompt file not found at '{root_prompt_source}'.", file=sys.stderr)
            sys.exit(1)
        root_prompt_template = root_prompt_source.read_text(encoding="utf-8")
    else:
        # Try repo root first, then script directory
        root_prompt_source = repo_root / "claude-root-prompt.md"
        if not root_prompt_source.exists():
            root_prompt_source = Path(__file__).parent.resolve() / "claude-root-prompt.md"
        if not root_prompt_source.exists():
            print(
                "Error: no root prompt template found.\n"
                "Provide a path with --root-prompt-file or place claude-root-prompt.md "
                "in the repo root or script directory.",
                file=sys.stderr,
            )
            sys.exit(1)
        root_prompt_template = root_prompt_source.read_text(encoding="utf-8")

    # Hybrid prompt (used for single-file mode when multiple subdirectories exist)
    hybrid_prompt_source = None
    hybrid_prompt_template: str | None = None
    if args.hybrid_prompt_file:
        hybrid_prompt_source = Path(args.hybrid_prompt_file)
        if not hybrid_prompt_source.exists():
            print(f"Error: hybrid prompt file not found at '{hybrid_prompt_source}'.", file=sys.stderr)
            sys.exit(1)
        hybrid_prompt_template = hybrid_prompt_source.read_text(encoding="utf-8")
    else:
        for candidate in (
            repo_root / "claude-hybrid-prompt.md",
            Path(__file__).parent.resolve() / "claude-hybrid-prompt.md",
        ):
            if candidate.exists():
                hybrid_prompt_source = candidate
                hybrid_prompt_template = candidate.read_text(encoding="utf-8")
                break

    exclude_dirs: set[str] = DEFAULT_EXCLUDE_DIRS | set(args.exclude)
    prompt_template = prompt_file.read_text(encoding="utf-8")

    # Collect gitignored directories
    gitignored = get_gitignored_dirs(repo_root)
    if gitignored:
        print(f"Gitignore  : excluding {len(gitignored)} ignored path(s)", flush=True)

    print(f"Repository : {repo_root}", flush=True)
    print(f"Threshold  : ≤ {args.threshold:,} source lines (split above this)", flush=True)
    print(f"Template   : {prompt_file}", flush=True)
    print(f"Root tpl   : {root_prompt_source}", flush=True)
    print(f"Hybrid tpl : {hybrid_prompt_source or '(not found — will fall back to child prompt)'}", flush=True)
    print(f"Team       : {args.team}", flush=True)
    print(f"Parallel   : {args.parallel} workers", flush=True)
    if args.dry_run:
        print("Mode       : DRY RUN (no files will be written)", flush=True)
    elif args.overwrite:
        print("Mode       : overwrite existing CLAUDE.md files", flush=True)
    else:
        print("Mode       : skip existing CLAUDE.md files (use --overwrite to regenerate)", flush=True)
    print(flush=True)

    # ── Build-tool module detection ───────────────────────────────────────────
    detected = parse_build_modules(repo_root)

    if detected:
        tool_name, raw_modules = detected
        # Every declared sub-module always gets a CLAUDE.md. Large modules get
        # recursive treatment (child CLAUDE.md files + root-style module CLAUDE.md).
        valid_modules = []
        for mod_path_str in raw_modules:
            mod_path = repo_root / mod_path_str
            if mod_path.is_dir():
                valid_modules.append(mod_path)
            else:
                print(f"  [warn]   Module '{mod_path_str}' declared in {tool_name} workspace but directory not found", flush=True)

        # Deduplicate: if both a parent and a child are declared (e.g.,
        # "internal-services" and "internal-services:api"), drop the child from
        # the top-level list — the parent's recursive processing will handle it.
        declared_set = set(valid_modules)
        valid_modules = [
            mp for mp in valid_modules
            if not any(
                mp != other and mp.is_relative_to(other)
                for other in declared_set
            )
        ]

        # Compute LOC for each module once.
        module_locs: dict[Path, int] = {
            mp: count_dir_loc(mp, exclude_dirs, repo_root, gitignored)
            for mp in valid_modules
        }

        # 0-LOC modules bubble up to their depth-1 ancestor (the directory
        # sitting immediately under repo_root).  That ancestor gets exactly
        # 1 CLAUDE.md covering itself and all its empty descendants.
        # Non-zero-LOC modules keep their own position.
        final_targets: list[Path] = []
        depth1_from_empty: set[Path] = set()

        for mod_path in sorted(valid_modules):
            if module_locs[mod_path] == 0:
                depth1 = repo_root / mod_path.relative_to(repo_root).parts[0]
                depth1_from_empty.add(depth1)
            else:
                final_targets.append(mod_path)

        # Add depth-1 ancestors collected from empty modules (skip if already
        # present as a non-zero target).
        target_paths = set(final_targets)
        for d1 in sorted(depth1_from_empty):
            if d1 not in target_paths:
                final_targets.append(d1)
                module_locs[d1] = count_dir_loc(d1, exclude_dirs, repo_root, gitignored)

        valid_modules = sorted(final_targets)

        print(f"{tool_name} modules detected: {len(valid_modules)}\n", flush=True)

        if args.dry_run:
            for mod_path in sorted(valid_modules):
                loc = module_locs.get(mod_path, 0)
                rel = mod_path.relative_to(repo_root)
                if loc > args.threshold:
                    print(f"  [large]  {rel}  ({loc:,} LOC > {args.threshold:,} — recursive split)", flush=True)
                    generate_module_recursive(
                        mod_path, repo_root, args.threshold, exclude_dirs,
                        gitignored, prompt_template, root_prompt_template,
                        args.team, args.parallel, args.overwrite, dry_run=True,
                    )
                else:
                    print(f"  [target] {rel}  ({loc:,} LOC — single CLAUDE.md)", flush=True)
            print("\nDry run complete — nothing written.", flush=True)

            if not args.skip_root_update:
                print("(Root CLAUDE.md would also be generated)", flush=True)
            return

        # Generate CLAUDE.md for each declared module in parallel
        all_generated: list[tuple[Path, int]] = []
        all_module_info: list[tuple[Path, int]] = [
            (mod_path, module_locs.get(mod_path, 0)) for mod_path in sorted(valid_modules)
        ]

        def _module_worker(mod_path: Path) -> list[tuple[Path, int]]:
            return generate_module_recursive(
                mod_path, repo_root, args.threshold, exclude_dirs,
                gitignored, prompt_template, root_prompt_template,
                args.team, args.parallel, args.overwrite, dry_run=False,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = {pool.submit(_module_worker, mp): mp for mp, _ in all_module_info}
            for future in concurrent.futures.as_completed(futures):
                mod_path = futures[future]
                try:
                    gen = future.result()
                    all_generated.extend(gen)
                except Exception as exc:
                    print(
                        f"  [error]  {mod_path.relative_to(repo_root)}: {exc}",
                        file=sys.stderr, flush=True,
                    )

        # ── Root CLAUDE.md generation ─────────────────────────────────────────
        if not args.skip_root_update:
            generate_root_claude_md(
                repo_root, all_generated, all_module_info, root_prompt_template,
                team=args.team,
            )

        total_files = sum(1 for dp, _ in all_generated if (dp / "CLAUDE.md").exists())
        print(f"\nDone.  {total_files} CLAUDE.md file(s) generated.", flush=True)
        _write_costs(args.costs_output)
        return

    # ── Fallback: LOC-based discovery (no workspace definition found) ────────

    # Single-file mode: entire repo fits within threshold
    root_loc = count_dir_loc(repo_root, exclude_dirs, repo_root, gitignored)
    if root_loc <= args.threshold:
        print(f"Repository root has {root_loc:,} LOC (≤ {args.threshold:,} threshold).", flush=True)

        # Detect whether the root contains multiple subdirectories (excluding
        # excluded/ignored dirs).  If so, use the hybrid prompt so the single
        # CLAUDE.md covers both repo-level and directory-level concerns.
        has_subdirs = any(
            d.is_dir()
            and d.name not in exclude_dirs
            and not (gitignored and _is_ignored(str(d.relative_to(repo_root)), gitignored))
            for d in repo_root.iterdir()
        )

        if has_subdirs and hybrid_prompt_template:
            active_template = hybrid_prompt_template
            mode_label = "single-file hybrid mode (root + subdirectories)"
        else:
            active_template = prompt_template
            mode_label = "single-file mode"

        print(f"Generating a single CLAUDE.md at the repository root ({mode_label}).", flush=True)

        if args.dry_run:
            print(f"\nDry run complete — {mode_label}, nothing written.", flush=True)
            return

        if (repo_root / "CLAUDE.md").exists() and not args.overwrite:
            print("[skip]   CLAUDE.md already exists (use --overwrite to regenerate)", flush=True)
            return

        prompt = (
            active_template
            .replace("{DIRECTORY}", ".")
            .replace("{REPO}", repo_root.name)
            .replace("{TEAM}", args.team)
        )
        ok, cost_info = _run_claude(prompt, repo_root)
        if ok and (repo_root / "CLAUDE.md").exists():
            print(f"[ok]     Written → CLAUDE.md ({mode_label})", flush=True)
            _record_cost(".", cost_info["cost_usd"], cost_info["duration_ms"])
        else:
            print("[error]  CLAUDE.md was not written.", file=sys.stderr, flush=True)
        _write_costs(args.costs_output)
        return

    # LOC-based discovery phase
    print("Scanning repository (DFS) …", flush=True)
    qualifying, all_modules = collect_qualifying_dirs(
        repo_root, args.threshold, exclude_dirs, gitignored,
    )

    if not qualifying:
        print(f"No directories found at or below the {args.threshold:,} LOC threshold.", flush=True)
        if not args.skip_root_update:
            print("Will still generate a root CLAUDE.md covering all modules.", flush=True)
            if not args.dry_run:
                generate_root_claude_md(
                    repo_root, [], all_modules, root_prompt_template,
                    team=args.team,
                )
        _write_costs(args.costs_output)
        return

    print(f"Found {len(qualifying)} qualifying director{'y' if len(qualifying) == 1 else 'ies'}:\n", flush=True)
    for dir_path, loc in sorted(qualifying, key=lambda x: str(x[0])):
        rel = dir_path.relative_to(repo_root)
        claude_md = dir_path / "CLAUDE.md"
        tag = "[exists]" if claude_md.exists() else "[new]   "
        print(f"  {tag}  {loc:>8,} LOC   {rel}", flush=True)

    print(f"\nTotal modules in repository: {len(all_modules)}", flush=True)

    if args.dry_run:
        print("\nDry run complete — nothing written.", flush=True)
        return

    # Generation phase (parallel)
    generated: list[tuple[Path, int]] = []

    to_generate = []
    for dir_path, loc in qualifying:
        if (dir_path / "CLAUDE.md").exists() and not args.overwrite:
            rel = dir_path.relative_to(repo_root)
            print(f"[skip]   {rel}  (already has CLAUDE.md)", flush=True)
            generated.append((dir_path, loc))
        else:
            to_generate.append((dir_path, loc))

    if to_generate:
        print(f"\nGenerating {len(to_generate)} CLAUDE.md file(s) with {args.parallel} parallel workers …\n", flush=True)

        def _gen_worker(item: tuple[Path, int]) -> tuple[Path, int, bool]:
            dir_path, loc = item
            rel = dir_path.relative_to(repo_root)
            print(f"[gen]    {rel}  ({loc:,} LOC) …", flush=True)
            ok = generate_claude_md(
                dir_path, repo_root, prompt_template,
                team=args.team,
            )
            if ok:
                print(f"[ok]     {(dir_path / 'CLAUDE.md').relative_to(repo_root)}", flush=True)
            else:
                print(f"[error]  CLAUDE.md not written for {rel}", flush=True)
            return dir_path, loc, ok

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = {pool.submit(_gen_worker, item): item for item in to_generate}
            for future in concurrent.futures.as_completed(futures):
                dir_path, loc, ok = future.result()
                if ok:
                    generated.append((dir_path, loc))

    # Root CLAUDE.md generation
    if not args.skip_root_update:
        generate_root_claude_md(
            repo_root, generated, all_modules, root_prompt_template,
            team=args.team,
        )

    print(f"\nDone.  {len(generated)} director{'y' if len(generated) == 1 else 'ies'} documented.", flush=True)
    _write_costs(args.costs_output)


if __name__ == "__main__":
    main()
