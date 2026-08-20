# ZRT v2 — Phase 5 구현 프롬프트 (Implementer 세션용)

작업: ZRT v2 — Phase 5 `ztr run-phase` deterministic relay 골격. 한국어로 응답/주석.

[형상관리] 시작 전 `git status`. **`v2` 브랜치 유지** — 새 브랜치/태그 생성 금지. 커밋은 한국어, feat/fix/refactor/docs 프리픽스, 사용자 확인 후.

[전제] Phase 4 완료·커밋된 상태에서 시작. 기준선: `ztr review --changed`, `ztr gate`, `ztr verify --post-merge`, `ztr invariants` 사용 가능. ruff/mypy/pytest와 2-leg 리뷰가 PASS인 상태.

[SoT — 반드시 읽을 것]
- `docs/ROADMAP_V2.md` — §0 spike S1 상태, 끝 절 불변 원칙
- `docs/DESIGN.md` — Boundaries: 판단·편집·머지 금지
- `docs/ZRT_V2_DIAGNOSIS.md` §6 파이프라인 릴레이
- `docs/discovery/ztr-v2/design.md` — D5/D6, Boundaries 표
- `docs/discovery/ztr-v2/validation_plan.md` — PASS vs NOT CLAIMED
- `docs/LESSONS_LEARNED.md` — Windows subprocess/인코딩 교훈

[이번 범위]
1. `ztr run-phase` 서브커맨드의 deterministic 릴레이 골격.
2. 실제 claude/codex live 호출 전, fake CLI 스텁 기반으로 순서·timeout·exit routing·Envelope 캡처를 고정.
3. 입력/출력 envelope 저장과 verdict enum 라우팅만 수행. 리뷰 자연어 의미 해석 금지.
4. stdin EOF 처리, timeout, process kill은 LESSON-001과 ROADMAP §0 spike S1 주의사항을 따른다.
5. finding을 출력해야 하는 경우 M2 5필드 `severity / finding / evidence_or_repro / impact / recommendation`을 유지한다.

★ 명시적 out-of-scope ★
- 소스 코드 자동 편집·머지·rollback
- 리뷰 내용 의미 파싱 또는 설계 수락 판단
- Phase 6 full E2E dogfood
- 대시보드/web 변경
- `ztr roundtable --once`

[부착점/대상]
| 위치 | 내용 |
|---|---|
| `src/runner.py` | `run-phase` 서브커맨드 등록 |
| 신규 `src/engine/phase_relay.py` | fake/live CLI 실행 경계와 라우팅 |
| `src/envelope.py` | stdout 계약. 필드 추가 금지 |
| 신규 `tests/test_phase_relay.py`, `tests/test_run_phase_cli.py` | deterministic fake CLI 테스트 |

[검증/DoD]
1. unit: fake CLI PASS/CHANGES_REQUESTED/BLOCKED, timeout, stdin close 경로.
2. deterministic CLI: `ztr run-phase` envelope schema, exit code 0/1/2.
3. 회귀: pytest 전체 GREEN, mypy src strict, ruff check src tests PASS.
4. dogfood: `ztr review --changed`, `ztr verify --post-merge --changed`, `ztr invariants`.
5. 리뷰 ALL PASS: 기계 + 별도 Reviewer.

[완료 보고]
변경 파일 / 핵심 결정·부착 위치 / 검증 결과 / PASS는 어디까지·NOT CLAIMED·가정 / 새 LESSON 발견 시 append 여부.
