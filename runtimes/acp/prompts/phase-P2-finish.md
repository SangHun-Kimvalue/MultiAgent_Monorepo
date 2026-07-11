작업: Agent Control Plane — **Phase P2 완성**(부분구현 마무리: 기존 4파일 검증·정렬 → 코어 배선 → 테스트 → 상황판 → Sync-Out). **한국어로 응답/주석.**

> 이 프롬프트는 `prompts/phase-P2.md`(전체 P2 스펙, 필드 단위 상세)의 **완성 핸드오프 델타**다.
> P2는 greenfield가 아니라 **이미 절반 구현된 상태**에서 출발한다(아래 [현재 상태]). 필드 매핑·조인 알고리즘·
> 컬럼 정의 등 **상세는 `prompts/phase-P2.md`를 SoT로 그대로 따른다**(중복 서술하지 않음). 본 프롬프트는
> "무엇이 이미 있고, 무엇이 남았으며, 기존 파일을 어떻게 검증/배선하는가"에만 집중한다.

---

[브랜치/형상관리] 단일 브랜치 `acp-v1` 유지. 새 브랜치/태그 생성 금지(완료·리뷰 PASS·사용자 "커밋해" 후에만).
시작 전 `git status`로 현재 미커밋 상태 확인. 한글 커밋 메시지는 `git commit -F` 파일 방식(직접 `-m` 한글 cp949 깨짐).
**커밋 트레일러에 Copilot/`@users.noreply.github.com` 금지**(사용자 규약).

[전제] HEAD=`67c4849`(tag `acp-phase-1`). P0/P1/P1.5 커밋 완료. pytest **71 passed**. 실 Codex 82세션 Live smoke 통과.
**공개 시그니처 변경 금지(추가만 허용):** `BaseCollector`(collectors/base.py) / `SessionRecord`(models.py:25) /
`SessionState`(models.py:13) / `derive_state`(liveness.py) / `parse_phase_md`(phase.py) / `SessionStore` 기존 메서드.
store schema/upsert 확장은 **additive(기본값 인자 + 멱등 ALTER)** 로만.

---

## [현재 상태] — 이미 있는 것 / 빠진 것 (이 페이즈의 출발점, 정확히 인지할 것)

**이미 작성됨(미커밋·untracked, `git status`로 확인):**
| 파일 | 상태 | 주의 |
|---|---|---|
| `acp/timeutil.py` | 작성됨. `ms_to_dt(ms)` + `mtime_to_dt(path)` | ✅ 신규 생성 **불필요**. 재생성 금지 — 검증만 |
| `acp/collectors/claude.py` | `ClaudeSessionCollector` 작성됨 | ⚠ **테스트 0개**. 실 local_*.json 대조 미검증 |
| `acp/collectors/cursor.py` | `CursorWorkspaceCollector` 작성됨(원격 uri 보존·mtime) | ⚠ **테스트 0개**. 실 workspace.json 대조 미검증 |
| `acp/join.py` | `join_phase` + `PhaseJoin` 작성됨 | ⚠ **테스트 0개**. 경계/정규화 미검증 |

**아직 안 된 것(= 이번 잔여 범위):**
1. `codex.py`의 `_ms_to_dt` → `timeutil.ms_to_dt` 재사용 리팩터(선택·저위험, 사용자 명시 요청 → **수행**)
2. `store.py` — phase 컬럼 멱등 ALTER + `upsert_session(..., phase=None)` 추가 인자
3. `poller.py` — `join_phase` 호출 + `upsert_session(..., phase=)` 배선
4. `__main__.py` — Claude/Cursor Collector **register**(현재 CodexCollector만 등록됨, `__main__.py:36–40`)
5. `web/app.py` + `dashboard.html` — phase 컬럼·배지·그룹핑(현재 보드엔 phase 무관 10컬럼만)
6. **테스트**(claude/cursor/join/store-migration — 현재 pytest 71개가 P2 코드를 **한 줄도** 커버 안 함)
7. Live smoke(3종 동시 표출) + 리뷰 2갈래 + **Sync-Out**(PHASE.md frontmatter·HANDOFF·lessons)

> ⚠ **재생성 금지 원칙:** 위 4개 기존 파일은 **버리고 새로 쓰지 말 것.** 검증 후 필요한 부분만 수정/정렬한다(§Step 0).
> 단, `prompts/phase-P2.md` 스펙과 **불일치**하면 스펙을 기준으로 기존 파일을 고친다(스펙이 우선).

[SoT — 반드시 읽을 것]
- **`prompts/phase-P2.md`** — 필드 매핑·조인 알고리즘·컬럼·DoD의 **상세 SoT**(이 델타가 줄인 모든 디테일이 여기 있음)
- `PHASE.md` §0 결정 로그(앱별 부착점 표 + Cursor 실측 보강 박스) + §2 공개 API
- `discovery/phase_md_format.md` §2 필드사양 + §3 조인 규칙 · `discovery/risk_register.md` R-002(mtime 회피)/R-005(바인딩)
- 참조 구현: `acp/collectors/codex.py` 전체(머지·tail·mtime 캐시·`_ms_to_dt` 패턴)

