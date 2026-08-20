# ZRT v2 — Phase 2 구현 프롬프트 (Implementer 세션용) — v2 (외부 설계 리뷰 반영판)

작업: ZRT v2 — Phase 2 `ztr review` (ruff+mypy 내장 실행 + ollama 보조, M2 finding 포맷, M3 NOT CLAIMED, envelope 첫 배선). 한국어로 응답/주석.

[형상관리] 시작 전 `git status` — 클린이어야 함(아니면 STOP 후 보고). **`v2` 브랜치 유지** — 새 브랜치/태그 생성 금지. 커밋은 한국어, feat/fix/refactor/docs 프리픽스, 사용자 확인 후.

[전제] Phase 1 + 후속 보강 커밋 완료 상태에서 시작. 기준선: pytest **133 passed / 3 skipped**, `ruff check src tests` PASS, `mypy src`(strict, **python_version=3.12** — 3.13으로 올리지 말 것, 로드맵 §0 재확정) PASS.

[SoT — 반드시 읽을 것]
- `docs/ROADMAP_V2.md` §0 결정 로그 — 특히 **`ztr review` 출력 계약 / mypy 실행 단위 / fallback_used 의미 / CircuitBreaker 이관 / 외부 Nitpicker Daemon 의존 제거** 5행. 끝 절 불변 원칙 1~7.
- `docs/discovery/ztr-v2/design.md` — Boundaries 표, D3/D4
- `docs/ZRT_V2_DIAGNOSIS.md` §4(M2/M3), §5(S1/S3/S4)
- `docs/LESSONS_LEARNED.md` — LESSON-001(Windows subprocess)

[전략 리마인더] **무료 검사를 유료 리뷰 앞에** — ruff/mypy(결정론·무료)가 1차 방어, ollama는 보조(R4: 보조로만 claim). "구현 가능 ≠ 지금 다 구현": Layer 3 고도화·프롬프트 튜닝·정확도 평가·CircuitBreaker 배선은 범위 밖. 수동 파이프라인에 **즉시 끼워 쓸 수 있는** 최소 완결이 목표(Success Criteria 1).

[이번 범위]
1. **외부 의존 제거**: `NitpickerAgent`의 외부 Nitpicker Daemon(`jemmin` sys.path import) 의존을 제거하고 ruff/mypy를 **ztr 내부 subprocess로 직접 실행**. `D:\Nitpicker`가 현재 존재하지만 결정은 유지(로드맵 §0 — 이식성·역할 순수성). subprocess는 LESSON-001 3단 방어(communicate + wait_for + finally kill), 타임아웃 시 exit 124 의미 보존(MS S4).
2. **실행 단위 (로드맵 §0 고정 — 변경 금지)**:
   - **mypy: 항상 고정 타깃 `src` 전체** (`{sys.executable} -m mypy src --output json`). 부분 파일 검사 금지 — import graph 누락으로 "통과처럼 보이는 누락"이 생긴다.
   - **ruff: 변경 파일 단위 가능** (`--output-format json`). `--changed`는 ruff 대상 선별과 ollama용 diff payload 산출에만 사용.
3. **도구 출력 계약 (실측 고정, 2026-06-13)**:
   - ruff `--output-format json` → JSON 배열 (ruff 0.15.17 실측).
   - mypy `--output json` → **줄당 1개 JSON 객체**(JSONL — 배열 아님, mypy 2.1.0 실측): `{file, line, column, end_line, end_column, message, hint, code, severity}`. 파서는 이 필드만 해석 — 그 외 필드 등장 시 무시.
   - 도구 버전이 달라 계약이 깨지면 추측 보정하지 말고 STOP 후 보고(핵심 원칙 4).
