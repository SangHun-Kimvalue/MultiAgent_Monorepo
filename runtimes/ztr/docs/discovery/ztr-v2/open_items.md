# Open Items — ZRT v2

> 규칙: TBD를 남기지 않는다. Assumption / Decision / Evidence Required로 재분류하고 gate impact를 붙인다.
> **TBD count: 0** ✅

## Evidence Required

| ID | 항목 | 근거 상태 | Gate impact |
|---|---|---|---|
| E-1 (P1) | `claude`/`codex` 헤드리스 CLI의 Windows 가용성 — 2026-06-13 실측: PowerShell PATH·npm 전역·`~/.local/bin`·WSL 모두 **부재**. 설치(npm `@anthropic-ai/claude-code` 또는 네이티브 인스톨러, codex CLI) 후 `-p`/`--model`/`--resume`·`codex exec` 플래그 실측 spike(S1) 필요 | 미확보 | **Phase 5(릴레이) 진입 차단.** Phase 1~4 비차단 — 전체 게이트는 통과 가능 |

## Decision (인터뷰·실측으로 확정)

| ID | 결정 | 출처 |
|---|---|---|
| D-1 | 구현 Reviewer = Sonnet 헤드리스 기본, L2 페이즈만 Opus | 인터뷰 2026-06-13 |
| D-2 | PASS = unit + deterministic + live 1회 (full E2E는 NOT CLAIMED) | 인터뷰 2026-06-13 |
| D-3 | 같은 repo 점진 리팩토링 (v2 브랜치) | 인터뷰 2026-06-13 |
| D-4 | nitpicker 프리필터에 ollama 포함 (0.18.0 + qwen2.5-coder 실측 확인) | 인터뷰 + 실측 |
| D-5 | mypy 미설치·venv 부재 → Phase 1 환경 구성 작업에 포함 | 실측 2026-06-13 |
| D-6 | 대시보드 동결 (신규 개발 없음, 기존 자산 보존) | C5 — 실수요 시 부활 |

## Assumption (반증되면 해당 페이즈에서 재결정)

| ID | 가정 | 위험도 | 검증 시점 |
|---|---|---|---|
| A-1 | `qwen2.5-coder:7b`가 nitpicker 보조 역할에 충분 (ruff+mypy가 1차 방어 전제) | P3 | Phase 2 live 1회 |
| A-2 | Claude Code 데스크탑 앱과 별개로 CLI 설치가 가능하고 동일 구독 쿼터를 사용 | P2 | spike S1 |
| A-3 | Sonnet의 구현 리뷰(결정충실성+diff)가 실용 수준 — L2급 결함은 Opus leg가 커버 | P2 | Phase 6 dogfood LESSON으로 축적 |
