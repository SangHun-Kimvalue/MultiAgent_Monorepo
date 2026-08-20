# ZRT v2 — Phase 4 구현 프롬프트 (Implementer 세션용)

작업: ZRT v2 — Phase 4 `ztr invariants` (MM 인바리언트 사실 검사, envelope stdout 계약 유지, 의미 판단 금지). 한국어로 응답/주석.

[형상관리] 시작 전 `git status`. **`v2` 브랜치 유지** — 새 브랜치/태그 생성 금지. 커밋은 한국어, feat/fix/refactor/docs 프리픽스, 사용자 확인 후.

[전제] Phase 3 완료·커밋된 상태에서 시작. 기준선: `ztr review --changed`, `ztr gate`, `ztr verify --post-merge` 사용 가능. `pytest` 162 passed, `ruff check src tests` PASS, `mypy src` PASS.

[SoT — 반드시 읽을 것]
- `docs/ROADMAP_V2.md` — §0 결정 로그·끝 절 불변 원칙
- `docs/DESIGN.md` — v3.0 Mechanical 역할 계약, Boundaries
- `docs/ZRT_V2_DIAGNOSIS.md` §4(M4/M5), §5(S1/S3/S6)
- `docs/discovery/ztr-v2/design.md` — Boundaries 표
- `docs/discovery/ztr-v2/validation_plan.md` — PASS vs NOT CLAIMED
- `docs/LESSONS_LEARNED.md` — LESSON append 규칙

[전략 리마인더] Phase 4는 의미 해석기가 아니라 규약 준수 여부를 확인하는 파일시스템 사실 검사다. 코드가 “좋은 결정인지” 판단하지 않고, 정해진 파일·문구·형식·변경 사실이 존재하는지만 확인한다.

[이번 범위]
1. **`ztr invariants` 서브커맨드 신설**:
   - stdout은 Envelope JSON 단일 객체.
   - 기본 실행은 현재 repo root 기준.
   - 옵션: `--since <git-ref>`로 비교 기준 지정(기본 `HEAD` 또는 변경 작업 중이면 working tree 기준), `--paths <path...>`는 선택.
   - 결과 payload는 `{"checks": [...], "summary": {...}}` 고정.
2. **인바리언트 체크 목록(Phase 4 최소)**:
   - `docs/LESSONS_LEARNED.md`: 새 LESSON이 있으면 `LESSON-NNN` 형식과 append 위치 확인. 번호 역행·중복은 `CHANGES_REQUESTED`.
   - `docs/ROADMAP_V2.md`: 현재 Phase 상태 또는 다음 Phase 상세이 변경되었는지 확인. Phase 변경 작업인데 갱신이 없으면 `CHANGES_REQUESTED`.
   - `docs/prompts/v2_phase*.md`: 다음 Phase 프롬프트 파일 존재 여부 확인. 없으면 `CHANGES_REQUESTED`.
   - finding 포맷: 변경된 prompt/docs/tests에서 M2 5필드 명칭(`severity`, `finding`, `evidence_or_repro`, `impact`, `recommendation`)이 필요한 곳에 누락되지 않았는지 문자열 수준으로 확인.
   - NOT CLAIMED 표기: 검증/완료 보고 템플릿 또는 로드맵에 NOT CLAIMED 문구가 유지되는지 확인.
   - stdout envelope 계약: `src/runner.py`의 신규 ztr 명령이 `Envelope` 또는 `as_stdout_payload()` 경로를 사용하는지 문자열/AST 기반으로 확인.
3. **판정 규칙**:
   - 모든 체크 통과 = `PASS` exit 0.
   - 규약 위반 발견 = `CHANGES_REQUESTED` exit 1.
   - git 실행 불능, 파일 읽기 불능, 파서/검사 자체 오류 = `BLOCKED` exit 2 또는 70/124 규약.
4. **구현 구조**:
   - `src/engine/invariants.py` 신규 권장.
   - check는 작은 함수 또는 객체로 분리. 한 함수에 모든 규칙을 몰아넣지 말 것.
   - `runner.py`는 CLI 배선과 envelope 출력만 담당.

★ 명시적 out-of-scope ★
- MM 캐논(`D:\MultiAgent_Methodology`)과의 양방향 동기화 또는 수정
- 의미론적 설계 품질 평가
- 문서 자동 수정
- `--handoff-line` 출력(M5)과 progressive disclosure(S6)의 완성형
- Phase 5 `run-phase`, claude/codex 릴레이, CircuitBreaker 상주 배선
- 대시보드/web 변경

[부착점/대상]
| 위치 | 내용 |
|---|---|
| `src/runner.py` `main()` | `invariants` 서브커맨드 등록 |
| 신규 `src/engine/invariants.py` | 파일/문자열/git 사실 검사 엔진 |
| `src/envelope.py` | stdout 계약. 필드 추가 금지 |
| `docs/ROADMAP_V2.md` | Phase 4 완료 시 상태·다음 Phase 상세 갱신 |
| 신규 `tests/test_invariants.py`, `tests/test_invariants_cli.py` | deterministic 규약 테스트 |

[아키텍처 결정] (틀리기 쉬운 지점)
- 코드가 문서 내용을 “옳다/그르다” 판단하지 않는다. 존재·형식·갱신 여부만 본다.
- git diff를 사용할 때 삭제 파일·untracked 파일을 구분한다. untracked prompt/doc도 검사 대상에 포함한다.
- invariant 실패는 review finding과 같은 M2 5필드 payload로 보고한다.
- `ztr invariants` 자체가 실패해도 stdout은 envelope 한 덩어리여야 한다.

[불변 원칙] `docs/ROADMAP_V2.md` 끝 절 1~7 전부 적용. 특히 R5 Boundaries, M3 PASS vs NOT CLAIMED, stdout 단일 envelope, Windows/인코딩, subprocess 3단 방어, Timebox.

[검증/DoD]
1. unit: 각 invariant check PASS/CHANGES_REQUESTED/BLOCKED 경로.
2. deterministic CLI: `ztr invariants` envelope schema, exit code 0/1/2, git stub 또는 tmp repo 기반 테스트.
3. 회귀: `pytest` 전체 GREEN, `mypy src` strict, `ruff check src tests` PASS.
4. dogfood: `python -m src review --changed`, `python -m src gate <fixture>`, `python -m src verify --post-merge --changed`, `python -m src invariants`를 Phase 4 변경분에 실행.
5. 리뷰 ALL PASS:
   - B. 기계: `ztr review --changed` + ruff/mypy + `ztr invariants`.
   - A. 별도 Reviewer(설계): Planner가 집행. Implementer는 review bundle만 첨부. NOT CLAIMED로 닫지 말 것.
6. 커밋은 사용자 확인 후. 페이즈 경계를 ROADMAP_V2.md §1·§3과 커밋 메시지에 기록.

[완료 보고]
변경 파일 / 핵심 결정·부착 위치 / 검증 결과(명령+요약) / PASS는 어디까지·NOT CLAIMED·가정 / 새 LESSON 발견 시 LESSONS_LEARNED.md append 여부.
