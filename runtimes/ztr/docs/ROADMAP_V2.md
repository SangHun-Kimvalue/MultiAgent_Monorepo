# ZRT v2 — 전체 페이즈 로드맵

작성일: 2026-06-13 (Planner 세션)
문서 성격: 다중 세션 협업용 진행 기준(SoT 보조). 페이즈 경계·결정·부착점의 cold-start 핸드오프.
대상/전제: ZRT v1 (227 tests) → v2 재포지셔닝. DISCOVERY_PASS (조건부: Phase 5는 spike S1 통과 후).
상위 SoT: `docs/discovery/ztr-v2/` (8종) · `docs/ZRT_V2_DIAGNOSIS.md` · `docs/DESIGN.md`(Phase 1에서 v3.0 갱신)

## 0. 확정 결정 로그 (변경 시 이 표를 갱신)

Discovery 확정 (출처: design.md D1~D7, open_items.md):

| 항목 | 확정값 | 근거 |
|---|---|---|
| 리팩토링 방식 | 같은 repo, **v2 브랜치** 점진 (D1, D-3) | 테스트 자산 227건 보존 |
| 구현 Reviewer | Sonnet 헤드리스 기본, L2만 Opus (D2, D-1) | 토큰 제약이 v2 존재 이유 |
| 프리필터 | ruff + mypy + ollama `qwen2.5-coder:7b` (D3, D-4, Phase 2 실사용 모델) | 무료 검사를 유료 리뷰 앞에 |
| 인터페이스 | JSON envelope `{status, exit_code, backend, model, duration_s, stdout, stderr_sanitized, fallback_used, not_claimed}` (D4) | MS S1 + M3 |
| 휴먼 게이트 | 3곳 고정: 설계 PASS / 커밋 / 다음 페이즈 (D5) | MM Human 규약 |
| 리뷰 세션 생성 주체 | 릴레이(외부) — Implementer는 리뷰어 존재를 모름 (D6) | C4 변형 위반 차단 |
| 바인딩 키 | MM 6역할명 키, 모델은 값 (D7) | 프로바이더 중립 |
| PASS 레벨 | unit + deterministic + live 1회. full E2E는 NOT CLAIMED (D-2) | validation_plan.md |

Planner 확정 (2026-06-13 본 세션 — 보류 항목을 여기서 못박음):

