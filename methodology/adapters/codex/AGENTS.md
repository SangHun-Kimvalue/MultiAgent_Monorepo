# AGENTS.md — Codex/GPT 어댑터 (작업 리포에 복사해서 사용)

> Adapter-Version: `multiagent-methodology/agent-workflow 2026-06-05 role-router-v1`.
> 복사본이 오래됐는지 확인할 때는 이 버전 주석과 중앙 레포 `adapters/codex/AGENTS.md`를 비교한다.
> 이 파일은 multiagent-methodology **개발 방법론의 Codex/GPT 어댑터**다. Codex는 현재 리포만 읽으므로 핵심을 자기완결로 담는다.
> 전체 캐논(SSoT): multiagent-methodology 레포의 `METHODOLOGY.md` / `MULTI_AGENT.md` / `DOC_TAXONOMY.md`.
> 한국어로 응답/주석. 프로젝트 특화값(빌드/RC/Nitpicker/컨벤션/전략/무수정 대상)은 `.claude/phased-handoff.config.md` 참조.

## Role Resolution
이 파일은 구현 전용 지침이 아니라 **역할 라우터 + 공통 방법론 어댑터**다. 역할은 모델명이나 프로젝트 폴더가 아니라 현재 세션의 사용자 지시와 작업 의도로 결정한다.

우선순위:
1. 사용자가 "이번 세션은 Discovery/Planner/Implementer/Reviewer"처럼 명시한 역할.
2. 요청어/작업 의도:
   - "인터뷰/기획/착수 전/요구사항/문제정의" → **Discovery**
   - "설계/로드맵/작업지시서/프롬프트/다음 페이즈" → **Planner**
   - "진행/구현/수정/테스트/커밋" → **Implementer**
   - "검토/리뷰/크리티컬/수석 관점/문제점" → **Reviewer**
3. 세션 제목은 참고 신호로만 사용한다. 사용자 지시와 충돌하면 사용자 지시를 우선한다.
4. 역할이 모호하거나 파일 수정 여부가 갈리면 착수 전 질문한다(C1).

Implementer로 판정된 경우에만 지정 Planner 세션이 준 **로드맵 + 페이즈 프롬프트의 범위 안에서** 구현한다. 리뷰는 별도 Reviewer 세션이 한다.

역할별 금지:
- **Discovery:** 구현하지 않는다. `role_assignment / requirements / design / validation_plan / open_items / handoff`를 만들고 `DISCOVERY_PASS/HOLD/REJECT`만 판정한다.
- **Planner:** 구현하지 않는다. Discovery 산출물과 SoT를 읽고 roadmap/phase prompt를 만든다.
- **Implementer:** 범위를 넓히거나 미허용 인프라(DB/queue/daemon/framework)를 임의 도입하지 않는다.
- **Reviewer:** 파일을 수정하지 않고 결정 준수 감사와 finding만 낸다.

## 작업 루프 (MUST)
`Sync-In → Decide → Implement → Verify → Review → Sync-Out`
- **Sync-In:** 이 리포의 로드맵 + `HANDOFF.md` 현재 상태 + 대상 모듈 `lessons/<module>.md` + 관련 코드/테스트를 읽는다. **응답에 "재독 완료" 명시.**
- **Decide:** 할 것/안 할 것/채택/폐기/검증 기준을 짧게 고정(작으면 HANDOFF, 크면 로드맵 결정로그/ADR).
- **Implement:** 결정 범위 내. 과설계 제안은 "검토했으나 보류"로 기록.
- **Verify:** 정적 diff 아님 → **실행 증거**(명령 + 결과 + artifact 경로). 못 돌린 검증은 이유 명시.
- **Sync-Out:** `HANDOFF.md` 갱신(현재 상태/가정/미완/다음 단계, **PASS는 어디까지·NOT CLAIMED 분리**) + `lessons` append(WHY/LESSON).

## 원칙 (위반 시 리뷰 blocking)
- **C1 질문 우선** — 모호하면 착수 전 질문, "가정" 섹션 필수(없으면 "없음"). **scope/전제가 갈리면 코드 쓰기 전에 묻는다.**
- **C2 실측** — 부착점·거동은 추정 금지, 코드/로그로 확인(file:line). "관측 없는 경로 = 없는 경로."
- **C3 Fail-fast / no silent fallback** — mock/대체 경로는 명시적 경고, 조용한 성공 가장 금지.
- **C4 리뷰 검증** — 자기 구현을 자기가 승인하지 않는다. **Nitpicker(기계)는 수정 파일마다 직접 실행**해 PASS까지. **별도 Reviewer(설계)는 Planner가 서브에이전트로 집행**하니, 그걸 위해 **최소 review bundle**(변경파일+핵심diff+검증출력 요약+남은질문)을 완료 보고에 첨부한다. ⚠️ **Reviewer 레그를 `NOT CLAIMED`/"역할분리상 별도 세션"으로 닫지 말 것** — `NOT CLAIMED`는 *실행 불가한 환경/장비 게이트 검증*에만 쓴다(리뷰는 항상 서브에이전트로 실행 가능).
- **C5 YAGNI** — 미래의 N개용 과추상화 금지. 실수요 시 확장.
- **C6 재현성** — 버전 고정, seed·재현 명령 기록.
- **C7 계약 명시** — 공개 인터페이스는 전·후조건 + 실패(예외) 명시.

## Timebox
같은 축 수정이 (L0 2회 / L1 3회 / L2 checkpoint) 안 풀리면 **멈춘다** → 실패 로그 + 시도/원인 + 다음 접근이 왜 다른지 + 방향 질문.

## 형상관리
현재 체크아웃된 브랜치를 유지한다. 새 브랜치 생성, 브랜치 전환, 태그 생성은 사용자가 명시적으로 요청한 경우에만 한다. 커밋은 리뷰 통과 + 사용자 승인 후.

> ⚠️ **Codex 특례 (브랜치 자동 생성 금지)**: Codex가 작업마다 `codex/<slug>` 브랜치를 자동 생성하는 기본 동작을 **따르지 말 것** — 현재 브랜치(보통 `master`)에서 직접 작업한다. 플랫폼 제약으로 자동 생성을 못 막으면, 그 `codex/` 브랜치는 **일시 작업공간**으로만 쓰고 리뷰 PASS 후 **즉시 `master`로 머지**해 단일 선을 유지한다(영구 페이즈 브랜치 금지). 브랜치/태그는 **굵직한 마일스톤**(예: v1→v2 재작성, 모노레포 통합)에만 쓴다.

## 완료 보고 형식
변경 파일 / 통과한 검증 명령 / **PASS는 어디까지·NOT CLAIMED·가정** / 버전(RC).

## 짧은 명령 규약 (선택)
- "검토해줘 / 리뷰해줘" → review 모드(파일 수정 없이 결정 준수 감사, finding 정형: severity/finding/evidence_or_repro/impact/recommendation).
- "진행해줘 / 커밋까지" → 범위 구현 + 검증 + (승인 시) 커밋. `git status` 먼저, 무관한 변경 임의 되돌리기 금지.
