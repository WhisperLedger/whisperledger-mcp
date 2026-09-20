"""Build the Jupiter cross-repo service registry.

Scans all clones in ~/jarvis/repos for evidence of internal service deployment +
consumption, and emits a structured JSON registry that downstream tools
(agent.lookup_service, MCP, HTTP /api/v1/services/{name}) can read.

For each unique service-name (e.g. `bullet-ms`, `lending-lifecycle-manager-ms`,
`deposit-platform-blostem`), we aggregate:
  - K8s in-cluster URL (svc.cluster.local form) + namespace + port
  - Route53 cross-cluster URL (.internal form) + AWS account
  - consumer repos (those that hardcode either URL form)
  - source repo (best-guess: where its OpenAPI spec lives)
  - exposed paths (from the spec.yml / api.yaml / Play conf/routes)
  - auth pattern (inferred from consumer Feign @Headers)
  - sample consumer Feign-client file for further drilling

Output: ~/jarvis/index/service_registry.json (gitignored, same parent dir
as Qdrant data so resurrection-runbook covers it).

Run:
  cd ~/jarvis/scripts && ./indexer/.venv/bin/python build_service_registry.py
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone

REPOS_DIR = Path("/home/ubuntu/jarvis/repos")
OUT_PATH = Path("/home/ubuntu/jarvis/index/service_registry.json")

# Regex for the two hostname conventions
# K8s in-cluster:  http(s)?://<svc>.<ns>.svc.cluster.local(:<port>)?
K8S_RE = re.compile(
    r"https?://([a-zA-Z0-9_-]+)\.([a-zA-Z0-9_-]+)\.svc\.cluster\.local(?::(\d+))?"
)
# Route53 internal:  http(s)?://<host>.<account>.internal(:<port>)?
R53_RE = re.compile(
    r"https?://([a-zA-Z0-9_.-]+)\.([a-zA-Z0-9_-]+)\.internal(?::(\d+))?"
)

# Yaml extensions we'll grep
YAML_EXTS = (".yml", ".yaml")

# Spec file glob patterns (where servers declare their canonical paths)
SPEC_PATTERNS = [
    "**/api/spec.yml", "**/api/spec.yaml",
    "**/specs/v1/api.yaml", "**/specs/v1/api.yml",
    "**/src/main/resources/specs/**/api.yaml",
    "**/src/main/resources/specs/**/api.yml",
    "**/public/specs/specs.json",   # play-style swagger
    "**/openapi.yaml", "**/openapi.yml",
]


def iter_repo_files(repo_root: Path, extensions: tuple[str, ...]) -> list[Path]:
    """Walk a repo's tree returning files matching extensions, excluding noise dirs."""
    excluded = {"node_modules", ".git", "build", "dist", "target",
                "ios", "android"}  # ios/Pods, android/build covered by ios/android
    out: list[Path] = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in excluded]
        for f in files:
            if f.endswith(extensions):
                out.append(Path(root) / f)
    return out


def scan_urls_in_repo(repo_root: Path) -> tuple[list[dict], list[dict]]:
    """For one repo, return (k8s_hits, r53_hits). Each hit:
       {svc, ns_or_account, port, file (rel to repo), line, raw}"""
    k8s_hits: list[dict] = []
    r53_hits: list[dict] = []
    for fp in iter_repo_files(repo_root, YAML_EXTS):
        try:
            with fp.open() as f:
                for lineno, line in enumerate(f, start=1):
                    for m in K8S_RE.finditer(line):
                        svc, ns, port = m.group(1), m.group(2), m.group(3) or ""
                        k8s_hits.append({
                            "svc": svc, "ns": ns, "port": port or "default",
                            "file": str(fp.relative_to(repo_root)),
                            "line": lineno, "raw": m.group(0),
                        })
                    for m in R53_RE.finditer(line):
                        host, account, port = m.group(1), m.group(2), m.group(3) or ""
                        r53_hits.append({
                            "svc": host, "account": account, "port": port or "default",
                            "file": str(fp.relative_to(repo_root)),
                            "line": lineno, "raw": m.group(0),
                        })
        except Exception:
            # binary file masquerading as .yaml, encoding issues, etc.
            continue
    return k8s_hits, r53_hits