| 항목 | 확정값 | 근거 |
|---|---|---|
| 패키지명 | **`src` 유지**, CLI 표면만 `ztr` | pyproject.toml:27 `ztr = "src.runner:main"` 기존재. 18개 테스트 파일의 import 보존 — D1(점진) 귀결 |
| v2 verdict enum | **신규 정의** `PASS \| CHANGES_REQUESTED \| BLOCKED` — v1 `Verdict`(pass/conditional/fail, consensus.py:25)는 consensus.py와 함께 삭제, 재사용 금지 | M2 최종 판정 형식과 일치 |
| exit code 규약 | 0=PASS · 1=CHANGES_REQUESTED · 2=BLOCKED · 124=timeout(MS S4) · 70=internal error | 호출측(LLM 세션)이 결정론적 분기 |
| envelope 모듈 위치 | `src/envelope.py` (신규, 공유 계층) | 구조도 "공유: envelope" |
| 바인딩 config | `agents.config.yaml`에 `roles:` 최상위 키 추가(역할명→backend/model/call_type/`l2_model`). 기존 `agents:` 목록은 KEEP 에이전트만 남기고 축소 | G5, MM A1 |
| GeminiAgent 처분 | **확정 DROP** — 진단 §3 "전환"(바인딩 뒤로 이동) 행은 requirements.md Non-Goal("❌ gemini 워커")로 **대체(supersede)**됨 | 후속 세션 혼동 방지 기록 |
| 페이즈 프롬프트 위치 | `docs/prompts/v2_phaseN.md` (파일로 — 채팅은 세션을 못 넘는다) | 스킬 §6 |
| mypy 타깃 | `python_version = "3.12"` — 한때 3.13으로 복구됐으나 외부 설계 리뷰로 **3.12 재확정**(2026-06-13) | 실측(2026-08-10): `.venv` 실행 인터프리터는 3.13.5, 지원 하한은 `requires-python >=3.12`. 타입체크 타깃은 **최저 지원 런타임 기준**이 안전 — 3.13 타깃은 3.12 런타임에서 깨질 코드를 통과시킬 수 있음 |
| 외부 Nitpicker Daemon 의존 | **제거** — Phase 2에서 ruff/mypy를 ztr 내부 subprocess 직접 실행으로 교체. 후속: 데몬이 `D:\Nitpicker`로 복구돼 HEALTHY 확인됐으나 **결정 유지** — 제거 근거는 경로 부재만이 아니라 외부 프로젝트 sys.path import의 이식성·역할 순수성 문제(외부 설계 리뷰도 타당 판정) | 실측: 하드코딩 경로(`nitpicker.py:92`) 의존이 Phase 1에서 영구 NOT CLAIMED 유발. KEEP 대상은 "프리필터 능력"이지 jemmin import 메커니즘이 아님 |
| `ztr review` 출력 계약 | envelope.stdout = **고정 스키마 JSON 문자열** `{"findings": [...], "tool_results": {...}, "review_text": str\|null, "summary": {...}}` (`ensure_ascii=False`). 도구 출력은 구조화 포맷: ruff `--output-format json`(배열), mypy `--output json`(**줄당 1 JSON 객체** — mypy 2.1.0 실측 2026-06-13) | 외부 리뷰 P2 — envelope은 구조적인데 payload가 자유 텍스트가 되는 것 방지 |
| mypy 실행 단위 | **고정 타깃 `src` 전체** — `--changed`는 ruff 대상 선별·diff 산출에만 사용 | 외부 리뷰 P1 — 부분 파일 검사는 import graph 영향으로 "통과처럼 보이는 누락" 발생 |
| fallback_used 의미 | **실제 대체 백엔드 실행 시에만 true.** 선택 leg(ollama) 실패·생략 = `not_claimed=["ollama-review"]` + `fallback_used=false` | 외부 리뷰 P3 — M3 정직성과 일치 |
| CircuitBreaker 배선 시점 | Phase 2 제외(try/except 격리 + not_claimed까지만) — **Phase 5(상주 릴레이)로 이관** | 외부 리뷰 P3 — 1회성 CLI에서 breaker 상태 이력은 무효(C5/YAGNI) |
| run-phase 타임박스(L1=3회 STOP) | **영구 descope** — relay는 fail-fast 1패스 시퀀서(첫 non-PASS leg에서 나머지 skip). 자동 수정 루프(구현 재시도)는 미구현·미도입 | 자동 수정 루프 = code가 판단 루프 지휘 = v1 함정 재발(R5 정신). fail-fast가 더 옳음. 타임박스(동일축 N회)는 outer 세션 규약으로 잔류(MM L1) — Planner 결정 2026-06-13 |
| run-phase Toast 알림 | **descope(YAGNI)** — 헤드리스 relay는 호출 outer 세션이 envelope를 동기 수신하므로 별도 알림 불요. ToastHook(KEEP)은 보존 | 외부 설계 리뷰 §6 major 반영. 필요 시 재도입 저비용 — Planner 결정 2026-06-13 |
| invariants ROADMAP 검사 일반화 | `_check_roadmap_updated`의 phase_paths 하드코딩(Phase 4 고정) → `src/engine/`·`src/runner.py`·`src/config/`·`docs/prompts/v2_phase*` 일반 술어로 교체 | 외부 설계 리뷰 §5 major — 화석화된 게이트가 Phase 5+ 작업을 무검증 통과시키던 자기 무력화 버그. 수정 후 미커밋 engine 변경을 ztr가 스스로 검출 확인 |
| **spike S1 상태** | **완전 통과** (2026-06-13 실측, E-1 해소): ① claude leg ✅ — CLI v2.1.176 네이티브 설치(`~/.local/bin/claude.exe`, 사용자 PATH 등록), 사용자 `/login`(Max 구독) 후 `claude -p "..." --model sonnet` live 1회 성공(`SPIKE_S1_OK`) → A-2(구독 쿼터 공유) 검증됨. ② codex leg ✅ — Node v24.16.0(사용자 레벨 zip, `%LOCALAPPDATA%\Programs\nodejs`) + codex CLI 0.139.0(npm 전역), ChatGPT 로그인 기존재, `codex exec --skip-git-repo-check "..."` live 1회 성공(`SPIKE_S1_CODEX_OK`, gpt-5.5). **Phase 5 잠금 해제** — 진입은 Phase 4 완료 후 순차. 릴레이 구현 주의 실측 2건: (a) 비대화형 호출 시 stdin이 파이프면 두 CLI 모두 EOF 대기/경고 — stdin 명시 close(`< NUL`) 필수. (b) PATH는 사용자 레벨 등록 — 서비스/타 셸에서 전체 경로 폴백 고려 | open_items E-1 |
| Gemini CLI (참고 — v2 스코프 외) | 환경 실측 2026-06-13: `@google/gemini-cli` 0.46.0 설치(npm 전역), API 키·OAuth 파일 없이 `gemini -p` 헤드리스 1회 성공(`gemini-3.5-flash`, 기본 무료 티어로 추정). 헤드리스 자동화 시 `GEMINI_CLI_TRUST_WORKSPACE=true` 필요(trusted-folders 게이트). **v2 코드의 gemini 워커 Non-Goal은 유지** — 필요해지면 바인딩 레지스트리 값으로만 추가(코드 변경 불필요, D7). **⚠️ Gemini는 CLI(`gemini -p`, OAuth 무료 티어)로만 사용 — API 콜/API 키 백엔드 금지**(유료 크레딧 만료, 2026-06-13 사용자 확정). 바인딩 추가 시 반드시 CLI leg로 묶는다(run-phase의 CLI relay 모델과 일치). | 환경 가용성 + **Gemini CLI 전용 제약** |

## 1. 페이즈 개요

