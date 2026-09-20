"""Run a batch of questions through Jarvis, write a JSONL log + HTML report.

Input formats (auto-detected per line):
  - plain text: one question per line, blank lines + lines starting with '#' ignored
  - JSONL: {"q": "...", "asker": "name", "tags": [...]}

Usage:
  python -m agent.batch questions.txt
  python -m agent.batch questions.txt -o ~/jarvis/logs/batch_$(date +%Y%m%d-%H%M).jsonl

Outputs:
  <out>.jsonl  — one result record per question (resumable; appended as we go)
  <out>.html   — styled report for sharing with engineers
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path
from .agent import ask
from .report import render_report


def parse_questions(path: Path) -> list[dict]:
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("{"):
            out.append(json.loads(line))
        else:
            out.append({"q": line})
    return out


def run(questions: list[dict], out_jsonl: Path) -> list[dict]:
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    with out_jsonl.open("w", encoding="utf-8") as f:
        for i, item in enumerate(questions, 1):
            q = item["q"]
            print(f"\n[{i}/{len(questions)}] {q[:80]}{'...' if len(q) > 80 else ''}", flush=True)
            t0 = time.time()
            try:
                res = ask(q, verbose=False)
                rec = {
                    "i": i,
                    "q": q,
                    "asker": item.get("asker"),
                    "tags": item.get("tags"),
                    "answer": res.answer,
                    "iterations": res.iterations,
                    "tool_calls": res.tool_calls,
                    "input_tokens": res.input_tokens,
                    "output_tokens": res.output_tokens,
                    "cache_read_tokens": res.cache_read_tokens,
                    "cache_creation_tokens": res.cache_creation_tokens,
                    "elapsed_sec": res.elapsed_sec,
                    "ok": True,
                }
                # Sonnet 4.6 list pricing: $3/Mtok in (uncached), $0.30/Mtok cache reads,
                # $3.75/Mtok cache writes, $15/Mtok out. input_tokens already EXCLUDES cache reads.
                rec["est_cost_usd"] = round(
                    (rec["input_tokens"] * 3
                     + rec["cache_read_tokens"] * 0.30
                     + rec["cache_creation_tokens"] * 3.75
                     + rec["output_tokens"] * 15) / 1_000_000, 4
                )
                print(f"  done in {rec['elapsed_sec']}s · {rec['iterations']} iters · "
                      f"{len(rec['tool_calls'])} tool calls · ${rec['est_cost_usd']}", flush=True)
            except Exception as e:
                rec = {
                    "i": i, "q": q, "ok": False, "error": f"{type(e).__name__}: {e}",
                    "elapsed_sec": round(time.time() - t0, 2),
                }
                print(f"  ERROR: {rec['error']}", flush=True)
            records.append(rec)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
    return records


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="Path to questions file (.txt or .jsonl)")
    ap.add_argument("-o", "--out", default=None,
                    help="Output prefix (without extension). Default: ~/jarvis/logs/batch_<ts>")
    args = ap.parse_args()

    questions = parse_questions(Path(args.input).expanduser())
    if not questions:
        print("no questions to run", file=sys.stderr)
        return 1

    if args.out:
        out_prefix = Path(args.out).expanduser()
    else:
        ts = time.strftime("%Y%m%d-%H%M%S")
        out_prefix = Path.home() / "jarvis" / "logs" / f"batch_{ts}"

    out_jsonl = out_prefix.with_suffix(".jsonl")
    out_html = out_prefix.with_suffix(".html")

    print(f"input: {args.input}  ({len(questions)} questions)")
    print(f"jsonl: {out_jsonl}")
    print(f"html:  {out_html}")

    records = run(questions, out_jsonl)
    render_report(records, out_html)
    print(f"\nwrote {out_html} ({out_html.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
