---
name: cross-session-plan-review
description: >-
  Critically peer-review a next-phase plan, implementer prompt, or current-state
  briefing written by another session or author before it is acted on. Trigger
  when the user asks to 검토, 교차검증, 보완사항 체크, review this plan,
  cross-validate, sanity-check before handoff, or asks whether a prompt is safe
  to give an implementer. Verify factual claims such as working tree state,
  commits, done/not-started status, cited reports, numbers, and proposed new
  artifacts against the actual repo, then return severity-ranked critique and
  paste-ready fixes. This reviews an existing artifact only; do not author a new
  phased plan, run discovery, review source code/PR bugs, perform security
  review, refactor, or implement the work.
---

# Cross-Session Plan Review

A second pair of eyes on a plan that another session already wrote. You did not
author it; your job is to find where it is wrong, stale, over/under-engineered,
or built on unverified assumptions — **before** it becomes code.

The single highest-value thing this skill does is refuse to take the artifact's
claims at face value. Plans confidently assert "working tree clean", "X is the
next phase", "the report shows model Y passed", "let's create module Z". In
practice a large fraction of those claims are wrong or stale, and the wrongness
is invisible unless you open the repo and look. Catching that is worth more than
any stylistic critique.

## When this fits (and when it does not)

Use this when the user hands you an artifact from **somewhere else** and wants a
verdict on it:
- a next-phase implementer prompt drafted by another session
- a "current state + recommended next step" briefing
- a design decision or roadmap someone wants gut-checked before committing to it

Boundary against sibling skills — be strict, because mis-triggering wastes a
whole session:
- **phase0-discovery-interview** runs discovery on a *new* problem. If there is
  no artifact to review yet, that is its job, not this one.
- **phased-implementation-handoff** *authors* the plan and implementer prompt.
  If the user wants you to write the next plan, use that. This skill reviews a
  plan that already exists.
- **Implementer work** writes the code. This skill never implements; it only
  critiques the artifact. If review passes and the user then says "now do it",
  that is a separate task.

If the request is "write the next phase" or "build this", you are in the wrong
skill — say so and point at the right one.

## Operating principle: verify before you judge

Read the artifact once to understand intent. Then, before writing a single line
of critique, **verify its factual claims against the actual repository.** A
critique built on the artifact's own (possibly false) premises is worse than
useless — it launders the error forward with your authority behind it.

The five verification moves below are not a checklist to mechanically tick. They
are the recurring places artifacts lie to you. Derive the *specific* checks each
artifact needs — the commands differ every time, but the suspicion is constant.

### 1. Never trust meta-claims about state

Any claim about repository or process state is a claim to verify, not accept:
"working tree is clean", "latest commit is X", "this is uncommitted", "phase N
is done", "phase N+1 hasn't started", "tests pass".

Verify with the actual tools — `git status --short`, `git log --oneline -N`,
`git show --stat <sha>`, `git diff --stat`, `Glob` for claimed files. A mismatch
is not a footnote; it is usually a **P1 finding**, because the entire plan is
predicated on a state that does not exist. The classic failure: an artifact says
"next, implement feature F" while F is already sitting implemented in the working
tree — handing that prompt to a fresh implementer produces duplicate, conflicting
work.

### 2. Triangulate cited evidence

When the artifact cites numbers, files, reports, or test results to justify a
decision ("the calibration report shows schema_valid 0.10", "model Y passed",
"the benchmark says"), open the cited source and confirm the conclusion actually
follows from the data. Read the report, not just its summary line. Watch for:
the artifact's stated conclusion contradicting its own cited data; a number
quoted from a file that says something different; a decision attributed to a
report whose recommendation was actually the opposite. Each of these is at least
P2, often P1.

### 3. Reconnaissance before invention

Every "let's create / add / build X" in the artifact deserves the question "does
X already exist?" — answered by `Grep`/`Glob`, not by assumption. The most common
and most expensive failure in multi-session work is **duplicate construction**:
a second module, a second alias table, a second benchmark harness, a second
source of truth, built because the planning session didn't know the first one
existed. Dual-SSOT drift follows immediately. Before endorsing any new artifact
the plan proposes, confirm the repo doesn't already have it (possibly under a
different name) and that inline/ad-hoc versions aren't being left behind to rot.

### 4. Surface planner-only decisions

