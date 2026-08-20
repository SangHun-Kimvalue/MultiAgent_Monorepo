# Discovery Brief — ZRT v2 (1페이지)

> 2026-06-13 · Discovery 세션 작성 · 게이트 판정의 근거 스냅샷

## Problem
1. MM 리뷰 하드게이트의 Mechanical leg가 `run_nit.py` 단일 스크립트 수준 — 규약 준수(Nitpicker 실행, HANDOFF 갱신, finding 포맷)가 honor system이라 AI instruction decay에 취약.
2. 실사용 수동 페이즈 파이프라인(Opus 설계 → Codex 리뷰 → Codex 구현 → 리뷰 → 닛피커 → 커밋)에서 사람이 메시지 버스 역할(세션 간 전달·순서·완료 확인).
3. **Opus 토큰 제약으로 구현측 교차 리뷰를 포기** → 구현 검증 체인이 전부 Codex 계열(맹점 상관) + 구현 세션이 자기 리뷰어를 생성(C4 변형 위반).

## Baseline Failure (기존 방식의 실제 실패)
- **ZRT v1**: 판단 루프(Writer→Critic→Consensus)를 코드로 지휘 → LLM 출력 의미 파싱 → 하네스 군비경쟁(H1~H6) + 오케스트레이터 자체가 유지보수 제품화 → 은퇴.
- **현 수동 프로세스**: 동작하지만 위 Problem 2·3을 사람이 흡수. 페이즈마다 동일 비용 반복.

## New Value (이 작업이 추가하는 것)
- **가치 1 — 기계 검증 게이트**: `ztr review/gate/verify/invariants` — 무료 프리필터(ruff+mypy+ollama)와 MM 규약 자동 검사. honor system을 파일시스템 사실 확인으로 치환. *(환경 evidence 확보 완료)*
- **가치 2 — 릴레이로 교차 리뷰 복원**: `ztr run-phase`가 구현 리뷰를 **Sonnet 헤드리스 1회 호출**(스코프드 brief)로 자동 삽입 — 상주 Opus 세션 비용 없이 Implementer=Codex ↔ Reviewer=Claude 교차 복원. *(헤드리스 CLI evidence 미확보 — Phase 5 진입 조건)*

## Core Blockers
- **S1 (Evidence Required, P1)**: `claude`/`codex` 헤드리스 CLI가 현재 Windows PATH에 미설치 (npm 전역·~/.local/bin·WSL 실측 확인, 2026-06-13). 설치 + `-p/--model/--resume`·`codex exec` 플래그 실측 spike가 **Phase 5 진입 게이트**. Phase 1~4는 비차단.
- DESIGN.md v2.9(v1 스펙)가 현 SSoT — Phase 1에서 v3.0 갱신 필요 (스펙 표류 방지).

## Gate Recommendation
**DISCOVERY_PASS** — 단 스코프 조건부: Phase 1~4 무조건, Phase 5(릴레이)는 spike S1 통과 후 진입.