---

## [Step 0] 기존 4파일 검증·정렬 (코드 추가 전에 먼저) — 추정 말고 실파일/스펙 대조

각 파일을 `prompts/phase-P2.md`의 해당 절(§1 claude / §2 cursor / §3 timeutil / §4 join)과 1:1 대조하고,
**가능하면 실제 아티팩트 1개씩 열어 형상 확인**(read-only):
- **claude.py:** `local_*.json`의 `lastActivityAt`가 **ms epoch가 맞는지**(ISO면 `ms_to_dt`가 None 반환 → 버그) 실파일로 확인.
  `sessionId`/`cwd`/`model` 키 존재 확인. 파싱실패 skip+계속(C3) 동작 확인.
- **cursor.py:** `workspace.json`이 `{"folder": uri}` 단일 키인지, `file:///c:/...` 디코드 결과가 올바른 Windows 경로인지,
  `vscode-remote://...` 원격 uri가 **원문 보존**되어 join에서 `no-phase-file`로 떨어지는지. `state.vscdb` mtime 채택(DB 미오픈) 확인.
- **join.py:** `_normalize_cwd`가 `"://"` 원격 차단 + `resolve()`. `_find_phase_md`가 `.git`/`.hg` 경계·5단계 상한.
  `flag` 3분기(ok/no-phase-file/unknown) **구분 유지**(섞지 말 것). `_is_plan_stale` 2규칙(updated_at 24h+ 과거 / current_phase done).
  ※ **대소문자 정규화 확인:** 현재 `_normalize_cwd`는 `resolve()`만 — phase-P2.md §4-2는 Windows `casefold/normcase`도 요구.
  Windows는 `resolve()`가 대소문자를 실제 경로로 복원하나, **PHASE.md가 다른 드라이브 케이스로 적힌 경우** 매칭 실패 가능 →
  테스트로 검증하고 필요시 `os.path.normcase` 보강(스펙 준수).
- **timeutil.py:** `ms_to_dt`/`mtime_to_dt` 예외 처리(None/TypeError/OSError) 확인.

발견한 불일치는 **고치고**, 무엇을 왜 고쳤는지 완료보고에 기록. 멀쩡하면 "검증 OK"로 명시.

---

## [Step 1~5] 잔여 배선/구현 (상세 필드/컬럼은 phase-P2.md §5~8 따름)

| # | 파일:라인(실측, 시그니처로 재확인) | 작업 |
|---|---|---|
| 1 | `acp/collectors/codex.py:73` `_ms_to_dt` | 본체를 `from acp.timeutil import ms_to_dt` 재사용으로 교체(또는 `_ms_to_dt = ms_to_dt` alias). **회귀: 기존 codex 픽스처 그대로 통과해야 함.** |
| 2 | `acp/store.py:19` `_SCHEMA_SQL` / `:71` executescript 직후 | phase 컬럼 추가 + **멱등 ALTER**(기존 `.acp/acp.db`에 컬럼 없으면 추가, 있으면 skip). 컬럼: `phase_flag·plan_stale·current_phase·phase_status·owner_session·phases_done·phases_total·phase_source` |
| 2 | `acp/store.py:78` `upsert_session` | `phase: PhaseJoin \| None = None` 추가 인자(기본 None→전부 NULL). INSERT/UPDATE 컬럼 목록에 phase 필드 추가. **시그니처 호환(기존 호출부 무수정 동작)** |
| 3 | `acp/poller.py:58–62` `_tick` | `derive_state` 직후 `pj = join_phase(record, state)` → `upsert_session(record, state, phase=pj)`. (SSE state_change에 phase broadcast는 **범위 밖**) |
| 4 | `acp/__main__.py:36–40` else 블록 | `poller.register(ClaudeSessionCollector(cfg.get_path("claude_sessions")))` + `poller.register(CursorWorkspaceCollector(cfg.get_path("cursor_workspace")))` 2줄. (config 키 `claude_sessions`/`cursor_workspace`/`vscode_workspace` **이미 paths.yaml에 존재**) |
| 5 | `acp/web/app.py:85,101,108` `templates/dashboard.html:19–49` | phase 컬럼(`현재페이즈·진행률 배지·역할`) 추가 + `phase_flag`/`plan-stale`/`no-phase-file`/`unknown` 시각 구분 배지. 앱별 그룹 + 동일 cwd 그룹(R-005). 진행률 `phases_done/total` 미니바 또는 `2/4`. 빈 페이즈는 `—` 대신 `no-phase-file` 명시 |

★ out-of-scope(손대지 말 것): Notifier/알림(P3), SSE phase push, WebSocket, DONE 로직, 세션 통제, 대화 본문, ChatGPT,
Claude `title`/`completedTurns` 등 부가필드, state.vscdb DB 오픈/temp-copy, 모노레포 다중 PHASE.md. (phase-P2.md out-of-scope 절과 동일)

