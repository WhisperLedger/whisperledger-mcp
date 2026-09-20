"""Multi-dimensional developer ranking from commit history.

Reads a commits JSONL (output of agent.commit_summary), fetches per-commit
stats from gh api in parallel (additions/deletions/files), and computes a
richer view than just commit counts:

  - Volume:        total LOC added/deleted/net, files touched
  - Breadth:       distinct repos, distinct top-level dirs, distinct languages
  - Focus index:   median files-per-commit, median LOC-per-commit
                   (low = surgical; high = sweeping)
  - Build vs maintain: adds-to-deletes ratio
  - Bulk filter:   commits over the BULK_FILES / BULK_LINES thresholds are
                   excluded from focus stats (vendored bumps, formatter passes)
  - Composite:     normalized weighted score across dimensions

Outputs CLI tables and an HTML report.
"""
from __future__ import annotations
import argparse
import html
import json
import math
import statistics
import subprocess
from .repo_names import resolve_gh_org
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Heuristic: a commit touching this many files (or this many lines) is treated
# as "bulk" — kept in volume totals but excluded from focus/quality stats so
# auto-formatter passes and dependency bumps don't tank someone's median.
BULK_FILES = 100
BULK_LINES = 5000

# File extensions to exclude entirely from "lines of code" totals — generated
# or vendored files that don't represent human authorship.
GENERATED_EXTS = {
    "lock", "min", "map", "sum",  # *.lock, *.min.js, *.map, go.sum
}
GENERATED_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum", "gradle.lockfile",
}

LANG_BY_EXT = {
    "kt": "Kotlin", "kts": "Kotlin",
    "java": "Java", "scala": "Scala",
    "py": "Python",
    "ts": "TypeScript", "tsx": "TypeScript",
    "js": "JavaScript", "jsx": "JavaScript", "mjs": "JavaScript",
    "go": "Go", "rs": "Rust", "rb": "Ruby", "swift": "Swift",
    "c": "C", "cc": "C++", "cpp": "C++", "h": "C", "hpp": "C++",
    "sql": "SQL", "tf": "Terraform", "hcl": "HCL",
    "yaml": "Config", "yml": "Config", "json": "Config", "toml": "Config",
    "sh": "Shell", "bash": "Shell",
    "html": "Web", "css": "Web", "vue": "Web", "svelte": "Web",
    "md": "Docs", "rst": "Docs", "mdx": "Docs",
    "gradle": "Build", "proto": "Proto",
}


def _lang(filename: str) -> str:
    name = Path(filename).name.lower()
    if name in GENERATED_NAMES:
        return "_generated"
    ext = Path(filename).suffix.lstrip(".").lower()
    if ext in GENERATED_EXTS:
        return "_generated"
    return LANG_BY_EXT.get(ext, "Other")


