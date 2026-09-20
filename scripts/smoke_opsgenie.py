"""Smoke test: OpsGenie integration in investigate.py.

Tests against today's real SEV-1 incident
(a6022b52-548a-45da-bb9a-06d2a4030847 — "Users unable to raise dispute complaints").

Run on the box with the env sourced:
    source ~/.config/jarvis/env && cd ~/jarvis/scripts && \
        ./indexer/.venv/bin/python smoke_opsgenie.py
"""
from __future__ import annotations
import json
import sys

from agent import opsgenie_client
from agent.investigate import build_investigation_prompt


SEV1_URL = ("https://jupitermoney.app.opsgenie.com/incident/detail/"
            "a6022b52-548a-45da-bb9a-06d2a4030847")
SEV1_BARE_ID = "a6022b52-548a-45da-bb9a-06d2a4030847"
NONSENSE = "CMS New Card success rate less than 90 percent"


def step(label: str) -> None:
    print(f"\n{'=' * 6} {label} {'=' * 6}")


def main() -> int:
    failed = 0

    step("1. detect_ref(URL) — should return ('incident', uuid)")
    r = opsgenie_client.detect_ref(SEV1_URL)
    print(f"   {r}")
    if r != ("incident", SEV1_BARE_ID):
        failed += 1; print("   ✗ FAIL")

    step("2. detect_ref(bare-UUID) — should return ('alert', uuid) [ambiguous, alert-first]")
    r = opsgenie_client.detect_ref(SEV1_BARE_ID)
    print(f"   {r}")
    if r != ("alert", SEV1_BARE_ID):
        failed += 1; print("   ✗ FAIL")

    step("3. detect_ref(free text with no IDs) — should return None")
    r = opsgenie_client.detect_ref(NONSENSE)
    print(f"   {r}")
    if r is not None:
        failed += 1; print("   ✗ FAIL")

    step("4. fetch_context(URL) — should fetch incident, fall back to alert if needed")
    try:
        ctx = opsgenie_client.fetch_context(SEV1_URL)
        if ctx and not ctx.get("error"):
            print(f"   kind={ctx['kind']}  id={ctx['id'][:12]}...")
            print(f"   message: {(ctx.get('message') or '')[:120]}")
            print(f"   status={ctx.get('status')}  priority={ctx.get('priority')}")
            print(f"   responders={ctx.get('responders')}")
            print(f"   notes={len(ctx.get('notes') or [])}")
        else:
            print(f"   ✗ FAIL: {ctx}")
            failed += 1
    except Exception as e:
        print(f"   ✗ FAIL with exception: {e}")
        failed += 1

    step("5. build_investigation_prompt(URL) — should include the OpsGenie context block")
    prompt = build_investigation_prompt(SEV1_URL)
    has_og = "OpsGenie" in prompt and SEV1_BARE_ID[:8] in prompt
    has_structured_q = "structured analysis" in prompt
    print(f"   prompt length: {len(prompt)} chars")
    print(f"   has OpsGenie block: {has_og}")
    print(f"   has structured-analysis ask: {has_structured_q}")
    if not (has_og and has_structured_q):
        failed += 1; print("   ✗ FAIL")

    step("6. build_investigation_prompt(plain text) — should NOT call OpsGenie")
    prompt = build_investigation_prompt(NONSENSE)
    has_og = "OpsGenie" in prompt
    print(f"   prompt length: {len(prompt)} chars")
    print(f"   has OpsGenie block (should be False): {has_og}")
    if has_og:
        failed += 1; print("   ✗ FAIL")

    print(f"\n{'=' * 6} RESULT {'=' * 6}")
    print(f"   {6 - failed}/6 passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