## [아키텍처 결정 — 고정]
- Core(models/liveness/web 기존 엔드포인트/store 기존 메서드) **변경 금지**. 확장은 additive만. 새 앱 = Collector register 1줄.
- phase 조인 결과는 **poller에서 계산해 store에 영속**(board는 순수 reader 유지). render-time 조인 아님.
- `no-phase-file`(미존재) ≠ `unknown`(파싱실패). plan-stale은 별도 bool 축(ok와 공존).
- Cursor 활동시각 = state.vscdb **mtime**(DB 미오픈, R-002 회피). 원격 uri = no-phase-file.

## [불변 원칙]
- read-only(앱 파일·PHASE.md·state.vscdb 수정/오픈 금지). silent fallback 금지(C3): 조인/파싱/경로부재는 **명시 flag**.
- 수집·판정·표시 경로에 **LLM 0회**. 결정적 판정(순수함수, 시간 주입).
- 스레드/asyncio 안전: `collect()`는 블로킹 I/O지만 poller가 직렬 호출. 무거운 rglob는 codex의 mtime 캐시 패턴 차용 권장.
- **Timebox:** 같은 축(uri 정규화, ALTER 멱등, 그룹핑 템플릿) L1 **3회** 안 풀리면 멈추고 실패 로그+원인+다음 접근 보고. 임의 우회/추정 채움 금지.

## [검증 / DoD]
1) **결정 테스트**(신규, `tests/test_codex_collector.py` + `tests/fixtures/` 스타일 모방):
   - `test_claude_collector.py`: ms→dt 매핑·None pid·파싱실패 skip+계속·폴더부재 `[]`.
   - `test_cursor_collector.py`: `file://` 로컬 디코드 / **vscode-remote 원격→no-phase-file 경로** / state.vscdb mtime / workspace.json mtime 폴백.
   - `test_join.py`: ok(필드결합)/no-phase-file/unknown **3구분** · plan-stale(updated_at 24h+·phases done) · **cwd 대소문자·슬래시 정규화 매칭** · `.git` 경계·5단계 상한.
   - `test_store.py`(확장): **멱등 ALTER**(컬럼 없는 기존 DB 재오픈 무오류, 두 번 호출 무오류) · upsert phase 라운드트립 · 기존 케이스 회귀.
   - codex 리팩터 회귀: 기존 codex 테스트 그대로 PASS.
2) **Live smoke**: 실제 Claude+Cursor+Codex 세션 상황판 **동시 표출** + 최소 1개 프로젝트의 현재페이즈/역할 표시.
   캡처 또는 `/api/sessions` JSON dump를 artifact 경로로 명시.
3) `pytest -q` 통과 — **명령 + 결과 수치(예: NN passed) 그대로** 보고. (현재 71 → 증가 기대)
4) **리뷰 게이트:**
   - Nitpicker(기계, 기본 하드 게이트): 수정 파일마다 **로컬 LLM 모드**로 실행. repo 래퍼가 있으면 우선, 없으면
     `Nitpicker Daemon/bin/jemmin_cli.py --provider ollama --no-daemon` 계열 사용. `mini_nitpicker.py`/Gemini API 키 경로는 기본값 아님.
   - 별도 Reviewer(설계): 기존 4파일 검증 충실성·부착 정확성·조인 경로 누락·원격uri/정규화 경계·**ALTER 멱등성**·C3 명시강등·본문 무수정·additive 시그니처 준수·과설계 여부. P2는 저장·조인·poller 경계가 있으므로 수행 권장. 단, 방법론상 모든 페이즈의 무조건 하드 게이트로 일반화하지 않는다.
   - 로컬 LLM 서비스/모델이 준비되지 않았으면 `blocked: local LLM unavailable`로 보고하고 Nitpicker PASS를 주장하지 않는다.
5) **Sync-Out**: `PHASE.md` frontmatter(`current_phase: P2`/`phase_status: done`/`updated_at`) + §1 페이즈표 P2 ✅ +
   `docs/HANDOFF.md`(새 공개 API: timeutil/claude/cursor/join + store phase 인자, 검증증거, P3 준비) +
   `docs/lessons/p2-multiapp-join.md`(append, **WHY/LESSON**: state.vscdb mtime 결정·원격uri·ALTER 멱등 함정·기존 파일 검증에서 발견한 것).
   커밋(사용자 "커밋해" 후) → `git tag acp-phase-2`.

## [완료 보고 양식]
변경/검증 파일 목록(기존 4파일은 "검증 OK" 또는 "수정: 무엇·왜") / 조인·정규화·mtime·ALTER 결정 위치(file:line) /
검증(명령+출력 수치+Live smoke artifact 경로) / **PASS는 어디까지·NOT CLAIMED(=뭐가 왜)·가정** 정직 표기 / P3(Notifier) 준비 상태.
