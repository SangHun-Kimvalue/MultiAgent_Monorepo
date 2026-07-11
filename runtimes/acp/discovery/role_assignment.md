# Role Assignment

## Workstream

- Project / feature: **Agent Control Plane (멀티 데스크톱 앱 세션 통합 관제소)**
- Date: 2026-06-09
- Owner: 상훈 (사령관 / Product Owner)

## Sessions

역할은 모델이 아니라 세션에 부여한다. 같은 모델이라도 세션별 책임이 다르다.

| Role | Session / model / surface | Responsibility | Notes |
|---|---|---|---|
| Discovery | 현재 세션 (Copilot CLI / claude-opus-4.8) | 인터뷰, open items, gate 판정 | 본 문서 작성 세션 |
| Planner | 별도 세션 (TBD 모델) | 로드맵 + phase 프롬프트 저작 | DISCOVERY_PASS 후 착수 |
| Implementer | 별도 세션 | 수집기/코어/대시보드 구현 + evidence | Planner 프롬프트 수령 후 |
| Reviewer | **Implementer 세션이 띄우는 독립 컨텍스트 서브에이전트** | 설계 리뷰(부착 정확성·저장/동시성·마이그레이션·결정 준수). 구현 세션이 자기 diff에 대해 집행 | 위험도 기반(L2/저장·동시성·마이그레이션/사용자 요청 시). Nitpicker는 항상. 둘 다 Implementer 집행, Planner 비관여 |
| Mechanical | Nitpicker Daemon local LLM + ruff/mypy/pytest | 로컬 게이트 / CI 검증 | Nitpicker는 `jemmin_cli.py --provider ollama` 기준, Gemini/API 키 경로 기본값 아님 |

## Constraints

- Same-session role overlap: Discovery 세션은 **구현하지 않는다**. 코드 작성 금지.
- Explicitly separated roles: Planner와 Implementer를 분리. **Reviewer 레그는 Implementer 세션이 독립 컨텍스트 서브에이전트로 집행**(자기 코드 인라인 리뷰가 아니라 별도 컨텍스트라 교차검증 성립). Planner는 리뷰를 직접 돌리지 않는다(역할 오염 방지).
- Commit authority: Implementer 세션만 커밋. PASS 합의 후 `Reviewed by ...` 규약(기존 RULES.md 계승).