4. **`ztr review` 서브커맨드 신설** (`src/runner.py` `main()` 등록부 — 시그니처로 재확인):
   - `ztr review --changed` (git diff 기반 ruff 대상 선별) / `ztr review <paths...>` (명시 대상)
   - **stdout = envelope JSON 단일 객체** (`src/envelope.py` `Envelope.as_stdout_payload`, 첫 배선). 사람용 로그는 stderr 또는 `-v`로 분리.
   - envelope 쌍 검증 주의: 124/70은 `BLOCKED`와만 쌍이 됨(`envelope.py` `validate_status_exit_code_pair` — Phase 1 후속 보강에서 추가됨).
5. **envelope.stdout 내부 payload 스키마 (로드맵 §0 고정)**: `json.dumps(..., ensure_ascii=False)`로 직렬화한
   `{"findings": [M2 5필드 객체...], "tool_results": {"ruff": {...}, "mypy": {...}}, "review_text": str|null, "summary": {"verdict": ..., "counts": ...}}`
   - findings 항목 = M2: `severity / finding / evidence_or_repro / impact / recommendation` (evidence 예: `file:line:col [code]`)
   - `review_text` = ollama 응답 원문(실행됐을 때만, 아니면 null) — **구조 분해·파싱 금지.**
6. **verdict 산출 (결정론)**: ruff/mypy **기계 결과만으로** 결정 — 둘 다 깨끗=PASS, 진단 있음=CHANGES_REQUESTED, 검사 자체 실행 불능=BLOCKED(exit 2). **ollama 출력은 verdict에 기여하지 않는다**(R4·R5) — `review_text`로 동봉만.
7. **ollama leg (선택 leg)**: 기존 `OllamaAgent.invoke()` 사용(시그니처 변경 금지), 모델은 config `roles: mechanical` 값(`qwen2.5-coder:7b`) — 하드코딩 금지. 호출은 try/except로 격리. **실패·미기동 시 `not_claimed=["ollama-review"]` + `fallback_used=false`** — fallback_used는 실제 대체 백엔드를 실행했을 때만 true(로드맵 §0). 리뷰 프롬프트는 diff + "5필드 finding 형식으로 보고" 고정 템플릿 — 페르소나/룰 파일 시스템 부활 금지.
8. **책임 분리 (God Object 방지)**: ① 대상 선별(--changed/paths) ② 도구 subprocess 실행 ③ 출력 파싱(JSON→finding 매핑) ④ verdict·payload 조립 — 이 4개 책임을 **함수/모듈 수준에서 분리**하라(예: `src/engine/static_review.py` 신설 후 NitpickerAgent는 어댑터로 축소). 클래스 개수·이름은 재량이되 NitpickerAgent 한 몸에 다 넣지 말 것. `agent_type = "nitpicker"`와 IAgent 인터페이스(`base.py`)는 OCP 호환을 위해 유지.

★ 명시적 out-of-scope ★
- `ztr gate`/`verify`/`invariants`/`run-phase` (Phase 3~5), H4/H6 코드 이동 (Phase 3)
- **CircuitBreaker 배선** — Phase 5(상주 릴레이)로 이관(로드맵 §0). Phase 2는 try/except 격리 + not_claimed까지만.
- ollama 리뷰 *품질* 개선·프롬프트 튜닝·정확도 평가 (NOT CLAIMED 영역 — dogfood에서 LESSON으로)
- `--handoff-line`(M5), progressive disclosure(S6) — Phase 4~5에서
- claude/codex CLI 연동 (Phase 5)

[부착점/대상] (라인 이동 가능 → 시그니처로 재확인 후 부착)
| 위치 | 내용 |
|---|---|
| `src/runner.py` `main()` 서브커맨드 등록부 | `review` 파서 추가 지점 (Phase 1 후속 보강으로 라인 이동 — 시그니처로 확인) |
| `src/agents/nitpicker.py:45` `NitpickerAgent` | jemmin 의존 제거 대상 — IAgent 인터페이스·`agent_type` 유지, 내부는 ④번 분리 모듈에 위임 |
| `src/envelope.py` `Envelope` / `from_verdict` / `validate_status_exit_code_pair` | stdout 계약 — 필드 추가 금지(extra="forbid"). timeout/internal envelope이 필요하면 BLOCKED status와 쌍으로 생성 |
| `src/agents/ollama.py:26` `OllamaAgent`, `invoke()` | ollama leg 호출 지점 — 시그니처 변경 금지 |
| `src/config/agents.config.yaml` `roles: mechanical` | 모델/엔드포인트 읽기 — 하드코딩 금지 |
| `tests/test_nitpicker.py` | 기존 테스트 — jemmin mock 기반이면 내장 실행 기반으로 재작성 |