def find_spec_files(repo_root: Path) -> list[Path]:
    """Find canonical OpenAPI/Play spec files within a repo."""
    found = set()
    for pat in SPEC_PATTERNS:
        for fp in repo_root.glob(pat):
            if fp.is_file():
                found.add(fp)
    # Play services: conf/routes
    routes = repo_root / "backend" / "conf" / "routes"
    if routes.is_file():
        found.add(routes)
    routes2 = repo_root / "conf" / "routes"
    if routes2.is_file():
        found.add(routes2)
    return sorted(found)


def extract_paths_from_spec(spec_file: Path) -> list[str]:
    """Extract HTTP paths from an OpenAPI yaml/json or a Play conf/routes file."""
    paths: list[str] = []
    try:
        text = spec_file.read_text(errors="ignore")
    except Exception:
        return paths
    # OpenAPI YAML: lines starting with two-space-indent then '/'
    if spec_file.suffix in (".yaml", ".yml"):
        for m in re.finditer(r"^(?:  )(/[^:\s]+)\s*:\s*$", text, re.M):
            paths.append(m.group(1))
    # OpenAPI JSON (specs.json style — keys are "/path")
    elif spec_file.suffix == ".json":
        try:
            d = json.loads(text)
            for p in (d.get("paths") or {}).keys():
                paths.append(p)
        except Exception:
            pass
    # Play conf/routes: lines like `GET  /path/...   controller.action`
    elif spec_file.name == "routes":
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # METHOD  PATH  HANDLER
            m = re.match(r"^([A-Z]+|->)\s+(/\S+)\s+\S+", line)
            if m:
                paths.append(m.group(2))
    return sorted(set(paths))


def find_feign_client_for(svc_name: str, all_repos: list[Path]) -> str | None:
    """Best-effort: locate a Feign/Retrofit interface file that calls this service.
    Searches for `@RequestLine("...<svc-prefix>...")` patterns across repos.
    Returns the relative file path of the first match.
    """
    # Extract a prefix from the svc name (strip -ms, capitalize)
    prefix = svc_name.removesuffix("-ms").removesuffix("ms")
    # search via grep for class names that look like clients for this service
    needle = f"@RequestLine.*\\b{prefix}/v"
    try:
        r = subprocess.run(
            ["grep", "-rIlE", "--include=*.kt", "--include=*.java",
             needle, str(REPOS_DIR)],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            first = r.stdout.strip().splitlines()[0]
            return str(Path(first).relative_to(REPOS_DIR))
    except Exception:
        pass
    return None


def _build_gradle_module_map(all_repos: list[Path]) -> dict[str, str]:
    """Walk settings.gradle.kts / build.sbt across repos, build module-name → repo map.
    e.g. settings.gradle.kts in `jupiter-investments` lists ":deposit-platform" →
    map["deposit-platform"] = "jupiter-investments".
    """
    m: dict[str, str] = {}
    for repo in all_repos:
        for gradle in [repo / "settings.gradle.kts", repo / "settings.gradle"]:
            if gradle.is_file():
                try:
                    text = gradle.read_text(errors="ignore")
                except Exception:
                    continue
                for match in re.finditer(r'(?:include|springBootProjects[^)]*spec)\s*\(?\s*["\']?:?([a-zA-Z0-9_-]+)["\']?\s*\)?', text):
                    mod = match.group(1).strip(":")
                    if mod and mod not in m:
                        m[mod] = repo.name
        sbt = repo / "build.sbt"
        if sbt.is_file():
            try:
                text = sbt.read_text(errors="ignore")
            except Exception:
                continue
            # Project("name", file("...")) or lazy val name = project.in(...)
            for match in re.finditer(r'(?:Project\(|lazy\s+val\s+)([a-zA-Z0-9_]+)\s*[=]?\s*(?:project\.in|file\()?', text):
                mod = match.group(1)
                if mod and mod not in {"root", "settings"} and mod not in m:
                    m[mod] = repo.name
    return m


def infer_source_repo(
    svc_name: str,
    all_specs: list[tuple[str, Path, list[str]]],
    repo_names: set[str],
    module_map: dict[str, str],
) -> tuple[str, str | None, list[str]] | None:
    """Find the source repo for a service. Returns (repo, spec_rel_path | None, paths).
    Strategies (first match wins):
      A. Service name (minus -ms) matches a repo name directly  →  e.g. bullet-ms → bullet
      B. Service name (minus -ms) matches a Gradle/SBT module  →  e.g. deposit-platform-blostem (strip -blostem suffix) → deposit-platform module in jupiter-investments
      C. Path-prefix match against any spec  →  original heuristic
    """
    needle = svc_name.removesuffix("-ms").lower()

    def _find_spec_for(repo_name: str) -> tuple[str | None, list[str]]:
        """Pick the best spec file for repo_name (or any of its sub-modules).
        Return (spec_rel_path, paths). If no spec, (None, [])."""
        candidates = [
            (s_repo, sf, paths) for (s_repo, sf, paths) in all_specs
            if s_repo == repo_name
        ]
        if not candidates:
            return None, []
        # Prefer specs whose paths look most like this service
        candidates.sort(key=lambda c: (
            -sum(1 for p in c[2] if needle in p.lower()),  # path-name affinity
            -len(c[2]),  # then by path count (richer specs first)
        ))
        _, sf, paths = candidates[0]
        rel = str(sf.relative_to(REPOS_DIR / repo_name))
        return rel, paths

    # Strategy A: direct repo-name match
    if needle in repo_names:
        spec_rel, paths = _find_spec_for(needle)
        return needle, spec_rel, paths

    # Strategy B: progressive suffix-strip to match a Gradle/SBT module
    # e.g. "deposit-platform-blostem" → try "deposit-platform-blostem", then "deposit-platform", then "deposit"
    parts = needle.split("-")
    while parts:
        candidate = "-".join(parts)
        if candidate in module_map:
            repo = module_map[candidate]
            spec_rel, paths = _find_spec_for(repo)
            return repo, spec_rel, paths
        if candidate in repo_names:
            spec_rel, paths = _find_spec_for(candidate)
            return candidate, spec_rel, paths
        parts.pop()  # try shorter prefix

    # Strategy C: path-prefix match (original heuristic)
    cands = []
    for repo, sf, paths in all_specs:
        hits = sum(1 for p in paths if p.lower().startswith(f"/{needle}/") or p.lower().startswith(f"/{needle}-"))
        if hits > 0:
            cands.append((hits, repo, sf, paths))
    if cands:
        cands.sort(key=lambda x: -x[0])
        _, repo, sf, paths = cands[0]
        rel = str(sf.relative_to(REPOS_DIR / repo))
        return repo, rel, paths

    return None


def load_overrides() -> dict:
    """Load operator-curated overrides (manual cross-cluster URLs, aliases, source-
    repo fixes for cases the auto-discovery can't figure out). Optional file.
    """
    path = Path("/home/ubuntu/jarvis/scripts/service_registry_overrides.json")
    if not path.is_file():
        return {"services": {}}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"[registry] WARN: overrides file unreadable: {e}", flush=True)
        return {"services": {}}


