# Validation Plan

## PASS Definition

| Claim | PASS level | Evidence | NOT CLAIMED |
|---|---|---|---|
| 멀티 앱 세션을 종합 표시한다 | **Live smoke** | 실제 Claude+Cursor 세션이 상황판에 동시 표출된 스크린샷/로그 | 모든 AI 앱 지원(ChatGPT 등)은 미주장 |
| 세션↔프로젝트 바인딩 정확 | Deterministic | 고정 픽스처(샘플 JSON/workspace.json)로 기대 매핑 일치 | 대화 내용 기반 추론 미주장 |
| 페이즈 계획+현재 페이즈 표시 | Deterministic | PHASE.md 픽스처 → 파싱 결과 일치 | PHASE.md 자동 생성 미주장 |
| 좀비 판정 | Deterministic | last_activity+프로세스 부재 조합 결정 테스트 | 크래시 원인 진단 미주장 |
| 알림 발행 | Live smoke | 토스트/웹훅 1회 발행 캡처 | 알림 전달 보장(SLA) 미주장 |
| 토큰 0원 | Static/Unit | 수집·판정·표시 경로에 LLM import/호출 0 (grep+테스트) | 온디맨드 브리핑(v2)은 토큰 사용함 |

## Validation Matrix

| Check | Command / method | Expected result | Artifact |
|---|---|---|---|
| Unit | `pytest tests/` 수집기 파서 단위 | 픽스처 파싱 정확 | pytest 로그 |
| Integration | 수집기→코어→SQLite 적재 | 정규화 레코드 DB 반영 | DB 덤프 |
| Deterministic | 고정 픽스처로 상태전이/조인/좀비판정 | 기대 상태 일치(시간 mock) | 테스트 리포트 |
| Live smoke | 실제 실행 중 Claude/Cursor 대상 1회 수집 | 상황판에 세션 표출+알림 | 스크린샷/JSONL |
| Full E2E | (v1 제외) 다앱·다프로젝트 장시간 | — | NOT CLAIMED for v1 |

## Evidence Required

| Item | Why needed | Owner | Due |
|---|---|---|---|
| Claude 세션 JSON 전체 필드 스키마 | 파서 안정성/스키마 드리프트 대비 | Implementer | P0 |
| Cursor `state.vscdb` 내부 테이블 구조(세션/활동) | 활동시각 신뢰 소스 확정 | Implementer | P1 |
| `PHASE.md` 표준 포맷 확정(frontmatter 등) | 결정적 파싱 가능 여부 | Planner+상훈 | P0 |
| 프로세스 부재 판정 신뢰성(앱 다중 프로세스) | 좀비 오탐 방지 | Implementer | P1 |
