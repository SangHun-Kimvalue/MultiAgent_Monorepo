# ZRT v2 — Phase 1 구현 프롬프트 (Implementer 세션용)

작업: ZRT v2 — Phase 1 환경+골격 (v2 브랜치에서 DROP 자산 명시 삭제, envelope/바인딩 골격 신설, DESIGN.md v3.0 갱신). 한국어로 응답/주석.

[형상관리] 시작 전 `git status`. **`v2` 브랜치를 생성해 그 위에서 작업** (인터뷰 확정 결정 D1/D-3 — 로드맵 결정 로그 근거. 그 외 브랜치/태그 생성 금지). 커밋은 한국어, feat/fix/refactor/docs 프리픽스, 사용자 확인 후.

[전제] DISCOVERY_PASS 상태에서 시작. main 최신(90a7d1e, 227 tests 통과 상태). 선행 페이즈 없음 — v2 첫 페이즈.

[SoT — 반드시 읽을 것]
- `docs/ROADMAP_V2.md` — 결정 로그(§0)·불변 원칙(끝 절) 전부
- `docs/ZRT_V2_DIAGNOSIS.md` §3(KEEP/DROP/전환), §5(MS 차용 S1~S7)
- `docs/discovery/ztr-v2/design.md` — Boundaries 표, D1~D7
- `docs/LESSONS_LEARNED.md` — 특히 LESSON-001/002/003

[전략 리마인더] **빼는 페이즈다 — minimal-first.** v2 가치는 새 기능이 아니라 역할 축소(판단 반납)에서 나온다. "유지 가능 ≠ 유지". DROP 표에 없어도 Writer/orchestrator 전용이면 죽은 코드 — 의심되면 import 그래프로 실측 후 삭제. 반대로 KEEP 자산의 공개 시그니처는 건드리지 않는다.

[이번 범위]
1. **venv + dev 환경**: `.venv` 생성, `pip install -e .[dev]` (pytest/pytest-asyncio/mypy). pyproject.toml의 mypy `python_version = "3.13"` 실환경과 일치 확인.
2. **DROP 명시 삭제** (각각 별도 커밋 또는 분류별 커밋 — 침묵 삭제 금지 C3):
   - 확정 DROP: `src/engine/orchestrator.py`, `src/engine/consensus.py`(v1 Verdict 포함 — 재사용 금지), `src/engine/ast_merger.py`, `src/engine/round_diff.py`(H5), `src/agents/claude_code.py`(Writer), `src/agents/gemini.py`(gemini 워커 — Non-Goal)
   - 조건부 DROP (orchestrator/Writer 외 참조가 없음을 grep으로 실측 후 삭제, 커밋 메시지에 근거 기재): `src/engine/prompt_adapter.py`(H1 계열), `src/engine/summarizer.py`, `src/engine/context_extractor.py`
   - `src/runner.py`: DROP 모듈 의존 서브커맨드(`run` 등) 제거. `invoke/health/list-agents/history/stats/web` **및 `feedback/issues/decisions` 유지** — 뒤 3개는 진단 §3 "전환" 자산(H2/H3 → 판정 이력·정확도 추적으로 축소)이므로 삭제 금지.
   - 테스트: 각 test 파일의 import를 실측해 DROP 모듈 전용 테스트를 **명시 삭제 커밋**으로 제거 (예상: test_e2e.py, test_claude_code.py 등 — 추정 금지, import로 판정). **혼재 파일은 삭제가 아니라 분리(salvage)**: ① `tests/test_harness_advanced.py`는 H4(quality_gate)+H6(post_merge_verifier)+H5(round_diff)가 한 파일 — H5 테스트 클래스만 제거하고 H4/H6은 보존(Phase 3 이식 대상). ② `tests/test_phase3.py`·`test_phase5_recovery.py`(CircuitBreaker)·`test_phase5_unit.py`(ToastHook/HookManager)는 orchestrator를 import하지만 **KEEP 자산의 유일한 테스트** — orchestrator 의존부만 걷어내고 KEEP 자산 테스트는 반드시 살릴 것. ③ `tests/test_gemini_ollama.py`는 gemini(DROP)+ollama(KEEP) 혼재 — ollama 테스트 분리 보존.
3. **envelope 골격 신설**: `src/envelope.py` — Pydantic 모델 `Envelope {status, exit_code, backend, model, duration_s, stdout, stderr_sanitized, fallback_used, not_claimed}` (D4) + verdict enum `PASS | CHANGES_REQUESTED | BLOCKED` + exit code 매핑(0/1/2, 124=timeout, 70=internal). stderr redaction: 32자+ 토큰 `[REDACTED]` (MS S3).
4. **역할 바인딩 config** (G5/D7): `src/config/agents.config.yaml`에 `roles:` 최상위 키 신설 — MM 역할명 키(예: `implementer-reviewer: {backend: claude_cli, model: sonnet, call_type: headless, l2_model: opus}`, `mechanical: {backend: ollama, model: "qwen2.5-coder:7b"}`). `src/config/schema.py`에 대응 Pydantic 모델 추가. 기존 `agents:` 목록은 KEEP 에이전트(nitpicker-prefilter, ollama-local)만 남김. ollama 모델 값은 `llama3.1:8b` → `qwen2.5-coder:7b`로 갱신(D-4, Phase 2 실사용 모델).
5. **DESIGN.md v3.0 갱신**: v1 스펙(v2.9)을 v2 구도로 — 첫 줄에 M1 역할 계약("ZRT = Mechanical 역할. 검사만 하고 판단·편집 안 함"), Boundaries 표, 구조도(design.md §구조), discovery 문서 포인터. v1 절은 삭제하되 교훈 인용은 유지.