| Phase | 등급 | 내용 | 상태 |
|---|---|---|---|
| 1. 환경+골격 | L1 | venv+mypy, v2 브랜치, DROP 명시 삭제, envelope, 역할 바인딩 config, DESIGN.md v3.0 | ✅ 완료 (2026-06-13, 리뷰 2-leg PASS) |
| 2. ztr review | L1 | ruff+mypy+ollama 프리필터 통합, finding 포맷(M2), NOT CLAIMED(M3) | ✅ 완료 (2026-06-13) |
| 3. ztr gate + verify | L1 | v1 H4·H6 이식. **이 페이즈부터 자기 검증(dogfood) 전환** (R6) | ✅ 완료 (2026-06-13) |
| 4. ztr invariants | L1~L2 | MM 인바리언트 목록 정의(MM A3 캐논과 협의) + 파일시스템 사실 검사 | ✅ 완료 (2026-06-13) |
| 5. ztr run-phase | L2 | 릴레이 — 헤드리스 구동, verdict 라우팅, fail-fast 캡처 (휴먼 게이트=outer 세션 / Toast·타임박스=descope, §0) | ✅ 완료 (2026-06-13) |
| 6. dogfood | — | relay mechanics live dogfood + PASS/NOT CLAIMED claim 경계 감사 | ✅ 완료 (2026-06-13, full E2E/실제 개선 루프 NOT CLAIMED) |
| 7. 통합 E2E + UI 관측 | L1~L2 | CLI 체인 E2E + full relay live + v2 결과 SessionStore 관측 배선 + 대시보드 실측 (Phase 6 NOT CLAIMED full-e2e claim 시도) | ✅ 완료 (2026-06-13, relay/UI 관측 E2E PASS, 자동 개선 루프 NOT CLAIMED) |
| 8. run-phase resume 체인 | L2 | 헤드리스 leg session-id 캡처(JSON stdout) + `--session-map` 영속 + 다음 페이즈 resume 재주입. 신규 인자 `--implementer-resume`/`--reviewer-resume`/`--session-map` | ✅ 완료 (2026-06-13, master 머지) — spike S2 PASS로 잠금 해제 후 `runtimes/ztr/` 흡수. §7 명시-id resume 실패 BLOCKED 승격 + silent-new-session 차단 + invariants 모노레포 경로 수정까지 2-leg 리뷰 PASS. full 오케스트레이션 자동화는 NOT CLAIMED(dogfood) |

**페이즈 2개 이상 묶지 말 것** (discovery handoff 인계 주의사항). 각 페이즈 완료 시 이 표의 상태와 §3을 갱신.

## 2. 기준 코드 — KEEP 자산 (공개 시그니처 변경 금지, 라인은 이동 가능 → 시그니처로 재확인)

| 자산 | 위치 | v2 역할 |
|---|---|---|
| NitpickerAgent | `src/agents/nitpicker.py:45` | `ztr review` 핵심 (Phase 2) |
| OutputQualityGate | `src/engine/quality_gate.py:48` | `ztr gate` (Phase 3) |
| PostMergeVerifier | `src/engine/post_merge_verifier.py:32` | `ztr verify --post-merge` (Phase 3) |
| CircuitBreaker | `src/engine/circuit_breaker.py:23` | 백엔드 장애 폴백 |
| IAgent + 자동 등록 | `src/agents/base.py:110`, `registry.py:25`, `discovery.py:24` | OCP 유지 |
| OllamaAgent | `src/agents/ollama.py:26` | 프리필터 보조 |
| config 스키마/로더 | `src/config/schema.py:142` (RoundtableConfig), `loader.py:21` | 바인딩 형식으로 진화 |
| ToastHook/HookManager | `src/engine/hooks.py:57,140` | Phase 5 알림 재사용 |
| SessionStore + 대시보드 | `src/engine/session_store.py:95`, `src/web/app.py` | **동결** — 축소 유지만 |

## 3. Phase 8 상세 (`run-phase resume 체인`) — ✅ 완료

> **능동 ztr 작업 없음.** Console-script packaging 후속은 2026-06-14 완료(`docs/handoff/ZTR_ENTRYPOINT_PACKAGING_REPORT.md`). Phase 9 후보(`--payload-file`/`--review-artifact`)는 dogfood 증거 발생 시 분리(계약 §8). **스위트/오케스트레이터 진행 SoT = `methodology/docs/PHASE_CYCLE_ORCHESTRATOR_DESIGN.md §10`** (현재 위치=dogfood). 아래는 Phase 8 종결 상세.

핸드오프(콜드스타트 SoT): `MultiAgent_Monorepo/docs/handoff/PHASE8_RESUME_HANDOFF.md` · 설계: `…/methodology/docs/PHASE_CYCLE_ORCHESTRATOR_DESIGN.md §6`.
핵심: 헤드리스 leg의 session id를 **JSON stdout**에서 캡처(stderr redaction 우회 — LESSON-019) → `--session-map`에 영속 → 다음 페이즈 resume 재주입. **spike S2 PASS(2026-06-13)로 잠금 해제.** 착수 = ztr를 모노레포 `runtimes/ztr/`로 흡수 후(AD-4/개정 A1).
구현 제약: leg는 `--output-format json`(claude)/`--json`(codex) 강제 · codex는 node PATH 필요(LESSON-020) · **resume 실패(만료·무효) 시 조용한 맥락 손실 금지 — 폴백+보고**.
DoD: unit + deterministic + live 1회(2-페이즈 resume, 벤더별). full 오케스트레이션 자동화는 NOT CLAIMED.