[아키텍처 결정] (틀리기 쉬운 지점)
- **verdict는 ruff/mypy 기계 결과만으로 결정론 산출.** ollama 텍스트를 파싱해 verdict에 반영하면 v1 ConsensusEngine 회귀(R5) — 설계 리뷰 자동 BLOCKED 사유다.
- **mypy를 변경 파일 단위로 돌리고 싶어져도 참아라** — 속도 최적화보다 누락 차단이 우선(로드맵 §0 결정).
- ruff/mypy는 `sys.executable -m ruff|mypy` 패턴으로 venv 내 실행 — PATH 의존 금지(Windows).
- envelope stdout payload는 위 5의 고정 스키마 외 키 추가 금지 — 호출측(LLM 세션)이 의존할 계약이다.
- IAgent 자동 등록(`__init_subclass__`) 경로를 깨지 말 것(OCP).

[불변 원칙] `docs/ROADMAP_V2.md` 끝 절 1~7 전부. 특히: R5 Boundaries, encoding='utf-8', subprocess 3단 방어, Timebox L1=동일축 3회 실패 시 STOP.

[검증/DoD]
1) unit: 도구 JSON 픽스처→M2 5필드 매핑, verdict 결정 규칙(PASS/CHANGES_REQUESTED/BLOCKED), not_claimed·fallback_used 경로, payload 스키마 직렬화.
2) deterministic: 가짜 ruff/mypy 스텁(고정 JSON 출력) + ollama mock으로 `ztr review` 전 흐름 — envelope 스키마 검증, exit code 0/1/2 분기, ollama 다운 시 `not_claimed=["ollama-review"]`+`fallback_used=false`. 모델 호출 0회.
3) **live 1회 — 통과 기준을 정확히**: ollama `qwen2.5-coder:7b` 실호출로 `ztr review` 1건 실행 후 ① 프로세스가 verdict 매핑대로 종료(exit 0/1/2) ② stdout이 Envelope 스키마로 파싱됨 ③ 결정론 verdict 존재 — 이 3개가 단언 대상의 전부다. **ollama 응답의 의미 품질은 PASS 주장에 포함하지 않는다(NOT CLAIMED, R4).** 재현 커맨드 전문 + 모델 태그를 완료 보고에 기록(C6).
4) 회귀: `pytest` 전체 GREEN(기준선 133+신규), `mypy src` strict(3.12), `ruff check src tests` 통과.
5) 리뷰 ALL PASS (둘 다, 하드 게이트):
   - B. **기계**: 완성된 `ztr review`를 자기 자신에 실행(dogfood 1보) + ruff/mypy.
   - A. **별도 Reviewer(설계)**: Planner가 서브에이전트로 집행. Implementer는 review bundle(변경 파일+핵심 diff+검증 출력 요약+남은 질문)만 첨부. **NOT CLAIMED로 닫지 말 것.**
6) 커밋은 사용자 확인 후. 페이즈 경계를 ROADMAP_V2.md §1·커밋 메시지에 기록.

[완료 보고] 변경 파일 / 핵심 결정·부착 위치 / 검증 결과(재현 커맨드 포함) / **PASS는 어디까지·NOT CLAIMED·가정** / 새 LESSON 발견 시 LESSONS_LEARNED.md append 여부. 권장 정리 2건 처리 여부 보고: `tests/test_gemini_ollama.py`→`test_ollama.py` rename, `tests/test_config.py`의 `claude_code_cli` 픽스처 문자열 교체.