def apply_overrides(by_svc: dict, overrides: dict) -> int:
    """Merge overrides into the auto-discovered registry. Returns number of services
    touched. Override semantics:
      - r53 / k8s: list append (preserve auto-discovered + add manual)
      - source_repo / spec_file / paths: replace if non-empty
      - aliases: list append
      - manual_paths: extend paths
      - any other field: replace
    """
    touched = 0
    for svc_name, override in (overrides.get("services") or {}).items():
        entry = by_svc.setdefault(svc_name, {
            "name": svc_name, "k8s": [], "r53": [], "consumers": [],
            "consumer_count": 0, "_consumer_files": [],
            "source_repo": None, "spec_file": None, "paths": [],
            "sample_feign_client": None,
        })
        for key, value in override.items():
            if key in ("k8s", "r53"):
                # Append manual entries that aren't already present
                existing_urls = {x.get("url") for x in entry.get(key, [])}
                for x in value:
                    if x.get("url") not in existing_urls:
                        entry.setdefault(key, []).append(x)
            elif key == "aliases":
                entry.setdefault("aliases", [])
                for a in value:
                    if a not in entry["aliases"]:
                        entry["aliases"].append(a)
            elif key == "manual_paths":
                entry.setdefault("paths", []).extend(value)
                entry["paths"] = sorted(set(entry["paths"]))
            elif key in ("source_repo", "spec_file") and value:
                entry[key] = value
            elif key == "paths" and value:
                entry["paths"] = sorted(set(entry.get("paths", []) + list(value)))
            else:
                entry[key] = value
        entry["_overridden"] = True
        touched += 1
    return touched


