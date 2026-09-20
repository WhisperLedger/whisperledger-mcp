"""Analyze the jupitermoney repo estate to scope a 'finish everything' indexing run.

Refreshes metadata via gh, then applies filters:
- active in last N days
- not archived / fork / empty
- not infra (HCL, terraform, *.internal, k8s-only repos)
- not poc/demo/playground/generator
- not already indexed

Prints counts + size + sample, and writes /tmp/jarvis_candidates.txt with one
repo name per line for downstream cloning.
"""
from __future__ import annotations
import argparse
import json
import subprocess
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

INDEXED = {
    # Phase 0
    "bff-core", "platform", "lms", "gateway", "jupiter",
    # Tier 1
    "bullet", "lending-orchestrator", "cardboard", "brahma", "metal",
    # Tier 2
    "mf-order-xpress", "mf-explore-service", "insurance-platform", "bills",
    "ppi-rail", "ppi-pots", "ppi-router",
    # Tier 3 (banking cluster)
    "banking", "general-ledger-accountant", "bank-transfer-merchant",
    "ppi-accounting-service", "ds-jm-account-aggregator-service",
    "savings-account-ob", "kyc-service", "investment",
    "kyc-csb-callback", "kyc-csb-ekyc-callback",
}

INFRA_NAME_HINTS = (
    "terraform", "infra", "-prod.", "-staging", "-dev.", "oci", "aws-", "gcp-",
    "k8s", "ansible", "helm",
)
SKIP_NAME_HINTS = (
    "-poc", "-demo", "playground", "sandbox", "scratch",
    "test-", "-test", "openapi-generator", "generator-templates",
    "archive", "deprecated", "old-", "-old", "backup",
    # Asset / non-source repos
    "fastlane", "-certs", "-resources", "retool", "-notebooks", "jupyterhub",
    # Connector / external-platform config (not Jupiter source code per se)
    "cp-kafka", "cp-",
)

# Whitelist of "real source code" languages — drop everything else.
# (Drops Handlebars/Mustache/HTML/CSS template repos, Jupyter Notebook data work,
# and "none" repos which are usually pure docs / metadata.)
CODE_LANGS = {
    "Kotlin", "Scala", "Java",
    "Python",
    "TypeScript", "JavaScript",
    "Go", "Rust", "Ruby", "Swift",
    "C", "C++", "C#",
    "Shell",  # bash scripts are legit infra-glue code; usually small
}


def likely_infra(r: dict) -> bool:
    name = r["name"].lower()
    primary = r.get("primaryLanguage") or {}
    lang = primary.get("name", "") if primary else ""
    if lang in ("HCL", "Terraform"):
        return True
    if name.endswith(".internal") or name.endswith(".jupiter.money"):
        return True
    return any(h in name for h in INFRA_NAME_HINTS)


def likely_skip(r: dict) -> bool:
    n = r["name"].lower()
    return any(h in n for h in SKIP_NAME_HINTS)


