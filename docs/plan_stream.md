# Merlin plan-stream contract

`POST /api/v1/plan/stream` is Jarvis's standalone, read-only implementation
planning surface. It is not a prompt variant of `/api/v1/ask/stream`.

Merlin owns chat persistence and supplies a bounded snapshot of the active
session. Jarvis owns repository discovery, code investigation, planning policy,
scope evidence, and the returned plan artifact.

## Request

```json
{
  "sessionId": "2ba4…",
  "intent": "create",
  "sessionSummary": "User needs an LMS repayment settlement breakdown API.",
  "recentTurns": [
    {"role": "user", "content": "We need the TFIP and EMI/SPEND view."},
    {"role": "assistant", "content": "I can create an implementation plan."}
  ],
  "currentPrompt": "Plan the repayment breakdown API.",
  "maxCostUsd": 5.0
}
```

`repo` is intentionally absent. Jarvis begins unscoped, discovers the owning
repository from retrieval evidence, and emits that decision before it plans.
`maxCostUsd` is optional and defaults to `$5.00`; it may be lowered per plan but
never raised above `$5.00`.

For feedback on an active plan:

```json
{
  "sessionId": "2ba4…",
  "intent": "refine",
  "sessionSummary": "…",
  "currentPrompt": "Include the per-bucket split in the response.",
  "feedback": "The previous plan exposed only aggregate TFIP totals.",
  "previousPlan": {"version": 1, "summary": "…", "files": [], "scope": {}}
}
```

Use `create` for the first plan, `refine` to change an existing plan, and
`replan` when the user wants a fresh plan based on a prior artifact. Context is
intent only: Jarvis must revalidate code facts through tools before relying on
them.

## SSE lifecycle

```text
phase             understanding_context | resolving_scope | discovering |
                  mapping_requirements | collecting_evidence | investigating |
                  synthesizing | criticizing | repairing
tool_called       existing Jarvis retrieval/tool call
tool_result       result preview for the corresponding call
scope_resolved    primaryRepo, relatedRepos, confidence, reason, evidence
plan_ready        complete structured plan artifact
done              token usage, actual estimated cost, elapsed time, final scope
error             terminal failure; no plan artifact was produced
```

`plan_ready` is the source of truth. Merlin should render it directly rather
than collecting model text and regex-parsing JSON. The client may use `phase`
and tool events for live progress, but should never expose model chain-of-thought.

## Planning state machine

```text
scope preflight → requirements map → parallel evidence collection
                                              │
                                              ▼
                          bounded adaptive gap-fill (≤12 model turns)
                                              │
                                              ▼
                           synthesis → source-citing critic → one repair
                                              │
                                              ▼
                                      plan_ready / done
```

Scope preflight is deterministic retrieval, not a model decision, so it cannot
consume 90 seconds on a ceremonial `set_plan_scope` call. Evidence collection
uses five independent lanes (contract, entry point, domain, persistence/product
branches, and build/tests) and reads up to 20 selected files concurrently.
During adaptive gap-fill the model can request more read-only tools and changes
scope only with evidence. A premature incomplete artifact returns it to
gap-fill; only an artifact produced at synthesis receives the one constrained
repair attempt. This keeps the five-minute envelope focused on investigation
rather than repeated JSON retries.

## Plan artifact

```json
{
  "version": 2,
  "planVersion": 2,
  "intent": "refine",
  "summary": "…",
  "files": [
    {
      "repo": "lms",
      "path": "services/lms/api/spec.yml",
      "operation": "MODIFY",
      "description": "Verified change details…",
      "evidence": [{"repo": "lms", "path": "services/lms/api/spec.yml"}]
    }
  ],
  "coverage": [
    {
      "requirement": "Repayment API contract",
      "status": "verified",
      "conclusion": "The contract follows the existing API generation pattern.",
      "evidence": [{"repo": "lms", "path": "services/lms/api/spec.yml"}]
    }
  ],
  "contract": {
    "method": "GET",
    "path": "/lending/v1/customer/credit/repayment/breakdown",
    "parameters": [
      {"name": "userId", "source": "user", "evidence": []}
    ]
  },
  "execution": {
    "status": "ready",
    "blockers": [],
    "targetPaths": ["services/lms/api/spec.yml"],
    "steps": [
      {
        "id": "S01",
        "path": "services/lms/api/spec.yml",
        "operation": "MODIFY",
        "anchors": ["existing repayment API paths"],
        "change": "…",
        "dependsOn": [],
        "acceptance": ["Generated API contract exposes the planned operation."],
        "evidence": [{"repo": "lms", "path": "services/lms/api/spec.yml"}]
      }
    ],
    "invariants": [],
    "verification": ["…"],
    "guardrails": ["Modify only the target paths in the execution handoff."]
  },
  "buildSteps": ["…"],
  "risks": ["…"],
  "missingInfo": ["…"],
  "changeSummary": ["Added bucket-level allocation to plan v1."],
  "scope": {
    "primaryRepo": "lms",
    "relatedRepos": [],
    "confidence": "high",
    "reason": "…",
    "evidence": ["lms/services/lms/api/spec.yml"]
  }
}
```

The planner resolves scope through retrieval, performs a five-lane scoped
research fan-out, and reads up to 20 high-signal files in parallel before
adaptive investigation. Every final file and verified coverage statement must
cite a read source. A final in-scope file omitted from the model's reads is
mechanically verified; missing or out-of-scope paths still fail.

New tests must cite an existing test in the same build module, so a plan cannot
invent a test framework or fixture style from another module. A separate
source-citing critic pass checks every final artifact for unsupported claims,
uncovered requirements, contradictory risks, and missing validation or
transformation semantics before it is returned.

## Coding-agent handoff

`summary`, `coverage`, `scope`, and `contract` are planning and audit data:
they let Merlin present the decision, explain what was proven, and route the
work to the owning repository. `execution` is the agent-agnostic coding
contract. It contains ordered target-file steps, anchors, dependencies,
acceptance criteria, invariants, verification intent, and guardrails.

Merlin is the integration boundary: it can send this structured object directly
to a coding agent that supports it, or render the same fields into another
agent's request format. It must not dispatch an execution when
`plan.execution.status` is `blocked`; surface `blockers` to the user instead.
Jarvis deliberately does not know any coding-agent API, Git URL, branch, or
provider-specific execution policy.

## Budget

Every plan has a hard `$5.00` ceiling. Before each model call, Jarvis makes a
conservative reservation for the complete serialized prompt, output, and a
final synthesis turn; it also stops if the API-reported usage reaches the
approved budget. Large tool results are bounded before being returned to the
model so one broad read cannot consume the plan's entire context budget. For
portal-authenticated callers, the approved plan budget must also fit within the
user's remaining daily budget.

`JARVIS_PLAN_MAX_ITERATIONS` is an absolute ceiling of 24. The default state
machine uses at most 12 adaptive gap-filling turns, then synthesis, one compact
source-citing critic, and at most one validator-directed repair; lower
`JARVIS_PLAN_MAX_ITERATIONS` also lowers that envelope. The cost gate, not
iteration count, remains the hard `$5.00` enforcement point.
