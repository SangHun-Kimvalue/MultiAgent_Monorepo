# 문서 체계 (Single Source of Truth 구조)

방법론의 "어떤 문서를 두고, 무엇이 SSoT이며, 언제 갱신하나". **최소셋으로 시작**하고(C5/YAGNI),
프로젝트가 커지면 풀셋으로 확장한다. 작은 프로젝트에 풀셋을 강요하지 않는다.

## 1. 최소셋 (기본 — 모든 프로젝트)
| 문서 | 역할 | SSoT? | 수명/갱신 |
|---|---|---|---|
| `METHODOLOGY.md` (+ MULTI_AGENT/DOC_TAXONOMY) | 원칙·루프·역할 (provider 중립 캐논) | ✅ 방법론 SSoT | 반영구. 변경 시 어댑터 동기화 |
| 로드맵 (`*_ROADMAP.md` 또는 PHASES) | 페이즈 개요·상태 + **결정 로그** + 부착점 + DoD | ✅ 진행/결정 SSoT | 페이즈마다 갱신 |
| `HANDOFF.md` | 다음 작업자용 현재 상태 스냅샷(가정/미완/재현/PASS·NOT CLAIMED) | — | 매 작업 갱신, 최신 위로, 오래된 건 archive |
| `lessons/<module>.md` | 모듈별 교훈(WHY/LESSON) | — | 영구, append-only |
| 프로젝트 config (`.claude/...config.md`) | 빌드/RC/Nitpicker/컨벤션/전략 등 PARAM | ✅ 프로젝트 특화 SSoT | 환경 변경 시 |

→ 최소셋만으로 "원칙 + 진행/결정 + 인수인계 + 교훈 + 프로젝트값"이 모두 커버된다.

### 1.1 Phase 0 Discovery 산출물 (신규 L2 작업 시)
착수 전 Discovery gate(METHODOLOGY §2)를 거치면 아래가 추가로 생긴다. 이후 로드맵·구현의 입력이 된다.
| 문서 | 역할 | 수명 |
|---|---|---|
| `role_assignment.md` | Discovery/Planner/Implementer/Reviewer 역할을 맡는 세션 지정 | Discovery 시작 시 + 역할 변경 시 |
| `requirements.md` | 문제·목표·비목표·성공기준·Root Cause | Discovery 1회 + 변경 시 |
| `design.md` | SSOT·소유권·경계·주요 결정(대안·근거) | Discovery + 설계 변경 시 |
| `validation_plan.md` | PASS 정의(레벨)·재현 evidence | Discovery + 검증 변경 시 |
| `open_items.md` | TBD/Assumption/Decision/Evidence Required 대장 | gate 통과 시 TBD 0 |
| `handoff.md` | Planner 역할로 넘기는 현재 상태 | Discovery 종료 시 |
소규모(L0/L1)는 생략. Discovery 산출물은 phased handoff/로드맵의 입력 계약이다(중복 정의 아님 → 그대로 참조·확장).

## 2. 풀셋 (옵션 — 규모/리스크 큰 프로젝트)
실수요가 생길 때만 추가:
| 문서 | 추가 이유 |
|---|---|
| `DESIGN.md` | 아키텍처가 커져 로드맵 결정로그로 부족할 때(아키텍처 SSoT 분리) |
| `decisions/ADR-NNNN.md` | 되돌리기 어려운 큰 결정의 불변 기록 |
| `RISK_REGISTER.md` | 리스크가 많아 추적 테이블이 필요할 때 |
| `NEXT_ACTIONS.md` | 백로그가 길어 HANDOFF "다음 단계"로 부족할 때 |
| `NITPICKER.md` / review-rules | 기계 리뷰 규칙이 커질 때 |

## 3. 갱신 의무 (Sync-Out)
작업 종료 시(METHODOLOGY §2): **HANDOFF 갱신 + lessons append**가 의무. 설계가 바뀌면 로드맵 결정로그(또는 DESIGN/ADR) 동기화.
AI 에이전트는 응답에 재독·갱신 완료를 명시(MULTI_AGENT §3).

**다컴포넌트/스위트 진행 SoT (§4 적용 — 파편화·stale 방지):** 컴포넌트가 여럿이면(예: 모노레포 `methodology/` + `runtimes/`) "어디까지 됐나"의 **진행 SSoT를 1개 지정**한다. 현 스위트의 오케스트레이터 트랙 진행 SoT = **`methodology/docs/PHASE_CYCLE_ORCHESTRATOR_DESIGN.md §10`**. 규칙: ⓐ **페이즈 완료 시 그 SoT를 같은 작업에서 갱신**(✅ + 현재 위치), ⓑ 컴포넌트 로드맵(예: ztr `ROADMAP_V2.md`)은 *자기 내부*만 추적하고 cross-컴포넌트 진행은 SoT를 **포인터**로 가리킨다(같은 status를 두 곳에 적으면 drift→stale). ⓒ 진행 상태가 문서마다 다르면 SoT를 믿고 나머지를 같은 작업에서 정렬.

## 4. SSoT 충돌 규칙
- 문서와 코드가 다르면 **코드·테스트를 먼저 믿고** 문서를 같은 작업에서 갱신(stale 방치 금지).
- 같은 사실이 두 문서에 있으면 한쪽을 **SSoT로 지정**하고 다른 쪽은 포인터로(중복=drift).
- 캐논(METHODOLOGY)과 어댑터(SKILL/AGENTS.md) 사이도 동일: 캐논이 SSoT, 어댑터는 요약+출처.
