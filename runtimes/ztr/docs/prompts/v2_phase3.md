# ZRT v2 — Phase 3 구현 프롬프트 (Implementer 세션용)

작업: ZRT v2 — Phase 3 `ztr gate` + `ztr verify --post-merge` (H4/H6 배선, envelope stdout 계약 유지, 의미 판단/편집 금지). 한국어로 응답/주석.

[형상관리] 시작 전 `git status`. **`v2` 브랜치 유지** — 새 브랜치/태그 생성 금지. 커밋은 한국어, feat/fix/refactor/docs 프리픽스, 사용자 확인 후.

[전제] Phase 2 완료·커밋된 상태에서 시작. 기준선: `ztr review --changed` 사용 가능, `pytest` 148 passed, `ruff check src tests` PASS, `mypy src` PASS.

[SoT — 반드시 읽을 것]
- `docs/ROADMAP_V2.md` — §0 결정 로그·끝 절 불변 원칙
- `docs/DESIGN.md` — v3.0 Mechanical 역할 계약, Boundaries
- `docs/discovery/ztr-v2/design.md` — Boundaries 표, D4
- `docs/ZRT_V2_DIAGNOSIS.md` §3 KEEP(H4/H6), §4(M2/M3), §5(S1/S3/S4)
- `docs/LESSONS_LEARNED.md` — LESSON-001, LESSON-008, LESSON-016

[전략 리마인더] Phase 3도 판단 반납 원칙을 지킨다. `gate`와 `verify`는 기계적 품질·사실 검증만 수행하고, 리뷰 내용의 의미 해석·설계 수락·소스 편집·머지는 하지 않는다. stdout은 항상 envelope JSON 단일 객체다.

[이번 범위]
1. **`ztr gate` 신설**:
   - 입력: 구조화된 result JSON 파일 1개.
   - 기본 schema(최소): `{ "kind": "critic" | "writer", "verdict": "...", "findings": [...], "content": "..." }`.
   - `kind=critic`: `OutputQualityGate.validate_critic()`로 M2 findings의 기계적 품질 검사.
   - `kind=writer`: `OutputQualityGate.validate_writer()`로 빈 코드/짧은 코드/import-only 검사. Writer 자동 편집은 하지 않는다.
   - 결과: gate issue가 없으면 `PASS`, gate issue가 있으면 `CHANGES_REQUESTED`, 파일 읽기/JSON/schema 불능이면 `BLOCKED`.
   - envelope `stdout` payload는 JSON 문자열로 `{ "gate": {...}, "issues": [...], "input": {...} }` 고정.
2. **`ztr verify --post-merge` 신설**:
   - 입력: 명시 경로 `<paths...>` 또는 `--changed`.
   - 기존 `PostMergeVerifier.verify()`를 호출해 syntax/ruff/mypy 결과를 수집.
   - 하나라도 실패하면 `CHANGES_REQUESTED`, 실행 불능/대상 없음/도구 timeout은 `BLOCKED`, 전부 통과하면 `PASS`.
   - 파일 수정·rollback·merge는 하지 않는다.
   - envelope `stdout` payload는 `{ "verified": [...], "summary": {...} }` 고정.
3. **stdout/stderr 계약 유지**:
   - stdout은 envelope JSON 한 덩어리만.
   - 사람용 로그는 stderr 또는 `-v`로만.
   - stderr는 `Envelope`/redaction 규칙 유지.
4. **테스트 정리**:
   - `tests/test_harness_advanced.py`의 H4/H6 테스트는 CLI/엔진 테스트로 보강하되, 기존 공개 시그니처를 불필요하게 바꾸지 않는다.
   - deterministic CLI 테스트에서 stdout envelope schema, exit code 0/1/2, BLOCKED 경로를 확인한다.

★ 명시적 out-of-scope ★
- `ztr invariants` (Phase 4)
- `ztr run-phase`, CircuitBreaker 상주 릴레이, claude/codex CLI 구동 (Phase 5)
- H4/H6 알고리즘 고도화, LLM 리뷰 품질 평가
- 소스 코드 자동 편집·머지·rollback
- 대시보드/web 변경

[부착점/대상]
| 위치 | 내용 |
|---|---|
| `src/runner.py` `main()` | `gate`, `verify` 서브커맨드 등록 |
| `src/engine/quality_gate.py` | H4 gate 엔진. 공개 시그니처 보존 우선 |
| `src/engine/post_merge_verifier.py` | H6 verify 엔진. 필요 시 wrapper만 추가 |
| `src/envelope.py` | stdout 계약. 필드 추가 금지(extra forbid 유지) |
| `tests/test_harness_advanced.py` | 기존 H4/H6 단위 테스트 보존 |
| 신규 `tests/test_gate_cli.py`, `tests/test_verify_cli.py` | deterministic CLI 계약 테스트 권장 |

[아키텍처 결정] (틀리기 쉬운 지점)
- `gate`는 리뷰 의미를 해석하지 않는다. 구조화된 verdict/findings/content의 기계적 자기모순·부실만 검사한다.
- `verify`는 파일 상태를 검사만 한다. 실패했다고 수정하거나 rollback하지 않는다.
- `verify --changed`는 Phase 2의 changed path 수집 로직을 재사용해도 되지만, mypy/ruff 실행 단위가 H6 목적에 맞는지 테스트로 고정한다.
- `PostMergeVerifier`가 내부적으로 ruff/mypy를 실행하더라도 stdout에는 raw 로그를 직접 뿌리지 않는다. envelope payload로만 요약한다.

[불변 원칙] `docs/ROADMAP_V2.md` 끝 절 1~7 전부 적용. 특히 R5 Boundaries, M3 PASS vs NOT CLAIMED, stdout 단일 envelope, Windows/인코딩, subprocess 3단 방어, Timebox.

[검증/DoD]
1. unit: `OutputQualityGate` critic/writer gate issue, `PostMergeVerifier` pass/fail 경로.
2. deterministic CLI: `ztr gate` PASS/CHANGES_REQUESTED/BLOCKED, `ztr verify --post-merge` PASS/CHANGES_REQUESTED/BLOCKED, envelope schema 및 exit code 매핑.
3. 회귀: `pytest` 전체 GREEN, `mypy src` strict, `ruff check src tests` PASS.
4. dogfood: `python -m src review --changed`를 Phase 3 변경분에 실행. 단언 대상은 envelope 구조·결정론 verdict·exit code.
5. 리뷰 ALL PASS:
   - B. 기계: `ztr review --changed` + ruff/mypy.
   - A. 별도 Reviewer(설계): Planner가 집행. Implementer는 review bundle만 첨부. NOT CLAIMED로 닫지 말 것.
6. 커밋은 사용자 확인 후. 페이즈 경계를 ROADMAP_V2.md §1·§3과 커밋 메시지에 기록.

[완료 보고]
변경 파일 / 핵심 결정·부착 위치 / 검증 결과(명령+요약) / PASS는 어디까지·NOT CLAIMED·가정 / 새 LESSON 발견 시 LESSONS_LEARNED.md append 여부.