def pushed_dt(r: dict) -> datetime:
    return datetime.fromisoformat(r["pushedAt"].replace("Z", "+00:00")).replace(tzinfo=None)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90, help="Activity cutoff in days")
    ap.add_argument("--out", default="/tmp/jarvis_candidates.txt")
    args = ap.parse_args()

    repos_json = Path.home() / "jarvis" / "index" / "repos_v2.json"
    print("=== refreshing metadata via gh ===")
    subprocess.run(
        ["gh", "repo", "list", "jupitermoney", "--limit", "500", "--json",
         "name,description,pushedAt,primaryLanguage,isArchived,isPrivate,isFork,isEmpty,diskUsage,visibility,repositoryTopics"],
        check=True,
        stdout=repos_json.open("w"),
    )

    repos = json.loads(repos_json.read_text())
    total = len(repos)
    active = [r for r in repos if not r["isArchived"] and not r["isFork"] and not r["isEmpty"]]
    now = datetime.utcnow()
    recent = [r for r in active if pushed_dt(r) > now - timedelta(days=args.days)]

    def lang_of(r):
        p = r.get("primaryLanguage") or {}
        return (p.get("name") or "") if p else ""

    n_indexed = sum(1 for r in recent if r["name"] in INDEXED)
    n_infra = sum(1 for r in recent if r["name"] not in INDEXED and likely_infra(r))
    n_skip = sum(1 for r in recent if r["name"] not in INDEXED and not likely_infra(r) and likely_skip(r))
    n_lang = sum(1 for r in recent
                 if r["name"] not in INDEXED
                 and not likely_infra(r) and not likely_skip(r)
                 and lang_of(r) not in CODE_LANGS)

    candidates = [
        r for r in recent
        if r["name"] not in INDEXED
        and not likely_infra(r)
        and not likely_skip(r)
        and lang_of(r) in CODE_LANGS
    ]

    print(f"\n=== topline ===")
    print(f"  total in org:                       {total}")
    print(f"  non-archived/fork/empty:            {len(active)}")
    print(f"  active in last {args.days}d:                  {len(recent)}")
    print()
    print(f"=== applied filters to last-{args.days}d set ===")
    print(f"  - already indexed:                  -{n_indexed}")
    print(f"  - infra/terraform/internal:         -{n_infra}")
    print(f"  - poc/demo/test/generator/etc:      -{n_skip}")
    print(f"  - non-code language (notebooks etc):-{n_lang}")
    print(f"  -----------------------------------------")
    print(f"  CANDIDATE NEW REPOS:                 {len(candidates)}")

    langs = Counter(
        ((r.get("primaryLanguage") or {}).get("name", "none") if r.get("primaryLanguage") else "none")
        for r in candidates
    )
    print(f"\n  language mix:")
    for lang, n in langs.most_common():
        print(f"    {lang:15} {n}")

    sizes = sorted(r.get("diskUsage", 0) for r in candidates)
    if sizes:
        print(f"\n  size distribution (GitHub diskUsage, KB):")
        print(f"    median:  {sizes[len(sizes)//2]:,} KB")
        print(f"    p75:     {sizes[3*len(sizes)//4]:,} KB")
        print(f"    p95:     {sizes[int(0.95*len(sizes))]:,} KB")
        biggest = max(candidates, key=lambda r: r.get("diskUsage", 0))
        print(f"    max:     {biggest.get('diskUsage', 0):,} KB ({biggest['name']})")
        print(f"    total:   {sum(sizes):,} KB ({sum(sizes)/1024:.0f} MB)")

    print(f"\n  largest 12 candidates:")
    for r in sorted(candidates, key=lambda r: -r.get("diskUsage", 0))[:12]:
        lang = ((r.get("primaryLanguage") or {}).get("name", "-") if r.get("primaryLanguage") else "-")
        size_mb = r.get("diskUsage", 0) / 1024
        desc = (r.get("description") or "")[:55]
        print(f"    {size_mb:8.1f} MB  {lang:12}  {r['name']:38}  {desc}")

    # Write the list for cloning
    Path(args.out).write_text("\n".join(r["name"] for r in candidates) + "\n")
    print(f"\nwrote {args.out} with {len(candidates)} repo names")

    # Indexing cost estimate: ~1500 chunks per medium repo * ~600 tokens = ~0.9M tokens.
    # Voyage code-3 at $0.18/M.
    est_chunks = len(candidates) * 1500
    est_tokens_M = est_chunks * 600 / 1_000_000
    est_cost = est_tokens_M * 0.18
    print(f"\n=== rough cost estimate (very approximate) ===")
    print(f"  est. chunks:       ~{est_chunks:,}")
    print(f"  est. Voyage cost:  ~${est_cost:.0f} one-time + same per daily reindex")


if __name__ == "__main__":
    main()