def fetch_commit_detail(repo: str, sha: str) -> dict | None:
    """gh api repos/jupitermoney/{repo}/commits/{sha} → {additions, deletions, files: [{filename, additions, deletions}]}."""
    cmd = [
        "gh", "api", (lambda o=resolve_gh_org(repo): f"repos/{o[0]}/{o[1]}/commits/{sha}")(),
        "--jq",
        '{additions: .stats.additions, deletions: .stats.deletions, '
        'files: [.files[]? | {filename: .filename, additions: .additions, deletions: .deletions}]}',
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return None
    if res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


def _norm_key(name: str, email: str) -> str:
    if email and "@" in email:
        return email.split("@", 1)[0].lower().split("+", 1)[0]
    return (name or "unknown").lower().strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commits-jsonl", required=True,
                    help="Output of agent.commit_summary --raw-out")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--html-out", default=None)
    args = ap.parse_args()

    commits_path = Path(args.commits_jsonl).expanduser()
    base_commits = [json.loads(l) for l in commits_path.read_text().splitlines() if l.strip()]
    print(f"Loaded {len(base_commits)} commits from {commits_path}", file=sys.stderr)
    print(f"Fetching per-commit stats from gh api ({args.workers} workers)...", file=sys.stderr)

    # Parallel fetch of per-commit details.
    enriched: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(fetch_commit_detail, c["repo"], c["sha"]): c for c in base_commits}
        for i, fut in enumerate(as_completed(futures), 1):
            c = futures[fut]
            d = fut.result()
            if d is None:
                continue
            c["adds"] = d.get("additions") or 0
            c["dels"] = d.get("deletions") or 0
            c["files_data"] = d.get("files") or []
            enriched.append(c)
            if i % 100 == 0:
                print(f"  ... {i}/{len(base_commits)}", file=sys.stderr)

    print(f"\nEnriched {len(enriched)}/{len(base_commits)} commits.", file=sys.stderr)

    # Per-author aggregations.
    name_for_key: dict[str, Counter] = defaultdict(Counter)
    for c in enriched:
        k = _norm_key(c["author"], c.get("email", ""))
        c["_key"] = k
        name_for_key[k][c["author"]] += 1
    canonical = {k: cnt.most_common(1)[0][0] for k, cnt in name_for_key.items()}

    by_author: dict[str, dict] = defaultdict(lambda: {
        "commits": 0,
        "adds": 0, "dels": 0,
        "code_adds": 0, "code_dels": 0,  # excluding generated
        "files_touched_total": 0,        # sum across all commits (with dupes)
        "unique_files": set(),
        "repos": set(),
        "langs": Counter(),
        "lines_per_commit_samples": [],
        "files_per_commit_samples": [],
        "bulk_commits": 0,
    })

    for c in enriched:
        k = c["_key"]
        a = by_author[k]
        a["commits"] += 1
        a["adds"] += c["adds"]
        a["dels"] += c["dels"]
        a["repos"].add(c["repo"])

        files_in_commit = c["files_data"]
        n_files = len(files_in_commit)
        a["files_touched_total"] += n_files

        is_bulk = n_files > BULK_FILES or (c["adds"] + c["dels"]) > BULK_LINES
        if is_bulk:
            a["bulk_commits"] += 1
        else:
            a["lines_per_commit_samples"].append(c["adds"] + c["dels"])
            a["files_per_commit_samples"].append(n_files)

        for f in files_in_commit:
            fn = f.get("filename", "")
            lang = _lang(fn)
            a["unique_files"].add(f"{c['repo']}::{fn}")
            if lang == "_generated":
                continue
            a["code_adds"] += f.get("additions") or 0
            a["code_dels"] += f.get("deletions") or 0
            a["langs"][lang] += (f.get("additions") or 0) + (f.get("deletions") or 0)

    # Derive per-author scalars.
    for k, a in by_author.items():
        a["net"] = a["code_adds"] - a["code_dels"]
        a["churn"] = a["code_adds"] + a["code_dels"]
        a["repos_count"] = len(a["repos"])
        a["langs_count"] = sum(1 for v in a["langs"].values() if v > 0)
        a["unique_files_count"] = len(a["unique_files"])
        if a["lines_per_commit_samples"]:
            a["median_lines_per_commit"] = int(statistics.median(a["lines_per_commit_samples"]))
            a["median_files_per_commit"] = int(statistics.median(a["files_per_commit_samples"]))
        else:
            a["median_lines_per_commit"] = None
            a["median_files_per_commit"] = None
        # Build:cleanup ratio (avoid div-by-zero, clamp)
        if a["code_dels"] > 0:
            a["build_ratio"] = round(a["code_adds"] / a["code_dels"], 2)
        elif a["code_adds"] > 0:
            a["build_ratio"] = float("inf")
        else:
            a["build_ratio"] = 0.0

    # Composite score (normalize each dimension to [0,1] across all authors,
    # then weighted sum). Goal: reward sustained delivery (volume), breadth of
    # work (repos + langs), and surgical focus (lower is better → invert).
    keys = list(by_author)

    def _norm(values: list[float]) -> dict[str, float]:
        if not values:
            return {}
        lo, hi = min(values), max(values)
        if hi == lo:
            return {k: 0.5 for k, _ in zip(keys, values)}
        return {k: (v - lo) / (hi - lo) for k, v in zip(keys, values)}

    n_commits = _norm([by_author[k]["commits"] for k in keys])
    n_churn = _norm([math.log1p(by_author[k]["churn"]) for k in keys])  # log to dampen outliers
    n_repos = _norm([by_author[k]["repos_count"] for k in keys])
    n_langs = _norm([by_author[k]["langs_count"] for k in keys])
    # focus: invert median_files_per_commit (lower is better → higher score)
    fpc = [by_author[k]["median_files_per_commit"] or 0 for k in keys]
    focus_raw = [-x for x in fpc]
    n_focus = _norm(focus_raw)

    WEIGHTS = {
        "commits": 0.25,
        "churn":   0.30,
        "repos":   0.15,
        "langs":   0.10,
        "focus":   0.20,
    }
    for k in keys:
        a = by_author[k]
        a["score"] = round(
            WEIGHTS["commits"] * n_commits[k]
            + WEIGHTS["churn"] * n_churn[k]
            + WEIGHTS["repos"] * n_repos[k]
            + WEIGHTS["langs"] * n_langs[k]
            + WEIGHTS["focus"] * n_focus[k],
            3,
        )

    # CLI output.
    print(f"\n{'='*88}")
    print(f"DEVELOPER ANALYTICS — composite score (weights: {WEIGHTS})")
    print(f"{'='*88}")
    ranked = sorted(by_author.items(), key=lambda kv: -kv[1]["score"])
    print(f"{'#':>3}  {'Author':24} {'Score':>5}  {'Commits':>7} {'+LOC':>7} {'-LOC':>7} {'Net':>7}  {'Repos':>5} {'Langs':>5}  {'F/C':>4} {'L/C':>5}  Top langs")
    for rank, (k, a) in enumerate(ranked[:args.top], 1):
        name = canonical[k][:24]
        top_langs = ", ".join(f"{l}" for l, _ in a["langs"].most_common(3) if l != "Other")
        print(f"{rank:>3}  {name:24} {a['score']:>5.3f}  "
              f"{a['commits']:>7} {a['code_adds']:>7} {a['code_dels']:>7} {a['net']:>+7}  "
              f"{a['repos_count']:>5} {a['langs_count']:>5}  "
              f"{(a['median_files_per_commit'] or 0):>4} {(a['median_lines_per_commit'] or 0):>5}  "
              f"{top_langs}")

    print(f"\n--- KEY ---")
    print("  Score:   composite, range 0-1 (weights shown above)")
    print(f"  +LOC/-LOC: net code lines added/removed (excludes lockfiles, *.min, *.map, *.lock)")
    print(f"  F/C:     median files per commit (lower = more focused; bulk commits >{BULK_FILES} files excluded)")
    print(f"  L/C:     median LOC per commit (bulk commits >{BULK_LINES} lines excluded)")
    print(f"  Repos:   distinct repos touched")
    print(f"  Langs:   distinct language buckets touched (Kotlin, Python, TS, Config, Docs, ...)")

    print(f"\n--- 'BUILD vs CLEANUP' (adds:dels ratio, top 10 by churn) ---")
    by_churn = sorted(by_author.items(), key=lambda kv: -kv[1]["churn"])[:10]
    for k, a in by_churn:
        ratio = a["build_ratio"]
        ratio_str = f"{ratio}x" if ratio != float("inf") else "∞"
        verdict = "🟢 mostly building" if ratio > 3 else ("🟡 mixed" if ratio > 1 else "🔴 cleanup-heavy")
        print(f"  {canonical[k][:24]:24} +{a['code_adds']:>7} / -{a['code_dels']:>6} = {ratio_str:>7}  {verdict}")

    print(f"\n--- BREADTH leaders (distinct languages) ---")
    breadth = sorted(by_author.items(), key=lambda kv: -kv[1]["langs_count"])[:10]
    for k, a in breadth:
        top_langs = ", ".join(f"{l}({c})" for l, c in a["langs"].most_common(5) if l != "Other")
        print(f"  {canonical[k][:24]:24} {a['langs_count']:>2} langs · {top_langs}")

    print(f"\n--- FOCUS leaders (smallest median commit, min 5 commits) ---")
    focus = [(k, a) for k, a in by_author.items()
             if a["commits"] >= 5 and a["median_files_per_commit"] is not None]
    focus.sort(key=lambda kv: (kv[1]["median_files_per_commit"], kv[1]["median_lines_per_commit"]))
    for k, a in focus[:10]:
        print(f"  {canonical[k][:24]:24} median {a['median_files_per_commit']} files / "
              f"{a['median_lines_per_commit']} lines per commit  (over {a['commits']} commits)")

    if args.html_out:
        write_html(args.html_out, ranked[:args.top], canonical, WEIGHTS, by_author)
        print(f"\nHTML report → {args.html_out}")

    return 0


