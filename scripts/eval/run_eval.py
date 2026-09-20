"""Run search_code against the eval set + report hits@k + MRR + permalink stats.

Usage:
    python -m eval.run_eval
    python -m eval.run_eval --k 10 --save baselines/2026-06-11-post-ast.json
    python -m eval.run_eval --eval scripts/eval/jarvis_eval_v1.jsonl

Reports:
  - hits@1, hits@3, hits@5, hits@10 — does ANY expected (repo,path) appear in top-k
  - MRR — mean reciprocal rank of first-correct hit
  - per-bucket breakdown (see build_eval_set bucket labels)
  - permalink validity rate (does the field exist on every hit?)
"""
import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "/home/ubuntu/jarvis/scripts")

EVAL_DEFAULT = Path("/home/ubuntu/jarvis/scripts/eval/jarvis_eval_v1.jsonl")
BASELINE_DIR = Path("/home/ubuntu/jarvis/scripts/eval/baselines")


def bucket(q: str) -> str:
    low = q.lower()
    if any(k in low for k in ("xstate", "machine", "state machine")): return "xstate"
    if any(k in low for k in ("endpoint", "api", "/v1/", "/v2/")):    return "api"
    if any(k in low for k in ("kafka", "consumer", "producer")):       return "kafka"
    if any(k in low for k in ("kyc", "ckyc", "onboarding")):           return "onboarding"
    if any(k in low for k in ("kotlin", "spring", "controller")):      return "be"
    if any(k in low for k in ("screen", "tsx", "component", "react")): return "fe"
    if any(k in low for k in ("flow", "journey", "diagram")):          return "flow"
    if any(k in low for k in ("where", "find", "lookup", "service")):  return "lookup"
    return "other"


def match(hit: dict, expected: list[dict]) -> bool:
    """A hit matches if (repo, path) equals any expected entry."""
    hr, hp = hit.get("repo"), hit.get("path")
    if not hr or not hp:
        return False
    for e in expected:
        if hr == e.get("repo") and hp == e.get("path"):
            return True
    return False


def run(eval_path: Path, k: int, search_fn=None) -> dict:
    from agent.tools import search_code as _vector_search
    search_code = search_fn if search_fn is not None else _vector_search

    records = []
    for line in eval_path.read_text().splitlines():
        if line.strip():
            records.append(json.loads(line))

    print(f"[run_eval] {len(records)} queries, k={k}")
    per_q: list[dict] = []
    permalink_ok = 0
    permalink_total = 0
    started = time.time()

    for i, r in enumerate(records, 1):
        q = r["q"]
        expected = r["expected"]
        try:
            hits = json.loads(search_code(q, k=k))["hits"]
        except Exception as e:
            per_q.append({"qid": r.get("qid"), "q": q[:80], "error": f"{type(e).__name__}: {e!s}"})
            continue

        # First-correct rank
        first_rank = None
        for rank, hit in enumerate(hits, 1):
            if match(hit, expected):
                first_rank = rank
                break

        # Permalink validity
        for hit in hits:
            permalink_total += 1
            if hit.get("permalink") and "github.com" in hit.get("permalink", ""):
                permalink_ok += 1

        per_q.append({
            "qid": r.get("qid"),
            "q": q[:80],
            "bucket": bucket(q),
            "first_rank": first_rank,
            "hits_top3": [(h["repo"], h["path"]) for h in hits[:3]],
            "expected_first": (expected[0]["repo"], expected[0]["path"]) if expected else None,
        })

        if i % 10 == 0:
            elapsed = time.time() - started
            print(f"  [{i}/{len(records)}] {elapsed:.1f}s elapsed")

    elapsed = time.time() - started

    # Aggregate
    hits_at = {1: 0, 3: 0, 5: 0, 10: 0}
    mrr_total = 0.0
    bucket_stats = defaultdict(lambda: {"n": 0, "hits1": 0, "hits3": 0, "mrr": 0.0})

    for q in per_q:
        if "error" in q:
            continue
        r = q.get("first_rank")
        b = q.get("bucket", "?")
        bucket_stats[b]["n"] += 1
        if r is None:
            continue
        for thresh in hits_at:
            if r <= thresh:
                hits_at[thresh] += 1
        mrr_total += 1.0 / r
        if r <= 1:
            bucket_stats[b]["hits1"] += 1
        if r <= 3:
            bucket_stats[b]["hits3"] += 1
        bucket_stats[b]["mrr"] += 1.0 / r

    n_total = sum(1 for q in per_q if "error" not in q)
    n_errors = sum(1 for q in per_q if "error" in q)
    summary = {
        "n": n_total,
        "n_errors": n_errors,
        "elapsed_sec": round(elapsed, 1),
        "hits@1": round(hits_at[1] / n_total * 100, 1) if n_total else 0,
        "hits@3": round(hits_at[3] / n_total * 100, 1) if n_total else 0,
        "hits@5": round(hits_at[5] / n_total * 100, 1) if n_total else 0,
        "hits@10": round(hits_at[10] / n_total * 100, 1) if n_total else 0,
        "mrr": round(mrr_total / n_total, 3) if n_total else 0,
        "permalink_rate": round(permalink_ok / permalink_total * 100, 1) if permalink_total else 0,
        "buckets": {
            b: {"n": s["n"],
                "hits@1": round(s["hits1"] / s["n"] * 100, 1) if s["n"] else 0,
                "hits@3": round(s["hits3"] / s["n"] * 100, 1) if s["n"] else 0,
                "mrr": round(s["mrr"] / s["n"], 3) if s["n"] else 0}
            for b, s in bucket_stats.items()
        },
    }

    print(f"\n=== Summary ===")
    print(json.dumps(summary, indent=2))
    print(f"\n=== Misses (rank > 5) ===")
    for q in per_q:
        if "error" in q:
            continue
        r = q.get("first_rank")
        if r is None or r > 5:
            print(f"  miss: {q['q']!r}")
            print(f"    expected first: {q['expected_first']}")
            print(f"    got top3: {q['hits_top3']}")

    return {"summary": summary, "per_q": per_q}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", default=str(EVAL_DEFAULT))
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--save", default=None)
    args = ap.parse_args()

    result = run(Path(args.eval), args.k)

    if args.save:
        out = BASELINE_DIR / args.save if not args.save.startswith("/") else Path(args.save)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2))
        print(f"[run_eval] baseline saved to {out}")


if __name__ == "__main__":
    main()