★ 명시적 out-of-scope ★ — 다음 페이즈로:
- `ztr review/gate/verify/invariants/run-phase` 서브커맨드 구현 (Phase 2~5)
- NitpickerAgent에 ruff/mypy 통합 변경, finding 포맷 (Phase 2)
- H4/H6 코드 이동·이식 (Phase 3 — 이번엔 파일 위치 그대로 보존)
- 대시보드/web 어떤 변경도 (동결)
- claude/codex CLI 연동 코드 (Phase 5, spike S1 잠금)

[부착점/대상] (라인 이동 가능 → 시그니처로 재확인 후 부착)
| 위치 | 내용 |
|---|---|
| `pyproject.toml:26-27` | `[project.scripts] ztr = "src.runner:main"` — 유지. fastapi/uvicorn 등 web 의존성도 유지(동결이지 삭제 아님) |
| `src/runner.py:316` `main()` | 서브커맨드 등록부 — DROP 명령 제거 지점 |
| `src/runner.py:113` `cmd_run` | orchestrator 의존 — 삭제 대상 |
| `src/config/schema.py:142` `RoundtableConfig` | `roles:` 필드 추가 지점. `OrchestrationConfig`(:52)·`PromptAdapterConfig`(:106)·`ContextConfig`(:87)는 orchestrator/Writer DROP과 함께 참조 0 — 제거 검토(참조 실측) |
| `src/config/agents.config.yaml:60-72` | `orchestration:` 블록 — DROP 대상. `prompt_adapter:`(74-80)·`context:`(82-88) 블록도 동반 DROP 검토 |
| `tests/test_config.py:77` | `cfg.orchestration.max_rounds` 단언 — orchestration 제거 시 수정 필요 |
| `src/engine/consensus.py:25` | v1 `Verdict(pass/conditional/fail)` — 삭제. v2 enum은 `src/envelope.py`에 신규 |
| `src/agents/discovery.py:24` `discover_agents()` | 에이전트 자동 등록 — claude_code/gemini 모듈 삭제 시 import 에러 안 나는지 확인 |

[아키텍처 결정] (틀리기 쉬운 지점)
- v2 verdict enum은 **신규 작성**이다. v1 `Verdict`를 import하거나 값 체계(conditional 등)를 끌고 오지 말 것.
- envelope은 **모든 ztr 명령의 stdout 계약**이 될 모델이다(MS S1). 이번 페이즈에선 모델+단위 테스트만 만들고 runner에 배선하지 않는다.
- 바인딩 키는 **역할명**(MM 6역할), 모델명은 값이다(D7). 모델명을 키로 쓰지 말 것.
- 패키지명 `src` 유지 — 디렉토리 rename 금지(로드맵 결정 로그).

[불변 원칙] `docs/ROADMAP_V2.md` 끝 절 1~7 전부 적용. 특히:
- 코드가 파싱하는 LLM 출력은 verdict enum + exit code뿐 (R5 — 위반 시 리뷰 자동 BLOCKED).
- 파일 I/O `encoding='utf-8'` 명시. subprocess 3단 방어(LESSON-001).
- Timebox: 같은 축 3회 실패 시 STOP + 보고.

[검증/DoD]
1) 테스트: `pytest` 전체 GREEN (DROP 후 남은 테스트 기준). `src/envelope.py`·`roles:` 스키마에 deterministic 단위 테스트 신규(직렬화 왕복, exit code 매핑, redaction, config 로드). **KEEP 자산 커버리지 잔존 확인**: circuit_breaker / hooks(Toast) / ollama / quality_gate(H4) / post_merge_verifier(H6) 각각에 대해 테스트가 1건 이상 살아있음을 완료 보고에 명시 — 0건이어도 GREEN이 되는 함정 차단(R2).
2) 타입: `mypy src` strict 통과 (남은 트리 전체).
3) 정적: `ruff check src tests` 통과.
4) DESIGN.md v3.0 — Boundaries 표·M1 역할 계약 포함 확인.
5) 리뷰 ALL PASS (둘 다, 하드 게이트):
   - B. **Nitpicker(기계)**: v1 nitpicker + ruff 수동 실행(R6 — Phase 3 전까지 v1로 검증). REJECT→수정→재실행.
   - A. **별도 Reviewer(설계)**: Planner가 집행(Codex 설계리뷰 세션 또는 Claude Agent 서브에이전트). Implementer는 review bundle(변경 파일 목록 + 핵심 diff + 검증 출력 요약 + 남은 질문)만 완료 보고에 첨부. **NOT CLAIMED로 닫지 말 것.**
6) 커밋은 사용자 확인 후. 페이즈 경계를 ROADMAP_V2.md §1 상태·커밋 메시지에 기록.

[완료 보고] 변경/삭제 파일 목록(커밋별) / 핵심 결정·부착 위치 / 검증 결과(실행 명령+출력 요약) / **PASS는 어디까지·NOT CLAIMED·가정** / 새 LESSON 발견 시 LESSONS_LEARNED.md append 여부.