def write_html(path: str, ranked: list, canonical: dict, weights: dict, by_author: dict) -> None:
    rows = []
    for i, (k, a) in enumerate(ranked, 1):
        top_langs = ", ".join(f"{l}" for l, _ in a["langs"].most_common(4) if l != "Other")
        rows.append((i, html.escape(canonical[k]), a))
    weights_str = " · ".join(f"{k}={v}" for k, v in weights.items())

    html_doc = f"""<!doctype html><html><head><meta charset='utf-8'><title>Jarvis dev analytics</title>
<style>
:root{{--bg:#0d1117;--panel:#161b22;--border:#30363d;--fg:#e6edf3;--muted:#8b949e;--accent:#79c0ff;--green:#56d364;--red:#ff7b72;}}
*{{box-sizing:border-box}}
body{{margin:0;padding:30px 40px;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;font-size:14px;line-height:1.5}}
h1{{margin:0 0 8px;font-size:22px}}
.sub{{color:var(--muted);font-size:12px;margin-bottom:24px}}
table{{border-collapse:collapse;width:100%;margin:12px 0}}
th,td{{border:1px solid var(--border);padding:6px 9px;text-align:left;vertical-align:top;font-size:12.5px}}
th{{background:#1f242c;color:var(--accent);font-weight:600}}
tr:nth-child(even) td{{background:#1c2129}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.score{{font-weight:600;color:var(--green)}}
code{{font-family:"SF Mono",Menlo,monospace;font-size:12px;background:#1f242c;color:var(--accent);padding:1px 5px;border-radius:3px;border:1px solid var(--border)}}
.foot{{color:var(--muted);font-size:11px;margin-top:20px;border-top:1px solid var(--border);padding-top:12px}}
</style></head><body>
<h1>Jarvis dev analytics — April + May 2026</h1>
<div class="sub">Composite score weights: <code>{html.escape(weights_str)}</code> · LOC excludes lockfiles/min/map/sum · bulk commits (>{BULK_FILES} files / >{BULK_LINES} lines) excluded from focus stats.</div>
<table>
<thead><tr>
<th>#</th><th>Author</th><th class='num'>Score</th>
<th class='num'>Commits</th><th class='num'>+LOC</th><th class='num'>-LOC</th><th class='num'>Net</th>
<th class='num'>Repos</th><th class='num'>Langs</th>
<th class='num'>F/C</th><th class='num'>L/C</th>
<th>Top langs</th>
</tr></thead>
<tbody>
"""
    for i, name, a in rows:
        top_langs = ", ".join(html.escape(l) for l, _ in a["langs"].most_common(4) if l != "Other")
        html_doc += (
            f"<tr><td class='num'>{i}</td><td>{name}</td>"
            f"<td class='num score'>{a['score']:.3f}</td>"
            f"<td class='num'>{a['commits']}</td>"
            f"<td class='num'>{a['code_adds']}</td>"
            f"<td class='num'>{a['code_dels']}</td>"
            f"<td class='num'>{a['net']:+d}</td>"
            f"<td class='num'>{a['repos_count']}</td>"
            f"<td class='num'>{a['langs_count']}</td>"
            f"<td class='num'>{a['median_files_per_commit'] or 0}</td>"
            f"<td class='num'>{a['median_lines_per_commit'] or 0}</td>"
            f"<td>{top_langs}</td></tr>\n"
        )
    html_doc += """</tbody></table>
<div class='foot'>Score is a relative ranking, not an absolute quality measure. Weights are tunable; changing them will reshuffle the ranking.</div>
</body></html>"""
    Path(path).expanduser().write_text(html_doc, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
