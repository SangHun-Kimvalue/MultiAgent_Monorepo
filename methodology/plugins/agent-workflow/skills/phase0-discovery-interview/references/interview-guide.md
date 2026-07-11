# Discovery Interview Guide

## Common Minimum Questions

Use these first. Do not start with the full deep set.

1. What real problem is this project or feature trying to solve?
2. What is the root cause of the current failure or inefficiency?
3. What is the current baseline workflow or tool, and where did it concretely fail?
4. What does the new project/skill add that the baseline does not? State the core value in one sentence.
5. What evidence would prove that core value is real rather than assumed?
6. What is explicitly out of scope?
7. What does success mean: feature completion, evidence, or production readiness?
8. What value, state, artifact, or decision needs a single owner?
9. Which validation level will count as PASS?
10. What can fail silently if not guarded?
11. What prior lesson or repeated failure pattern is relevant?

## Open Items

Every unresolved item must be categorized.

| Type | Meaning | Close condition |
|---|---|---|
| `TBD` | Temporary unresolved item | Must become one of the types below before gate decision |
| `Assumption` | Proceed with stated assumption | Include basis and impact if wrong |
| `Decision` | Decided with rationale | Record alternatives and reason |
| `Evidence Required` | Needs measurement, PoC, or source verification | Add to validation plan |

Add `Gate impact` to every item:
- `Blocker`: blocks `DISCOVERY_PASS`.
- `Non-blocking`: may proceed with documented assumption/decision.
- `Watch`: not blocking now, but must be checked by Planner/Implementer.

## Senior Critique Questions

Ask these before gate decision.

1. Where can the design silently fallback?
2. Is PASS unit, deterministic, live smoke, full E2E, or production ready?
3. Who owns the real source of truth?
4. Is the adapter a real replacement boundary or just a wrapper name?
5. What evidence will allow failure diagnosis later?
6. Which prior lesson prevents repeating the same mistake?
7. Did the core value change after critique? If yes, were requirements and PASS rewritten?

## Reconcile Checklist

Run this after Senior Critique and before gate decision.

- Does `requirements.md` state the same core value as `handoff.md`?
- Does `validation_plan.md` require evidence for the core value?
- Do all open items have `Gate impact`?
- Are core-value `Evidence Required` items still open? If yes, gate is `DISCOVERY_HOLD`.
- Are external adapter/tool contracts verified when they carry core functionality? If no, gate is `DISCOVERY_HOLD`.
- Are P1 critique findings unresolved? If yes, gate is `DISCOVERY_HOLD` or `DISCOVERY_REJECT`.
- If the value proposition was downgraded, did the user explicitly accept that downgrade?

## Gate Criteria

`DISCOVERY_PASS` requires:
- `TBD=0`
- no open `Evidence Required` item tied to the core value proposition
- no unverified external adapter/tool contract that carries core functionality
- no unresolved P1 Senior Critique finding
- PASS level is explicit
- SSOT/ownership/boundary is stated
- non-goal is stated
- baseline delta and new core value are stated
- critique questions are answered
- requirements, validation plan, open items, and handoff agree on gate status and blockers
- handoff is ready for Planner

Return `DISCOVERY_HOLD` when evidence, PoC, source verification, or adapter contract confirmation is required before implementation.
Return `DISCOVERY_REJECT` when the problem, value proposition, or scope is invalid or must be rewritten.