def main() -> int:
    started = datetime.now(timezone.utc)
    all_repos = sorted([p for p in REPOS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")])
    print(f"[registry] scanning {len(all_repos)} repos under {REPOS_DIR}", flush=True)

    # Phase 1: scan every repo for K8s + Route53 URLs
    # Aggregate by service-name (the first hostname component)
    by_svc: dict[str, dict] = {}
    for repo in all_repos:
        k8s, r53 = scan_urls_in_repo(repo)
        for h in k8s:
            entry = by_svc.setdefault(h["svc"], {
                "name": h["svc"],
                "k8s": [],
                "r53": [],
                "consumers": set(),
                "_consumer_files": [],
            })
            entry["k8s"].append({
                "url": h["raw"], "ns": h["ns"], "port": h["port"],
            })
            entry["consumers"].add(repo.name)
            entry["_consumer_files"].append({
                "repo": repo.name, "file": h["file"], "line": h["line"],
            })
        for h in r53:
            entry = by_svc.setdefault(h["svc"], {
                "name": h["svc"],
                "k8s": [],
                "r53": [],
                "consumers": set(),
                "_consumer_files": [],
            })
            entry["r53"].append({
                "url": h["raw"], "account": h["account"], "port": h["port"],
            })
            entry["consumers"].add(repo.name)
            entry["_consumer_files"].append({
                "repo": repo.name, "file": h["file"], "line": h["line"],
            })

    print(f"[registry] found {len(by_svc)} distinct services across all consumer configs",
          flush=True)

    # Phase 2: collect every spec file across all repos (heavy, do once)
    print("[registry] collecting spec files org-wide...", flush=True)
    all_specs: list[tuple[str, Path, list[str]]] = []
    for repo in all_repos:
        for sf in find_spec_files(repo):
            paths = extract_paths_from_spec(sf)
            if paths:
                all_specs.append((repo.name, sf, paths))
    print(f"[registry] {len(all_specs)} spec files found", flush=True)

    # Phase 3a: build helpers for source-repo inference
    repo_names = {p.name for p in all_repos}
    print("[registry] walking Gradle/SBT module lists...", flush=True)
    module_map = _build_gradle_module_map(all_repos)
    print(f"[registry] {len(module_map)} gradle/sbt modules mapped to repos", flush=True)

    # Phase 3b: for each service, infer source repo + collect paths + find feign client
    for svc_name, entry in by_svc.items():
        src = infer_source_repo(svc_name, all_specs, repo_names, module_map)
        if src:
            repo, spec_rel, paths = src
            entry["source_repo"] = repo
            entry["spec_file"] = spec_rel
            entry["paths"] = paths
        else:
            entry["source_repo"] = None
            entry["spec_file"] = None
            entry["paths"] = []

        # Feign client lookup (best-effort)
        fc = find_feign_client_for(svc_name, all_repos)
        entry["sample_feign_client"] = fc

    # Phase 4: dedupe k8s / r53 lists (same URL referenced many times)
    for svc_name, entry in by_svc.items():
        seen_k = set()
        entry["k8s"] = [
            x for x in entry["k8s"]
            if not (x["url"] in seen_k or seen_k.add(x["url"]))
        ]
        seen_r = set()
        entry["r53"] = [
            x for x in entry["r53"]
            if not (x["url"] in seen_r or seen_r.add(x["url"]))
        ]
        # set → sorted list for JSON
        entry["consumers"] = sorted(entry["consumers"])
        entry["consumer_count"] = len(entry["consumers"])
        # cap _consumer_files to first 10 to keep JSON manageable
        entry["_consumer_files"] = entry["_consumer_files"][:10]

    # Phase 5: apply operator-curated overrides (cross-cluster URLs, aliases,
    # source-repo manual fixes for what auto-discovery can't infer)
    overrides = load_overrides()
    overridden = apply_overrides(by_svc, overrides)
    print(f"[registry] applied {overridden} override entries", flush=True)

    # Serialise
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "_meta": {
            "generated_at_utc": started.isoformat(),
            "duration_sec": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
            "repos_scanned": len(all_repos),
            "specs_collected": len(all_specs),
            "service_count": len(by_svc),
            "gradle_modules_mapped": len(module_map),
            "overrides_applied": overridden,
        },
        "services": dict(sorted(by_svc.items())),
    }
    with OUT_PATH.open("w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"[registry] wrote {OUT_PATH} ({len(by_svc)} services)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
