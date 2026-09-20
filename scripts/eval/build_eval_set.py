"""Build eval set v1 from qa_log.jsonl.

Approach: sample queries Jarvis already answered, extract the citations
from each answer as auto-ground-truth, then hand-validate a sample. This
is a self-eval (gives Jarvis credit for past correct retrievals) but it's
a reasonable starting point — better than smoke queries, weaker than
hand-curated ground truth. Future versions will hand-curate.

Filters:
  - Question length 25-300 chars (substantive but not essays)
  - Answer mentions at least one repo+path citation
  - User did NOT thumbs-down the answer (sampled if feedback exists)
  - Dedupe near-identical questions

Output: scripts/eval/jarvis_eval_v1.jsonl, one record per query:
  {qid, q, expected: [{repo, path, ...}], source: "auto-from-qa_log",
   ts: original-answer-ts}
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

QA_LOG = Path("/home/ubuntu/jarvis/logs/qa_log.jsonl")
FEEDBACK_LOG = Path("/home/ubuntu/jarvis/logs/feedback.jsonl")
OUT_PATH = Path("/home/ubuntu/jarvis/scripts/eval/jarvis_eval_v1.jsonl")

# Citation patterns we expect in answers:
#   <https://github.com/jupitermoney/<repo>/blob/<sha>/<path>#L<a>-L<b>|repo/path:a-b>
#   `<repo>/<path>:<start>-<end>` (older format)
#   `<repo>/<path>` (no line range)
GH_PERMALINK = re.compile(
    r"https?://github\.com/jupitermoney/([\w.\-]+)/blob/[a-f0-9HEAD]+/([^#|\s)]+)(?:#L(\d+)(?:-L(\d+))?)?"
)
INLINE_CITE = re.compile(
    r"`([a-z0-9][\w.\-]*?)/([\w/.\-]+\.(?:kt|kts|java|ts|tsx|js|jsx|py|scala|sql|yaml|yml|md|graphql|gql|proto|tf|hcl))`"
    r"(?::(\d+)(?:-(\d+))?)?",
    re.I,
)

INDEXED_REPOS = set()
for line in Path("/home/ubuntu/jarvis/scripts/indexed_repos.txt").read_text().splitlines():
    line = line.split("#", 1)[0].strip()
    if line:
        INDEXED_REPOS.add(line)


def extract_citations(answer: str) -> list[dict]:
    """Pull (repo, path, start_line, end_line) from an answer's citations."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for m in GH_PERMALINK.finditer(answer):
        repo, path = m.group(1), m.group(2)
        if repo not in INDEXED_REPOS:
            continue
        start = int(m.group(3)) if m.group(3) else None
        end = int(m.group(4)) if m.group(4) else start
        key = (repo, path)
        if key in seen:
            continue
        seen.add(key)
        out.append({"repo": repo, "path": path,
                    "start_line": start, "end_line": end,
                    "source": "permalink"})
    for m in INLINE_CITE.finditer(answer):
        repo, path = m.group(1), m.group(2)
        if repo not in INDEXED_REPOS:
            continue
        start = int(m.group(3)) if m.group(3) else None
        end = int(m.group(4)) if m.group(4) else start
        key = (repo, path)
        if key in seen:
            continue
        seen.add(key)
        out.append({"repo": repo, "path": path,
                    "start_line": start, "end_line": end,
                    "source": "inline"})
    return out


def load_feedback() -> dict[str, str]:
    """qid -> 'up' / 'down' if user clicked a button."""
    out: dict[str, str] = {}
    if not FEEDBACK_LOG.exists():
        return out
    for line in FEEDBACK_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            qid = r.get("qid")
            val = r.get("value") or r.get("rating") or r.get("feedback")
            if qid and val:
                out[qid] = "up" if "up" in str(val).lower() or "👍" in str(val) else "down" if "down" in str(val).lower() or "👎" in str(val) else None
        except Exception:
            continue
    return out


def main():
    feedback = load_feedback()
    candidates = []
    seen_q = set()
    for line in QA_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        q = (r.get("q") or "").strip()
        ans = r.get("answer") or ""
        qid = r.get("qid")
        ts = r.get("ts")
        if not q or len(q) < 25 or len(q) > 300:
            continue
        # Skip thumbs-down
        if feedback.get(qid) == "down":
            continue
        # Dedupe near-identical questions (normalized)
        norm = re.sub(r"\s+", " ", q.lower())[:120]
        if norm in seen_q:
            continue
        cites = extract_citations(ans)
        if not cites:
            continue
        seen_q.add(norm)
        candidates.append({
            "qid": qid, "q": q, "ts": ts,
            "expected": cites,
            "feedback": feedback.get(qid, "none"),
            "source": "auto-from-qa_log",
        })

    print(f"[build_eval_set] {len(candidates)} candidates after filtering")

    # Sample 50 diverse — bucket by topic keyword
    def bucket(q):
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

    by_bucket = defaultdict(list)
    for c in candidates:
        by_bucket[bucket(c["q"])].append(c)

    print(f"[build_eval_set] buckets: " + ", ".join(f"{k}={len(v)}" for k, v in sorted(by_bucket.items())))

    # Round-robin sample 50
    target = 50
    sample = []
    buckets = list(by_bucket.values())
    while len(sample) < target and any(buckets):
        for b in buckets:
            if b and len(sample) < target:
                sample.append(b.pop(0))
        if not any(buckets):
            break

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w") as f:
        for r in sample:
            f.write(json.dumps(r) + "\n")
    print(f"[build_eval_set] wrote {len(sample)} queries to {OUT_PATH}")
    # Show first 3 for sanity
    for r in sample[:3]:
        print(f"\n  qid={r['qid'][:8]}  q={r['q'][:100]!r}")
        for e in r["expected"][:2]:
            print(f"    expect: {e['repo']}/{e['path']}:{e.get('start_line')}-{e.get('end_line')}")


if __name__ == "__main__":
    main()
