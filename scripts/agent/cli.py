"""Jarvis CLI. Usage: python -m agent.cli "your question here" [--verbose]"""
from __future__ import annotations
import argparse
import sys
from .agent import ask


def main() -> int:
    ap = argparse.ArgumentParser(prog="jarvis", description="Ask Jarvis about Jupiter's code.")
    ap.add_argument("question", nargs="+", help="Your question.")
    ap.add_argument("-v", "--verbose", action="store_true", help="Print tool calls to stderr.")
    ap.add_argument("--stats", action="store_true", help="Print usage stats at the end.")
    args = ap.parse_args()

    q = " ".join(args.question)
    res = ask(q, verbose=args.verbose)

    print(res.answer)

    if args.stats or args.verbose:
        # Cost estimate: Sonnet 4.6 list pricing $3/Mtok in, $15/Mtok out, cache reads $0.30/Mtok.
        new_in = max(0, res.input_tokens - res.cache_read_tokens)
        cost = (new_in * 3 + res.cache_read_tokens * 0.30 + res.cache_creation_tokens * 3.75
                + res.output_tokens * 15) / 1_000_000
        print(
            f"\n--- stats ---",
            f"iterations: {res.iterations}",
            f"tool_calls: {len(res.tool_calls)}",
            f"input_tokens (incl cache): {res.input_tokens}",
            f"  cache_read: {res.cache_read_tokens}",
            f"  cache_creation: {res.cache_creation_tokens}",
            f"output_tokens: {res.output_tokens}",
            f"elapsed: {res.elapsed_sec}s",
            f"est cost: ${cost:.4f}",
            sep="\n",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