### Phase 8 착수 기록 (2026-06-13)
- AD-4/개정 A1에 따라 `D:\ZRT`를 모노레포 `runtimes/ztr/`로 subtree 흡수 후 구현 착수. 흡수 전 백업 bundle 생성: `D:\suite-backup-20260613-194642\ztr-main.bundle`, `D:\suite-backup-20260613-194642\monorepo-master.bundle`.
- 구현 방향: `src/engine/resume_chain.py`에 session-map 로드/저장, provider profile 기반 argv 변형, stdout JSON id 추출을 분리. `phase_relay.py`는 실행 전 변형·실패 시 정책 분기·성공 후 캡처만 호출한다.
- 정책 확정: `auto`로 map id를 사용한 resume 실패는 새 세션으로 폴백하고 맥락 손실 경고 및 `fallback_used=true`를 남긴다. 명시 session id 실패는 사용자 의도를 보존하기 위해 폴백하지 않는다.
- 단순화 확정: project.config 치환 전면 설계는 후속으로 두고, Phase 8은 CLI profile(`none`/`claude`/`codex`) 최소분으로 provider별 resume 변형을 명시 구성한다. resume id는 outer envelope model claim에 넣지 않고 step resume payload와 session-map에만 남긴다.
- 버전 traceability: Phase 8 live 검증은 Claude Code `2.1.176`, Codex CLI `0.130.0-alpha.5`로 수행했다. Codex는 WindowsApps alias가 Access denied였으므로 `C:\Users\user\AppData\Local\OpenAI\Codex\bin\codex.exe` 절대경로와 `--ignore-user-config -m gpt-5.5`로 검증했다.
- D7 config seam 기술부채: 현재 resume 변형은 `project.config` provider seam으로 이전되지 않았고 `--*-resume-profile {none,claude,codex}` CLI 플래그와 `resume_chain.py`의 provider별 argv 변형으로 고정되어 있다. 2벤더 Phase 8 범위에서는 의도적 최소화이나, provider 중립 config seam 이전은 후속 project.config 확장 단계까지 NOT CLAIMED로 둔다.
- NOT CLAIMED: full 다중페이즈 오케스트레이터, 외부 CLI id 필드의 장기 안정성, 실제 만료 id 서버 경로 재현.
- 설계 검토 leg 후속(2026-06-13): §7(명시-id resume 실패 BLOCKED 승격 + silent-new-session 차단) 반영(`74dcce6`). 전용 venv(`runtimes/ztr/.venv`, `pip install -e .[dev]`)로 e2e 편집가능 그림자(subprocess `python -m src`가 `D:\ZRT\src`로 해석되던 문제) 닫음. **invariants 모노레포 경로 버그 수정**: `git diff`(repo-root 상대)와 `git ls-files`(기본 cwd 상대)의 base를 `--full-name`으로 통일하고 `runtimes/ztr` 밖 변경은 필터링 → 서브트리 컨텍스트에서 `m2_finding_terms` BLOCKED 해소. 전용 venv full-run에서 `test_e2e_integration` 포함 **212 passed** 확인. circuit_breaker 타이밍 테스트는 **간헐 실패(flaky, 부하 민감 — 재실행 시 통과)** → 결정론화 후속 hygiene(NOT CLAIMED).

### Phase 8 구현 중 설계 세션으로 넘길 제안 (2026-06-13)
- **Windows 자동화 payload 규약 필요**: 긴 diff/파일 본문/리뷰 요청을 argv에 직접 싣는 방식은 Windows 명령줄 길이 한도와 quoting 규칙에 반복적으로 걸린다. `phase-cycle-orchestrator` 설계 세션에서 "작은 argv + 파일 기반 payload + 구조화 stdout"을 공통 규약으로 승격할 것을 제안한다.
- **provider adapter capability 표준화**: `project.config` 또는 후속 adapter schema에 `max_inline_prompt_chars`, `supports_stdin_prompt`, `supports_prompt_file`, `requires_json_output`, `known_argv_shape`, `windows_path_strategy` 같은 capability 필드를 둔다. 각 CLI의 stdin prompt 지원은 추정 금지, live smoke 후 `verified`로만 표시한다.
- **문자/포맷 규약 정규화**: Windows 콘솔 cp949, UTF-8 BOM, CRLF/LF, PowerShell JSON quoting 문제가 반복된다. 문서·프롬프트·캡처 파일은 UTF-8, 외부 입력 JSON은 UTF-8-SIG 허용, 자동 생성 로그/테스트 출력은 ASCII-safe 문자를 기본값으로 하는 스위트 공통 규약이 필요하다.
- **리뷰 leg 입력 전달 방식 변경 제안**: 대형 설계리뷰는 `claude -p "<huge prompt>"` 같은 inline argv가 아니라, 리뷰 artifact 파일을 만들고 짧은 prompt로 파일 경로와 검토 기준만 전달하는 방식으로 정규화한다. CLI가 파일 prompt를 직접 지원하지 않는 경우에도 adapter가 payload 파일을 만들고, 검증된 provider별 방식으로만 전달해야 한다.

