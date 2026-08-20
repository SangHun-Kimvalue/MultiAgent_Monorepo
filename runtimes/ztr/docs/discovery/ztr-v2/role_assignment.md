# Role Assignment — ZRT v2

> 역할은 모델이 아니라 세션에 부여한다 (MULTI_AGENT.md).

| 역할 | 배정 | 비고 |
|---|---|---|
| **Discovery** | 본 세션 (Claude Opus, 2026-06-13) | 본 문서 세트 작성. 구현하지 않음 |
| **Planner** | 차기 Claude 세션 (Opus) | handoff.md의 페이즈 제안을 검토·확정 → `phased-implementation-handoff`로 로드맵+페이즈 프롬프트 작성 |
| **Implementer** | Codex 구현 세션 (사용자 운영 방식 유지) 또는 Claude 세션 | 페이즈 프롬프트 단위 실행. 자기 리뷰어 생성 금지 (C4) |
| **Reviewer (설계)** | Codex 설계리뷰 세션 | 기존 운영 방식 유지 (Opus↔Codex 교차) |
| **Reviewer (구현)** | **Sonnet 헤드리스 1회 호출** (L2 페이즈만 Opus) | 인터뷰 확정 2026-06-13. 릴레이(Phase 5) 전까지는 수동 트리거 |
| **Mechanical** | ZRT 자신 (v1 nitpicker + ruff → v2에서 `ztr review`) | dogfood: v2를 만들면서 v2로 검증 |
| **Human** | 사용자 | 설계 PASS 확정 · 커밋 · 다음 페이즈 — 휴먼 게이트 3곳 |