Some choices must be locked by the planner *before* handoff, because an
implementer forced to decide mid-task will pick something arbitrary and create
drift: new enum values, schema changes, normalization/canonicalization policy,
thresholds, naming that implies semantics, scope boundaries. Scan the artifact
for decisions it left implicit or hand-waved ("ask_user or manual_review
boundary", "or current installed model"). Pull each one out and present it as a
decision the user should make and bake into the prompt now — ideally with your
recommended default and the reason.

### 5. Scope discipline: over- and under-engineering both

Plans drift in two directions. **Over**: new top-level modules for a single
observed case, frameworks/DBs/queues unprompted, fields that carry no
information (e.g. a "confidence_policy" for a deterministic exact-match), CSV +
JSON when one suffices. **Under**: the largest failure bucket in the cited data
left untouched; missing ground-truth labels or thresholds so the work "measures
but can't decide"; no repeat count on a noisy measurement; missing normalization
that makes a stated test case impossible to pass. Judge against the evidence and
a YAGNI bar, and name both directions explicitly.

## 독립 에이전트 에스컬레이션 (조건부 — MULTI_AGENT.md §1.2)

이 스킬은 **설계 검토 leg**다 — 설계 저자와 다른 세션이므로 **기본은 직접 검토**한다(서브에이전트 매번 호출은 과하다). 다음일 때만 추가 독립 에이전트를 호출한다: ① 대형·복잡 diff ② R5/보안 등 크리티컬 경계 ③ **심층 코드 검증**이 필요(이 스킬은 플랜 검토지 코드 리뷰가 아님 — 코드 레벨 검증은 위임) ④ 1차 판정 애매.

호출 기본 = **Codex CLI(`codex exec`)** — cross-vendor 독립이 같은 벤더 서브에이전트보다 맹점이 덜 겹친다. 원칙: **reviewer 벤더 ≠ author 벤더**(Codex가 저작했으면 Claude가, Claude가 저작했으면 Codex가). 구현 코드 리뷰(구현 리뷰 leg)는 구현 세션과 독립한 에이전트가 집행한다(C4) — 이 스킬이 직접 코드 리뷰를 떠안지 않는다.

## Output format

Match the language of the user's input (the demonstrated sessions were Korean;
the labels below are shown that way and adapt). Lead with the verification you
actually ran, because that is what makes the rest trustworthy. Every finding
cites a concrete source — `file:line`, a commit sha, or a command output. The
phrase "looks good" with nothing behind it is banned; if something is genuinely
fine, say *why* it is fine and what you checked.

Use this structure:

1. **검증 결과 / Verification** — what you ran and what it showed. Lead with any
   state-claim that turned out false. This section is not optional and not last.
2. **결론 요약 / Verdict** — is the artifact's direction sound? One honest
   paragraph. If the strategy is right but the prompt is unsafe to hand off
   as-is, say exactly that.
3. **P1 / P2 / P3 findings** — severity-ranked. P1 = breaks the plan or is
   factually false (wrong state, contradicted evidence, duplicate construction).
   P2 = materially weakens reliability/correctness. P3 = polish. Each finding
   names its source and gives the fix.
4. **Over- vs under-engineering table** — a compact table judging the proposed
   scope in both directions, so the user sees what to cut and what to add.
5. **Paste-ready patches** — the actual text to insert into or replace in the
   artifact, fenced and ready to copy. Don't describe the fix abstractly; write
   the lines. This is what makes the review actionable in one pass.
6. **Pre-decisions to lock** — the planner-only decisions from principle 4, each
   phrased as a concrete choice for the user with a recommended default.
7. **One-line summary** — the single most important thing, stated plainly.

## Disposition

Be critical on purpose. The user invoked a *review*; agreeing pleasantly is a
failure mode, not politeness. The demonstrated value came from finding the thing
the planning session missed — the 6 inline tuples it counted as 3, the report
whose own §8 already recommended the opposite, the enum that already existed.
That only happens if you actually look and are willing to contradict a confident
artifact. At the same time, when a later artifact has clearly absorbed prior
feedback, say so specifically — "this is the most mature prompt in the cycle"
is a real finding when it's true and earned by comparison.

Do not edit the repository and do not commit. The deliverable is the review;
acting on it is the user's call, and usually a separate session.

## Closing step: emit the corrected handoff prompt

A review is most useful paired with the fixed artifact. After delivering the
severity-ranked review, produce the ready-to-hand-off implementer prompt for the
same phase in the same turn — P1/P2/P3 fixes folded in, pre-decisions resolved to
recommended defaults (flag any the user must still choose). This stays within
review scope: you emit a *prompt*, never code, and still do not edit the repo or
commit. Keep the project's established phase format (역할 / 상태 검증 / SoT / 범위 /
Out of scope / 테스트 / 검증 / 리뷰 루프 / 완료 보고) and "커밋하지 말고 워킹트리에
남긴다" unless told otherwise.

When the user asks in Korean for "구현세션에 넘길 프롬프트", "구현 프롬프트 브리핑", or "다음 세션에 붙여넣을 내용", treat that as an explicit request for this closing step and include a paste-ready Implementer briefing at the end of the review.