### Console-script packaging 후속 완료 (2026-06-14)
- 원인: `pyproject.toml`의 package discovery가 setuptools 기본 src-layout으로 자동 추론되어 editable `.pth`가 `runtimes/ztr/src`를 가리켰고, repo root에서 `ztr.exe`가 패키지명 `src`를 import하지 못했다. 추가로 `ztr review`는 mypy cwd가 호출 cwd에 묶여 repo root에서 `src` 타깃을 찾지 못했다.
- 수정: `pyproject.toml`에 build backend와 `src` 패키지 discovery를 명시하고, static review의 ruff/git cwd와 mypy cwd를 분리했다. `cmd_review()`는 설치된 ztr runtime root를 mypy cwd로 넘긴다. 적용에는 전용 venv에서 editable 재설치가 필요하며, 이번 검증 전 `pip install -e .[dev]`를 재실행했다.
- 검증: 전용 venv(`runtimes/ztr/.venv`)에서 `pytest -q` 214 passed, `ruff check src tests` PASS, `mypy src` PASS, repo root cwd `ztr.exe --help` PASS, repo root cwd `ztr.exe review --changed --timeout 30` PASS.
- NOT CLAIMED: non-editable wheel/sdist 설치, repo root 밖 비-git cwd의 `review --changed`, `review --changed` mypy leg의 비-ztr Python diff 타입검증, ACP/오케스트레이터 backend 다형성.

### Phase 7 종결 기록 (2026-06-13)
- 시작 기준선: ruff PASS, mypy PASS, pytest `189 passed`, `ztr invariants` PASS, `ztr review --changed` PASS(`qwen2.5-coder:7b`). 깨끗한 워킹트리에서 `ztr verify --post-merge --changed`는 변경 대상 없음으로 BLOCKED 가능하므로 기준선 PASS로 claim하지 않는다.
- 구현: `review`, `verify --post-merge`, `run-phase`에 `--record` 옵션을 추가해 v2 Envelope verdict를 `SessionStore`에 기록. 기록 실패는 stderr warning으로 격리하고 stdout 단일 Envelope JSON과 명령 verdict를 오염시키지 않는다.
- 결정론적 E2E: 테스트에서 `review(CHANGES_REQUESTED) -> review(PASS) -> verify(PASS) -> invariants(PASS)`와 `gate(CHANGES_REQUESTED/PASS)` 체인을 단일 Envelope JSON 및 exit code 매핑으로 고정. **여기서 "결정론적"=LLM 호출 0회**(ollama는 config `agents` 비활성 분기로 미호출). 단 정적 툴체인(ruff+mypy)이 `sys.executable`에 설치돼 있다는 **환경 전제**가 있다 — 표준 `.venv`에선 GREEN(19 passed)이나, mypy 미설치 인터프리터로 돌리면 review가 BLOCKED로 떨어져 단언이 깨질 수 있음(이식성 메모).
- Live recorded relay 명령: `.venv\Scripts\python.exe -m src run-phase --record --prompt-file D:\ZRT\.ztr\phase7-e2e\phase7_run_phase_prompt.md --phase-id phase7-live-codex-recorded --timeout 180 --output-dir D:\ZRT\.ztr\run-phase --implementer-cmd <codex exec JSON array>`.
- Live recorded relay 결과: outer envelope `PASS`, exit code `0`, backend `phase-relay`, model `external-cli`, step child exit `0`, stdout preview `ZRT_PHASE7_E2E_OK`. 캡처 디렉터리: `D:\ZRT\.ztr\run-phase\20260613-143645-phase7-live-codex-recorded`; outer envelope: `D:\ZRT\.ztr\phase7-e2e\outer_envelope_codex_recorded.json`.
- SessionStore/UI 관측: `.ztr/sessions.db`에 session `#1` 생성(`task=ztr run-phase phase7-live-codex-recorded`, `final_verdict=PASS`, `rounds=1`). `python -m src web` 로컬 서버에서 `/`와 `/session/1` 모두 `PASS` 및 `phase7-live-codex-recorded` 표시 확인. HTML assertion 산출물: `D:\ZRT\.ztr\phase7-e2e\ui_index.html`, `D:\ZRT\.ztr\phase7-e2e\ui_session_detail.html`.
- Live 2-leg relay 시도: `codex exec` implementer + `claude -p --model sonnet` reviewer leg로 `ztr run-phase` 실행. outer envelope `PASS`, exit code `0`, completed `2/2`, 캡처 디렉터리 `D:\ZRT\.ztr\run-phase\20260613-143805-phase7-live-codex-claude-attempt`. 이는 headless CLI 릴레이 관통 증거이며, Sonnet 리뷰 품질 평가는 NOT CLAIMED.
- PASS로 claim: CLI 체인 E2E의 결정론적 exit routing, live `run-phase` leg 순차 실행, stdin EOF, child exit code→verdict routing, raw stdin/stdout/stderr/step envelope 캡처, outer 단일 Envelope JSON, `--record` SessionStore 기록, 대시보드 목록/상세 v2 PASS 관측.
- NOT CLAIMED: 자동 소스 편집, 자동 merge, 자동 rollback, 리뷰 자연어 의미 판단, Sonnet 리뷰 품질, 실제 프로젝트 개선의 의미론적 성공, 장기 운영 품질.
- **독립 Reviewer leg (Planner Opus 집행)**: PASS — blocker 0/major 0/minor 2. R5 Boundaries 깨끗(--record는 verdict enum·rounds·redact stderr·사실 metadata만 기록, LLM 의미 파싱 없음), stdout 단일 Envelope 계약 불변(opt-in·실패 격리), 대시보드 관측 전용(Jinja autoescape, XSS 없음). minor 2건 반영: ① deterministic의 toolchain 전제 명시(위), ② invariants `_is_text_invariant_target`의 `tests/` 제외 의도 주석 고정.
- 후속 프롬프트: Phase 8은 아직 작성하지 않음. 새 기능보다 운영 반복 데이터가 먼저 필요하므로 후속 페이즈는 별도 Planner 결정 후 작성한다.

