# Design — ZRT v2

## SSoT & Ownership
- **영속 스펙 SSoT**: `docs/DESIGN.md` — Phase 1에서 v2.9 → **v3.0 갱신** (v2 구도 반영). 본 discovery 문서는 입력이며 갱신 후 포인터화.
- 재포지셔닝 근거 SSoT: `docs/ZRT_V2_DIAGNOSIS.md` (KEEP/DROP 분류, MM/MS 차용표).
- 소유권: `ztr` 패키지 (같은 repo, v2 브랜치 점진 리팩토링 — 인터뷰 확정).

## Boundaries (v1 함정 차단선)
| 경계 | 코드가 한다 | 코드가 안 한다 |
|---|---|---|
| 출력 해석 | verdict enum(`PASS\|CHANGES_REQUESTED\|BLOCKED`) + exit code 파싱 | 리뷰 *내용* 해석 (읽는 주체 = 다음 세션의 LLM) |
| 진행 | 고정 악보 sequencing, 게이트 검사, 알림, 타임박스 강제 | 설계 판단, 수락/거부 결정, 다음 페이즈 결정 |
| 파일 | envelope·result 캡처, 인바리언트 사실 확인 | 소스 코드 편집·머지 |

## 주요 결정 (대안 포함)
| # | 결정 | 대안 (기각 사유) |
|---|---|---|
| D1 | 같은 repo 점진 리팩토링 (v2 브랜치) | 새 패키지 재시작 (테스트 자산 227건 재작성 비용) |
| D2 | 구현 Reviewer = **Sonnet 헤드리스 기본, L2만 Opus** | 전부 Opus (토큰 제약 — 이 제약이 v2의 존재 이유), 전부 Sonnet (L2 설계급 결함 탐지력 부족 우려) |
| D3 | 프리필터 = ruff + mypy + ollama(`qwen2.5-coder:7b` Phase 2 실사용 모델) — **무료 검사를 유료 리뷰 앞에** | ollama 제외 (보유 확인됨 — 0.18.0, 모델 3종. 제외할 이유 없음) |
| D4 | 인터페이스 = JSON envelope `{status, exit_code, backend, model, duration_s, stdout, stderr_sanitized, fallback_used, not_claimed}` (MS S1 차용 + M3) | 자유 텍스트 (호출측 LLM이 상태를 결정론적으로 확인 불가) |
| D5 | 휴먼 게이트 3곳 고정: 설계 PASS 확정 / 커밋 / 다음 페이즈 | 전면 자동 (MM Human 역할 규약 위반 + 승인 모델 붕괴) |
| D6 | 리뷰 세션 생성 주체 = 릴레이(외부) — Implementer는 리뷰어 존재를 모름 | 구현 세션 내부 생성 (C4 변형 위반 — 현 수동 프로세스의 빈틈 ②) |
| D7 | 바인딩 키 = MM 6역할명 (`implementer-reviewer:` 등), 모델은 값 | 모델명 키 (MS 방식 — 프로바이더 중립 상실) |

## 구조 (목표 상태)
```
ztr CLI
├─ review    ← v1 nitpicker.py + ruff/mypy 통합, finding 포맷
├─ gate      ← v1 engine/quality_gate.py 이식
├─ verify    ← v1 engine/post_merge_verifier.py 이식
├─ invariants ← 신규 (MM A3)
└─ run-phase ← 신규 (Phase 5, spike S1 조건부)
공유: config(역할 바인딩) / envelope / subprocess 안전(LESSON-001) /
      타임아웃 러너(MS S4) / CircuitBreaker·폴백(v1) / hooks Toast(v1)
삭제: orchestrator.py, consensus.py, ast_merger.py, Writer 에이전트군, H1/H5
```
