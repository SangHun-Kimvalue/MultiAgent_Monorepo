# Requirements — ZRT v2

## Problem & Root Cause
- 표면 문제: MM Mechanical leg 부실 + 수동 파이프라인의 사람-메시지버스 + 구현측 교차리뷰 부재.
- **Root cause 1** (v1 실패): 판단을 코드에 넣음 → LLM 출력 의미 파싱 → 하네스 군비경쟁.
- **Root cause 2** (수동 프로세스 빈틈): Opus 상주 세션의 토큰 비용 → 구현 리뷰를 Codex 계열로만 구성 + Implementer가 자기 리뷰어 생성. 의도가 아니라 **비용 제약의 귀결** — 호출 단가를 낮추면(헤드리스 1회 + Sonnet) 제약 자체가 소멸.

## Goals
| # | 목표 | 대응 |
|---|---|---|
| G1 | `ztr review` — 무료 프리필터(ruff+mypy+ollama) + MM finding 포맷 + envelope JSON | 가치 1 |
| G2 | `ztr gate`(H4: 빈함수/칭찬-only) + `ztr verify --post-merge`(H6: ast+ruff+mypy) | 가치 1 |
| G3 | `ztr invariants` — MM 규약 자동 검사 (HANDOFF 갱신 여부, lessons append, finding 포맷, NOT CLAIMED 표기) | 가치 1, MM A3 |
| G4 | `ztr run-phase` — 페이즈 내 릴레이 (구현→Sonnet 리뷰→닛피커→수정 루프, verdict enum 라우팅, 휴먼 게이트 3곳, 타임박스 L1=3회) | 가치 2 |
| G5 | 바인딩 레지스트리 — `agents.config.yaml`을 MM 역할 키(role→model→call_type)로 진화. 구현 Reviewer 기본 Sonnet, `grade: L2`면 Opus | MM A1 |

## Non-Goals (명시적 제외)
- ❌ 판단 자동화 — ConsensusEngine류 의미 파싱 부활 금지. 코드가 파싱하는 것은 verdict enum + exit code뿐.
- ❌ 자동 머지 (ASTMerger) — 코드 편집은 Implementer 세션의 일.
- ❌ 대시보드 신규 개발 — 기존 자산 동결, 검증 이력 관측으로 축소 유지만.
- ❌ Supervisor / 재귀 위임 / gemini 워커.
- ❌ full E2E 자동 관통을 v2 PASS에 포함 (validation_plan 참조).

## Success Criteria
1. 수동 파이프라인에서 `ztr review`만 먼저 끼워 즉시 사용 가능 (릴레이 없이도 가치 성립).
2. 구현 리뷰가 Sonnet 헤드리스로 자동 삽입되어 Codex-only 검증 체인 해소 (Phase 5 후).
3. MM 인바리언트 위반(HANDOFF 미갱신 등)이 커밋 전 기계적으로 검출.
4. 페이즈당 사람의 수동 전달 행위가 휴먼 게이트 3곳으로 수렴.