### Phase 3~6 독립 Reviewer leg (2026-06-13, Planner Opus 집행 — 사후 보강)
- 배경: Phase 3·4·5·6은 토큰 제약으로 Codex 단독 진행 → 독립 설계 Reviewer leg(Planner 소유, 2-leg 하드게이트의 A leg)가 누락된 채 커밋됨. Planner가 사후 집행해 공백을 메움.
- **R5 Boundaries(최우선): 다섯 명령(review/gate/verify/invariants/run-phase) 전부 PASS** — verdict가 LLM 자연어에서 새어드는 경로 없음. ollama `review_text`·relay child stdout은 payload 동봉 전용, verdict 무관. 이 프로젝트 핵심 방어선 견고.
- 기계 게이트 재실측: ruff PASS, mypy(strict, 3.12) PASS, pytest 189 passed, `ztr invariants` PASS.
- 잡은 major 2건 → 본 세션 반영: ① invariants ROADMAP 검사 화석화(§0 일반화 결정·코드 수정 완료) ② Toast·타임박스 침묵 누락(§0 descope 기록 완료). minor 다수는 후속 정리 대상(redaction 일관성, m2_terms needle 느슨, gate 입력의 v1 verdict 어휘 등)으로 LESSON/후속 페이즈 이관.

### Phase 6 종결 기록 (2026-06-13)
- 시작 기준선: ruff PASS, mypy PASS, pytest `189 passed`, `ztr invariants` PASS, `ztr review --changed` PASS(`qwen2.5-coder:7b`). 깨끗한 워킹트리에서 `ztr verify --post-merge --changed`는 `verify 대상이 없습니다`로 BLOCKED — 변경 대상이 없는 상태에서는 PASS를 claim하지 않는다.
- Dogfood 1차 live leg: `claude -p --model sonnet` 실행·캡처는 성공했으나 Claude CLI 세션 한도(`You've hit your session limit`)로 child exit `1`, relay verdict `CHANGES_REQUESTED`. Sonnet live PASS와 Sonnet 리뷰 품질은 NOT CLAIMED.
- Dogfood 2차 live leg: `codex exec --cd D:\ZRT --sandbox read-only --ephemeral --color never -`를 `ztr run-phase` implementer leg로 실행. outer envelope `PASS`, exit code `0`, model `external-cli`, step child exit `0`, stdout preview `ZRT_RUN_PHASE_DOGFOOD_OK`.
- 캡처 디렉터리: `D:\ZRT\.ztr\run-phase\20260613-103124-phase6-dogfood-codex-after-fix`. 확인 파일: `01-implementer.stdin.txt`, `01-implementer.stdout.txt`, `01-implementer.stderr.txt`, `01-implementer.envelope.json`. outer envelope: `D:\ZRT\.ztr\run-phase-dogfood\outer_envelope_codex_after_fix.json`.
- PASS로 claim: CLI leg 순차 실행(단일 leg), stdin EOF, child exit code→verdict routing, raw stdin/stdout/stderr/step envelope 캡처, 단일 outer Envelope JSON 출력, read-only live Codex leg 관통.
- NOT CLAIMED: 자동 소스 편집, 자동 merge, 자동 rollback, 리뷰 자연어 의미 판단, Sonnet 리뷰 품질, 실제 프로젝트 개선의 의미론적 성공.
- Dogfood 반영: `run-phase` outer envelope의 `model` 필드를 특정 역할 모델(`sonnet`)로 claim하지 않고 `external-cli`로 중립화. 실제 CLI/모델은 step `command`와 캡처 stderr에 기록한다. 런타임 캡처 `.ztr/run-phase/`, `.ztr/run-phase-dogfood/`는 git 추적 제외.

