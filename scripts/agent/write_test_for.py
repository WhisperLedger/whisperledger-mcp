"""Draft a test for a function or class.

Pulls:
- The function/class body via lookup_symbol + read_file
- 1-2 existing tests from the same repo as style examples
- Calls Haiku to draft test cases following the existing style

Returns markdown the agent surfaces. Engineer copies the draft, refines,
and commits.
"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path

REPOS_DIR = Path("/home/ubuntu/jarvis/repos")
MODEL = "claude-haiku-4-5-20251001"


def _read_text_capped(path: Path, cap: int = 8000) -> str:
    try:
        return path.read_text(errors="replace")[:cap]
    except Exception:
        return ""


def _find_existing_tests(repo_path: Path, symbol_hint: str, limit: int = 2) -> list[dict]:
    """Find 1-2 existing test files in the repo as style examples."""
    matches: list[dict] = []
    # Look for the symbol first; if no match, fall back to a generic test under src/test
    patterns = [f"*{symbol_hint}*Test*", f"*{symbol_hint}*Spec*",
                f"*{symbol_hint}*.test.*", "*Test*", "*.test.*", "*_test.py"]
    seen: set[str] = set()
    for pat in patterns:
        for p in repo_path.rglob(pat):
            if not p.is_file():
                continue
            if any(seg in p.parts for seg in ("node_modules", "build", "dist", ".gradle", "target")):
                continue
            if str(p) in seen:
                continue
            seen.add(str(p))
            text = _read_text_capped(p, cap=4000)
            if not text:
                continue
            matches.append({"path": str(p.relative_to(repo_path)),
                            "snippet": text})
            if len(matches) >= limit:
                return matches
    return matches


def write_test_for(repo: str, file_path: str, function: str | None = None) -> str:
    """Draft tests for a given function (or whole file).

    Returns JSON: {ok, draft_markdown, source_excerpt, style_examples, model}.
    """
    repo_root = REPOS_DIR / repo
    if not (repo_root / ".git").is_dir():
        return json.dumps({"ok": False, "error": f"repo not cloned: {repo}"})
    full = (repo_root / file_path).resolve()
    if not str(full).startswith(str(repo_root.resolve()) + "/"):
        return json.dumps({"ok": False, "error": "path traversal"})
    if not full.is_file():
        return json.dumps({"ok": False, "error": f"file not found: {repo}/{file_path}"})

    source = _read_text_capped(full, cap=8000)
    suffix = full.suffix.lstrip(".")
    style_examples = _find_existing_tests(repo_root, function or full.stem)

    examples_blob = "\n\n".join(
        f"### Existing test from `{ex['path']}`\n```{suffix}\n{ex['snippet']}\n```"
        for ex in style_examples
    ) or "(no existing tests found — draft from scratch following standard conventions for this language)"

    rubric = (
        "You draft test cases. Match the existing test style in the repo as "
        "closely as possible — same framework (JUnit / Kotest / Jest / pytest / "
        "etc), same assertion library, same naming conventions, same fixture "
        "pattern.\n\n"
        "Cover: happy path, at least 2 edge cases, error/exception case. Don't "
        "invent dependencies — only mock things actually used by the function "
        "under test. Include imports/setup needed.\n\n"
        "Output markdown with two sections:\n\n"
        "## Test plan\n"
        "Brief bullet list of cases you cover and why.\n\n"
        "## Draft code\n"
        "A single fenced code block (correct language tag) with the actual test "
        "ready to paste into the repo.\n\n"
        "Do NOT invent file contents. If the function signature is unclear, say "
        "what you'd need under '## Test plan' and stop."
    )
    target_note = f"Focus on function: `{function}`" if function else "Cover the file's main public surface."
    user_prompt = (
        f"Repo: {repo}\nFile: {file_path}\nLanguage: {suffix}\n{target_note}\n\n"
        f"## Source under test\n```{suffix}\n{source}\n```\n\n"
        f"## Style examples from this repo\n{examples_blob}"
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=rubric,
            messages=[{"role": "user", "content": user_prompt}],
        )
        draft = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e!s}"})

    return json.dumps({
        "ok": True,
        "repo": repo,
        "file": file_path,
        "function": function,
        "draft_markdown": draft,
        "style_examples": [ex["path"] for ex in style_examples],
        "model": MODEL,
    }, ensure_ascii=False)
