---
name: phase0-discovery-interview
description: >
  신규 프로젝트, 신규 기능, L2급 설계 변경, AI/자동화/제품/MVP/레거시 개선을 시작하기 전에
  구현으로 바로 들어가지 않고 Discovery gate를 수행한다. 문제정의, Root Cause, non-goal,
  기존 방식 대비 추가 가치(Baseline Delta), SSOT/소유권, adapter/runtime 경계, 핵심 evidence,
  검증 PASS 레벨, Open Items, role assignment를 정리하고 DISCOVERY_PASS/HOLD/REJECT 판정으로
  Planner 세션에 넘길지 결정할 때 사용한다.
---

# Phase 0 Discovery Interview

이 스킬은 구현 스킬이 아니다. 현재 세션은 **Discovery 세션**으로 동작하며, 프로젝트 착수 전 모호함과 실패 조건을 줄인다.

역할은 모델이 아니라 세션에 부여한다. 같은 모델을 쓰더라도 Discovery/Planner/Implementer/Reviewer 세션은 서로 다른 책임을 가진다.

## Canon

캐논의 단일 소스는 방법론 키트의 `METHODOLOGY.md`, `MULTI_AGENT.md`, `DOC_TAXONOMY.md`다. 이 스킬은 그 캐논을 실행하기 위한 Phase 0 adapter다.

불변식:
- Discovery 세션은 구현하지 않는다.
- L2급 신규 작업은 `DISCOVERY_PASS` 전 구현 프롬프트를 만들지 않는다.
- TBD를 그대로 두지 않는다. `Assumption`, `Decision`, `Evidence Required`로 재분류하되, 핵심 가치와 연결된 `Evidence Required`는 `DISCOVERY_HOLD` 조건이다.
- PASS는 `unit`, `deterministic`, `live`, `full E2E` 등 레벨로 말한다.
- Discovery와 phased handoff는 분리한다. Discovery 산출물이 Planner 세션의 입력이다.
- Senior Critique가 핵심 가치, PASS, adapter 계약을 뒤집으면 `requirements/design/validation/handoff`를 재정렬한 뒤 gate를 판정한다.

## Workflow

1. **Role Assignment**: Discovery/Planner/Implementer/Reviewer/Mechanical 세션을 지정한다.
2. **Profile Classification**: 작업을 웹, AI/LLM, 자동화, C++/장비, 레거시, 제품/MVP, 문서/평가, wrapper/adapter 중 1개 이상으로 분류한다.
3. **Baseline Delta Screening**: 기존 방식, 실제 실패 사례, 새 스킬/프로젝트가 추가하는 가치, 핵심 가치 claim을 먼저 고정한다.
4. **Minimum Interview**: 공통 질문 + profile 최소 질문만 먼저 묻는다. 자세한 질문은 `references/interview-guide.md`와 `references/profile-questions.md`를 사용한다.
5. **Open Items Ledger**: 모르는 항목을 `TBD`, `Assumption`, `Decision`, `Evidence Required`로 기록한다. 각 항목에 `Gate impact`를 붙이고, 핵심 가치/외부 adapter 계약과 연결되면 blocker로 표시한다.
6. **Discovery Brief First**: 전체 산출물 작성 전에 `discovery_brief.md` 1페이지를 만든다. Problem / Baseline failure / New value / Core blockers / Gate recommendation이 흔들리면 추가 질문을 먼저 한다.
7. **Draft Outputs**: brief가 안정된 뒤 `assets/templates/`를 기반으로 산출물을 만든다.
8. **Senior Critique**: silent fallback, PASS 레벨, SSOT, adapter 경계, evidence, 과거 lessons, baseline delta를 finding 형식으로 압박한다.
9. **Reconcile**: critique finding을 반영해 `requirements/design/validation/open_items/handoff`의 gate, 가치 claim, blocker, NOT CLAIMED가 서로 일치하는지 재검토한다.
10. **Gate Decision**:
   - `DISCOVERY_PASS`: Planner 세션으로 handoff 가능
   - `DISCOVERY_HOLD`: 추가 질문 또는 PoC 필요
   - `DISCOVERY_REJECT`: 문제정의/범위 재작성 필요

## Required Outputs

최소 산출물:
- `discovery_brief.md`
- `role_assignment.md`
- `requirements.md`
- `design.md`
- `validation_plan.md`
- `open_items.md`
- `handoff.md`

리스크가 크면 추가:
- `risk_register.md`

## Exit Report

Discovery 종료 보고는 아래 형식을 따른다.

```text
Gate: DISCOVERY_PASS | DISCOVERY_HOLD | DISCOVERY_REJECT
Profile:
Core Value:
Role Assignment:
Outputs:
Open Items:
Gate Blockers:
PASS Definition:
Top Risks:
Next:
```

`DISCOVERY_PASS`일 때만 Planner 세션이 phased handoff를 작성한다.