### Phase 5 종결 기록 (2026-06-13)
- Implementer: `src/engine/phase_relay.py` 신설, `ztr run-phase` CLI 배선. fake CLI 기반으로 순서 실행, stdin EOF, timeout kill, child exit code 라우팅, raw stdin/stdout/stderr/envelope 캡처를 고정.
- 출력 계약: 바깥 stdout은 단일 Envelope JSON. envelope.stdout은 `{"phase": {...}, "steps": [...], "summary": {...}}` JSON 문자열이며, step 결과는 경로·exit code·timeout 여부만 포함한다. 리뷰 자연어 의미 판단은 하지 않는다.
- exit routing: child `0 -> PASS`, `1 -> CHANGES_REQUESTED`, `2 -> BLOCKED`, timeout `124 -> BLOCKED`, 실행 파일 부재/구조 실패 `70 -> BLOCKED`.
- **descope(§0 기록, 침묵 누락 아님)**: ① 타임박스(L1=3회 STOP) — relay는 fail-fast 1패스라 자동 수정 루프 미도입(v1 함정 회피). ② Toast 알림 — 헤드리스 relay에 YAGNI. 휴먼 게이트 3곳은 코드가 아니라 outer 세션 휴먼 행위(진단 §6 2루프 분리)로 설계상 정상.
- 검증 목표: `python -m ruff check src tests` PASS, `.venv\Scripts\python.exe -m mypy src` PASS, `python -m pytest -q` GREEN, `python -m src run-phase ...` deterministic PASS/CR/BLOCKED/timeout 확인, `python -m src review --changed` PASS.
- NOT CLAIMED: full E2E 자동 관통, Sonnet 리뷰 탐지 품질, 실제 프로젝트 dogfood 결과.

### Phase 4 종결 기록 (2026-06-13)
- Implementer: `src/engine/invariants.py` 신설, `ztr invariants` CLI 배선, LESSON append-only, ROADMAP phase 기록, 다음 phase prompt 존재, M2 finding 필드명, NOT CLAIMED 표기, runner envelope 배선의 파일시스템 사실 검사 구현.
- 결과 payload: `{"checks": [...], "summary": {...}}`를 envelope.stdout JSON 문자열로 고정. invariant issue는 M2 5-field 형식으로 출력.
- 리뷰 반영 예정 기준: 의미론적 설계 품질 평가는 하지 않고, 파일·문자열·git 변경 사실만 검사.
- 검증 목표: `python -m ruff check src tests` PASS, `python -m mypy src` PASS, `python -m pytest -q` GREEN, `python -m src invariants` PASS, `python -m src review --changed` PASS.
- NOT CLAIMED: MM 캐논(`D:\MultiAgent_Methodology`)과 양방향 동기화, `--handoff-line`, full E2E.

### Phase 3 종결 기록 (2026-06-13)
- Implementer: `ztr gate <result-file>`와 `ztr verify --post-merge` 배선. 두 명령 모두 stdout 단일 Envelope JSON 계약 준수.
- `PostMergeVerifier.verify()` 공개 시그니처 유지. ruff/mypy 실행 불능, timeout, 진단 실패를 구분하도록 보강.
- 리뷰 반영: critic finding M2 5-field 누락은 `CHANGES_REQUESTED`, ruff/mypy stderr-only 실행 불능은 `BLOCKED`, `--timeout`은 ruff/mypy 모두 적용, subprocess one-envelope smoke 추가.
- 새 교훈: LESSON-017 Windows PowerShell UTF8 BOM 입력 대응.
- 검증: `python -m ruff check src tests` PASS, `python -m mypy src` PASS, `python -m pytest -q` 162 passed, `python -m src gate ...` PASS, `python -m src verify --post-merge --changed` PASS, `python -m src review --changed` PASS.
- NOT CLAIMED: `gate`가 리뷰 의미를 평가하는 것, `verify`가 소스 수정·rollback을 수행하는 것, full E2E.

### Phase 2 종결 기록 (2026-06-13)
- Implementer: 외부 Nitpicker Daemon(`jemmin`) 의존 제거, `src/engine/static_review.py` 신설, `ztr review --changed <paths...>` 배선, M2 finding 포맷, envelope 첫 stdout 배선.
- 모델: `roles.mechanical` 및 `ollama-local`을 `qwen2.5-coder:7b`로 고정.
- 리뷰 반영: ruff/mypy nonzero + 진단 0건은 PASS가 아니라 BLOCKED, Ollama optional leg 실패는 deterministic verdict 오염 없이 `not_claimed=["ollama-review"]`.
- 검증: `python -m ruff check src tests` PASS, `python -m mypy src` PASS, `python -m pytest -q` 148 passed, `python -m src review --changed` PASS 및 Ollama live 응답 확인.
- NOT CLAIMED: Ollama 응답의 의미 품질, full E2E.

### Phase 1 종결 기록 (2026-06-13)
- Implementer(Codex 세션): v2 브랜치, DROP 9모듈+전용 테스트 5종 명시 삭제, 혼재 테스트 salvage, envelope/roles 골격, DESIGN.md v3.0. 커밋 0b0a050·010d0c8·a90e8ff·460dba7.
- 후속 보강(설계 리뷰 반영): envelope status/exit_code 쌍 검증(124/70은 BLOCKED와만), roles 키 allow-list(MM 역할명), `invoke --role`(기본 manager). mypy 타깃은 3.13 복구 시도 후 외부 리뷰로 **3.12 재확정**(§0).
- **리뷰 게이트 (2-leg)**: A. 설계 Reviewer(독립 서브에이전트) PASS + 외부(Codex) 리뷰 반영 / B. 기계: pytest **133 passed / 3 skipped**, ruff PASS, `mypy src`(strict, 3.12) PASS — 재확정 후 재실측. `nitpicker-prefilter` HEALTHY(`D:\Nitpicker`), `ollama-local` HEALTHY, live 응답 확인(`ZRT_LOCAL_OK`).
- **NOT CLAIMED**: full E2E(validation_plan 참조). ollama 리뷰 의미 품질(보조 역할만 claim, R4).

