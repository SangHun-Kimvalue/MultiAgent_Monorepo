# Handoff — ZRT v2 Discovery → Planner

> 2026-06-13 · Discovery 세션 → Planner 세션 인계 스냅샷

## Gate
**DISCOVERY_PASS** (조건부 스코프: Phase 1~4 무조건 / Phase 5는 spike S1 통과 후 진입)

## 현재 상태
- Discovery 산출 7+1종 완료: `docs/discovery/ztr-v2/` (brief, role_assignment, requirements, design, validation_plan, open_items, risk_register, 본 문서)
- 환경 실측 완료: ollama 0.18.0 ✅ / ruff 0.3.4 ✅ / Python 3.12.10 ✅ / **claude·codex CLI 부재** ⚠️(E-1) / mypy·venv 부재(Phase 1 구성)
- 상위 결정 문서: `docs/ZRT_V2_DIAGNOSIS.md`(KEEP/DROP·차용표), `D:\MultiAgent_Methodology\docs\PROCESS_CONCLUSION.md`(전체 구도)

## Planner 입력 — 페이즈 제안 (확정은 Planner 몫)

| Phase | 등급 | 내용 | 선행조건 / DoD 골자 |
|---|---|---|---|
| **1. 환경+골격** | L1 | venv+mypy 구성, v2 브랜치, DROP 코드 명시 삭제, envelope 스키마, 역할 키 바인딩 config, **DESIGN.md v3.0 갱신** | DoD: deterministic 테스트 통과 + DESIGN.md가 v2 구도 반영 |
| **2. ztr review** | L1 | ruff+mypy+ollama 프리필터 통합, finding 포맷(M2), NOT CLAIMED(M3) | DoD: live 1회(ollama) + 수동 파이프라인에 즉시 투입 가능 |
| **3. ztr gate + verify** | L1 | v1 H4(quality_gate)·H6(post_merge_verifier) 이식 | DoD: unit+deterministic. 이 페이즈부터 자기 검증(dogfood) 전환 |
| **4. ztr invariants** | L1~L2 | MM 인바리언트 목록 정의(**MM 캐논과 협의 — MM A3**) + 파일시스템 사실 검사 구현 | DoD: HANDOFF 미갱신·finding 포맷 위반 검출 시연 |
| **5. ztr run-phase** | L2 | 릴레이 — 헤드리스 구동, verdict enum 라우팅, 휴먼 게이트 3곳, Toast 알림, 타임박스(L1=3회 STOP) | **진입 게이트: spike S1** (CLI 설치+플래그 실측). DoD: deterministic 루프 + live 1회(Sonnet 리뷰) |
| **6. dogfood** | — | 실프로젝트 1페이즈 적용, LESSON-NNN 회수, full E2E 검증(여기서 비로소 claim) | DoD: LESSON 3건 이상 + MM E1 사이클 보고 |

## 인계 주의사항
- Planner는 `phased-implementation-handoff` 스킬로 로드맵+페이즈 프롬프트 작성. **페이즈 2개 이상 묶지 말 것.**
- 구현 프롬프트에 design.md **Boundaries 표**를 불변식으로 포함 — "verdict enum+exit code 외 파싱 추가 금지"(R5).
- 리뷰 leg: 설계=Codex 세션(기존), 구현=Sonnet 헤드리스(릴레이 전까지 수동 트리거).
- spike S1은 Phase 1~4와 병행 가능 (사용자가 CLI 설치 후 5분 실측).

## NOT CLAIMED
- full E2E 자동 관통, Sonnet 리뷰 탐지 품질, ollama 린트 품질 — validation_plan.md 참조.