## 4. 후속 페이즈 예고 (골자 — 각 페이즈 진입 시 Planner가 프롬프트 작성)

- **Phase 2 (ztr review)**: 완료. NitpickerAgent를 내부 static review 어댑터로 축소하고 `ztr review --changed` 서브커맨드 배선. finding = `severity / finding / evidence_or_repro / impact / recommendation`(M2), 미실행 검사는 `not_claimed`(M3).
- **Phase 3 (gate+verify)**: 완료. H4 `OutputQualityGate`를 `ztr gate`로, H6 `PostMergeVerifier`를 `ztr verify --post-merge`로 배선. stdout 단일 Envelope 계약 유지.
- **Phase 4 (invariants)**: MM 캐논(`D:\MultiAgent_Methodology`)과 인바리언트 목록 협의(A3) — HANDOFF 갱신, lessons append-only, finding 포맷, NOT CLAIMED 표기. 파일시스템 사실 검사만(의미 해석 금지). DoD: 위반 검출 시연.
- **Phase 5 (run-phase)**: 진입 게이트 = spike S1 완전 통과(§0 결정 로그). 헤드리스 구동(`claude -p/--resume`, `codex exec`), verdict enum 라우팅, 휴먼 게이트 3곳, Toast, 타임박스(L1=동일축 3회→STOP). DoD: deterministic 루프(가짜 CLI 스텁) + live 1회(Sonnet 리뷰).
- **Phase 6 (dogfood)**: 완료. live Codex leg로 relay mechanics를 dogfood하고 PASS/NOT CLAIMED 경계를 재확정. full E2E 자동 편집·머지와 실제 개선 루프는 NOT CLAIMED.

- **Phase 9 (relay 프로세스 트리 종료)**: **P0~P3 완료 — 네 호출부 전부 배선. RED 8/8 → GREEN. 남은 것은 POSIX 실측·실제 codex 계층 관측(NOT CLAIMED).** 입력 게이트 3라운드 종료(v4, R3 APPROVED). P0 실측: baseline 이 4개 중 3개 시나리오에서 고아+행, `taskkill /T` 는 중간 부모 선종료에서 rc=128 로 실패, **Job Object 만 전 시나리오 통과**. 타임아웃 시 `proc.kill()`이 직계만 죽여 **고아가 남고**(D1), kill 뒤 `communicate()`에 상한이 없어 손자가 파이프를 쥐면 **무한 대기**(D2)한다. 둘 다 **실측 재현됨**(손자 생존 + 30s 초과). 부착점 4곳 — `phase_relay.py:605` · `static_review.py:262` · acp `orch_drivers.py:101` · `orch_relay_driver.py:79`(D2만 조치됨). ⚠ **P0 스파이크로 DEC-1(트리 킬 수단)을 확정하기 전에는 구현 착수 금지.** 두 라운드 연속으로 메커니즘 설명 오류가 나왔다 — R1: 프로세스 그룹↔Job Object 혼동 · R2: Windows 재양육 오설명 + PID 불변 자기모순. v3 는 단정을 줄이고 **P0 10칸 실험 행렬**로 옮겼다. 계획 정본 = `methodology/docs/discovery/ztr-relay-process-tree-kill-20260821/PLAN.md`.

## 끝. 불변 원칙 (전 페이즈 공통 — 모든 프롬프트의 [불변 원칙]에 주입)

1. **Boundaries (R5 — 리뷰 자동 BLOCKED 사유)**: 코드가 파싱하는 LLM 출력은 **verdict enum + exit code뿐**. 리뷰 *내용* 해석·설계 판단·수락/거부·소스 편집·머지는 코드가 하지 않는다 (design.md Boundaries 표 전문 참조).
2. **C3 — 침묵 금지**: DROP은 별도 커밋으로 명시 삭제. 폴백 사용 시 envelope `fallback_used`에 명시.
3. **M3 — PASS vs NOT CLAIMED**: 실행 못 한 검사는 통과가 아니라 미주장. 단, 리뷰 2-leg는 NOT CLAIMED 불가(서브에이전트로 집행 가능).
4. **리뷰 2-leg 하드 게이트**: 설계 Reviewer(Codex 세션, 기존 방식) + 기계(Nitpicker/ztr) 모두 PASS 전 페이즈 미완료, 커밋 보류. 구현 리뷰 leg는 claude CLI 로그인 완료 후 Sonnet 헤드리스 수동 트리거, 그 전엔 Claude Agent 서브에이전트로 대체(생략 금지).
5. **Timebox**: L1 = 같은 축 3회 실패 시 STOP + 실패 로그·원인·다음 접근 보고.
6. **Windows/인코딩**: 파일 I/O `encoding='utf-8'` 명시(CLAUDE.md). subprocess는 3단 방어(LESSON-001).
7. **커밋**: 한국어 메시지, feat/fix/refactor/docs 프리픽스. 커밋은 사용자 확인("커밋해") 후.
