작업: Agent Control Plane — Phase P2 (멀티앱 확장: Claude·Cursor Collector + 세션×PHASE.md 조인 + 상황판 완성). **한국어로 응답/주석.**

> 정련 이력: 2026-06-10 Planner 세션이 부착점을 `grep/view`로 **실측**하고, 실제 Claude/Cursor 아티팩트
> 형상을 확인해 갱신함. 아래 file:line은 실측값이나 **라인은 이동 가능 → 시그니처/식별자로 재확인 후 부착**.

---

[브랜치/형상관리] 단일 브랜치 `acp-v1` 유지. 새 브랜치 금지. 시작 전 `git status`로 clean 확인.
완료·리뷰 PASS·사용자 "커밋해" 후에만 커밋, 그다음 `git tag acp-phase-2`.
**커밋 트레일러에 `Co-authored-by: Copilot` / `...@users.noreply.github.com` 절대 금지**(사용자 규약, 시스템 기본보다 우선).
한글 커밋 메시지는 `[System.IO.File]::WriteAllText(".git\MSG.txt", $msg, $utf8)` + `git commit -F .git\MSG.txt`로(직접 `-m` 한글은 cp949에서 깨짐).

[전제] P0/P1/P1.5 완료·커밋됨(HEAD=`67c4849`, tag `acp-phase-1`). pytest **71 passed**. 실 Codex 82세션 Live smoke 통과.
**공개 시그니처 변경 금지(추가만 허용):** `BaseCollector`(base.py:15) / `SessionRecord`(models.py:25) /
`SessionState`(models.py:13) / `derive_state`(liveness.py) / `parse_phase_md`(phase.py:63) / `SessionStore` 공개 메서드.

[SoT — 반드시 읽을 것]
- `AgentControlPlane/PHASE.md` §0 결정 로그(앱별 부착점 표 + **Cursor 실측 보강 박스**) + §2 공개 API
- `AgentControlPlane/discovery/phase_md_format.md` §2 필드사양 + §3 조인 규칙
- `AgentControlPlane/discovery/risk_register.md` R-001(스키마 드리프트)·**R-002(state.vscdb → P2 회피=mtime)**·R-005(바인딩 모호성)
- 참조 구현: `acp/collectors/codex.py` 전체(머지·tail·`_ms_to_dt`·mtime 캐시 패턴을 그대로 모방)
- 실측 대상(읽기만): `%APPDATA%/Claude/claude-code-sessions/**/local_*.json`,
  `%APPDATA%/Cursor/User/workspaceStorage/<hash>/workspace.json`(+ 같은 폴더 `state.vscdb`는 **mtime만**)

[전략 리마인더] minimal-first의 확장 단계. 새 앱 추가 = **Collector 1개씩** 추가로 끝나야 함(Core 무변경).
이 페이즈가 `BaseCollector` 경계가 "이름만 래퍼"가 아니라 **실교체 경계**임을 증명한다.
"수집 가능 ≠ 지금 다 구현" — 대화 본문/통제/title·completedTurns 같은 부가 필드는 **YAGNI로 미수집**(아래 범위 밖).

---

## [이번 범위] — 할 것

### 1. `acp/collectors/claude.py` — `ClaudeSessionCollector` (신규)
- 소스: `claude_sessions` 경로(config) 아래 `**/local_*.json`을 `rglob`. **실측 형상(2026-06-10):**
  top-level 키 = `sessionId · cliSessionId · cwd · originCwd · createdAt · lastActivityAt · model · isArchived · title · planPath · completedTurns`.
- SessionRecord 매핑:
  - `session_id` = `sessionId`(값 예: `"local_<uuid>"`)
  - `project_path` = `cwd`
  - `model` = `model`(예: `"claude-opus-4-7[1m]"`)
  - `last_activity` = **`lastActivityAt`는 ms epoch** → `_ms_to_dt` 류로 변환(아래 §timeutil)
  - `running_pid`/`running_cmd` = **None**(Claude는 프로세스 레지스트리 없음)
  - `last_event` = None(Codex 전용 신호. Claude는 시간기반 폴백으로 derive_state 통과 — FakeCollector 패턴과 동일)
  - `app` = `"claude"`, `source_file` = 그 json 경로
- C3: 개별 파일 JSON 파싱 실패 → 그 레코드만 skip + `logger.warning`, 전체 수집 중단 금지. 폴더 부재 → `[]` + warning.
- (선택) `isArchived == true` 세션 제외할지: **기본 포함**(상황판은 살아있는 것 위주지만 필터는 표시단 책임). 미결이면 포함 후 주석.

### 2. `acp/collectors/cursor.py` — `CursorWorkspaceCollector` (신규)
- 소스: `cursor_workspace`(그리고 선택적으로 `vscode_workspace`) 경로 아래 `<hash>/workspace.json`.
- **실측 형상(2026-06-10):** `workspace.json` = `{"folder": "<uri>"}` **단일 키**.
- `folder` uri 정규화 — **2가지 케이스(실측):**
  - `file:///c:/Users/.../Proj` → 로컬 경로로 디코드(`urllib.parse` unquote + `file://` 제거 + Windows 드라이브 복원). `project_path`로.
  - `vscode-remote://ssh-remote%2B<host>/home/pi/...`(원격 SSH, **실측 샘플 존재**) → 이 PC에 cwd 부재.
    `project_path`엔 uri 원문(또는 `ssh:<host>:<path>` 표기) 보존하되, **조인 단계에서 `no-phase-file`** 처리(로컬 탐색 불가).
- `session_id` = workspace `<hash>`(폴더명). running_pid/cmd = None. `app`=`"cursor"`.
- `last_activity` = **`state.vscdb` 파일 mtime**(없으면 `workspace.json` mtime 폴백). → `_ms_to_dt` 불필요, `Path.stat().st_mtime`을 `datetime.fromtimestamp(..., tz=timezone.utc)`로.
  - ★ **state.vscdb를 열지 않는다.** 실측 결과 DB 내부에 단일 활동시각 필드가 없어(ItemTable만; history.entries뿐) 실익 없음.
    이로써 R-002(DB 잠금) 회피. temp-copy/immutable 패턴은 P2 미도입(YAGNI). — 결정 로그/R-002에 기록됨.
- `source_file` = `state.vscdb`(또는 `workspace.json`) 경로.

### 3. `acp/timeutil.py` (신규, additive util) + ms→dt 공유
- 현재 `_ms_to_dt`는 `codex.py:73`에 모듈-프라이빗으로 있음. claude.py도 ms 변환 필요 → **사설 import 금지**.
- 작은 공개 헬퍼 `acp/timeutil.py: ms_to_dt(ms) -> datetime | None` 신설(codex의 로직 복제). claude.py가 사용.
  - codex.py 리팩터(자기 `_ms_to_dt`를 timeutil 재사용으로 교체)는 **선택**(저위험·additive). 하면 회귀 픽스처로 71개 그대로 통과 확인. 안 하면 그대로 둠.
- mtime→dt는 단순하니 cursor.py 로컬 처리 가능(또는 timeutil에 `mtime_to_dt` 추가).

### 4. `acp/join.py` — `join_phase(record, ...)` (신규) — **이 프로젝트의 심장**
- 입력: `SessionRecord`(+ 판정된 `SessionState`). 출력: `PhaseJoin`(아래 모델).
- 알고리즘:
  1. `record.project_path`가 None이거나 **비-`file://` 원격 uri**(cursor 원격) → `flag="no-phase-file"`로 즉시 반환.
  2. **cwd 정규화:** `Path(project_path).resolve()` → Windows `os.path.normcase`/`str.casefold()`(대소문자 무시), 슬래시 통일, 후행 슬래시 제거.
  3. **상위로 거슬러 `PHASE.md` 탐색:** cwd→부모로 올라가되 **`.git`(또는 `.hg`) 디렉토리를 만나면 그 레벨에서 멈춤**(경계),
     경계 못 만나면 **최대 5단계**. 각 레벨에서 `PHASE.md` 존재 확인. **최근접 1개만** 채택(모노레포 다중 = YAGNI).
  4. 찾으면 `parse_phase_md(path)`(phase.py:63) 호출:
     - 반환 `PhaseDoc` → `current_phase / phase_status / owner_session / phases[]` 결합, `flag="ok"`.
     - 반환 `None`(파싱/스키마 실패) → `flag="unknown"`. **PHASE.md 미존재(flag="no-phase-file")와 명확히 구분**(섞지 말 것, C3).
  5. **plan-stale 판정**(둘 중 하나면 stale 부가 플래그, ok와 공존 가능):
     - 세션 활동중(`state ∈ {LIVE, RUNNING, IDLE}`)인데 `PhaseDoc.updated_at`(ISO str, phase.py:55)을 파싱한 시각이
       `record.last_activity`보다 **24h 이상 과거** → `plan_stale=True`.
     - 또는 `current_phase`에 해당하는 `phases[]` 엔트리 status가 `done`인데 세션이 활동중 → `plan_stale=True`.
     - `updated_at` 파싱 실패(None/형식오류)는 stale로 단정 말고 `plan_stale=False` + 주석(추정 금지).
- **PhaseJoin 모델**(join.py 안에 pydantic `BaseModel` 또는 frozen dataclass):
  `flag: Literal["ok","plan-stale","no-phase-file","unknown"]` 는 단일 enum이 아니라
  `flag`(존재/파싱) + `plan_stale: bool`(노후) **2축**으로 두는 걸 권장 → 조합 표현 가능.
  필드: `current_phase|None, phase_status|None, owner_session|None, phases_done/total(진행률용 int), flag, plan_stale, phase_source|None`.

### 5. 저장: `acp/store.py` 확장 (additive)
- sessions 테이블에 phase 컬럼 추가: `current_phase, phase_status, owner_session, phase_flag, plan_stale, phase_source`.
- ★ **마이그레이션 함정:** `_SCHEMA_SQL`(store.py:19)은 `CREATE TABLE IF NOT EXISTS` — **기존 .acp/acp.db엔 새 컬럼이 안 생긴다.**
  `__init__`의 `executescript` 직후(store.py:71 근처)에 **멱등 `ALTER TABLE ADD COLUMN`** 추가:
  `PRAGMA table_info(sessions)`로 기존 컬럼 집합 조회 → 없는 컬럼만 `ALTER TABLE sessions ADD COLUMN ...`.
- `upsert_session` 시그니처(store.py:78)는 **호환 유지 + 추가**: `upsert_session(record, state, phase: "PhaseJoin | None" = None)`
  (기본값 None → 기존 호출부·테스트 무변경). phase가 주어지면 INSERT/UPDATE 컬럼에 포함.
- `list_sessions`/`get_session`은 `SELECT *`라 새 컬럼 자동 포함(별도 변경 불필요).

### 6. 폴링 통합: `acp/poller.py`
- 조인 호출 위치 = `_tick`의 `derive_state` 직후, `upsert_session` 호출 지점(**poller.py:58–62**):
  ```
  state = derive_state(record, now, self._cfg.liveness, is_alive=is_pid_alive)
  phase = join_phase(record, state)          # ← 신규 1줄
  ...
  self._store.upsert_session(record, state, phase=phase)   # ← phase 인자 추가
  ```
  - poller는 매 tick **전체 레코드 재수집·재upsert** → PHASE.md를 수정해도 ≤15s 내 상황판 반영(별도 watch 불필요).
- (선택) state_change 이벤트(poller.py:65–77)에 phase 변경도 broadcast할지는 P2 범위 밖(상태배지 위주 유지). 페이지 새로고침/주기 폴링으로 충분.

### 7. 수집기 등록: `acp/__main__.py`
- 실모드 분기(`__main__.py:36–40`, `else:` 블록)에 2줄 추가:
  ```
  poller.register(ClaudeSessionCollector(cfg.get_path("claude_sessions")))
  poller.register(CursorWorkspaceCollector(cfg.get_path("cursor_workspace")))  # 선택: + vscode_workspace
  ```
  → Codex와 함께 3종 동시 수집. config `app_paths`엔 `claude_sessions`/`cursor_workspace`/`vscode_workspace` **이미 존재**(paths.yaml 확인됨, 추가 불필요).

### 8. 상황판 완성: `acp/web/app.py` + `templates/dashboard.html` — 보드의 심장
- **컬럼**(dashboard.html:19–32 thead, 34–49 tbody 확장):
  `[상태배지][앱][세션][프로젝트][모델][현재페이즈][phase_status(진행률 배지)][역할(owner_session)][마지막 이벤트][최근 활동][실행명령][PID][갱신]`.
- **배지 헬퍼**: `_STATE_BADGE`(app.py:85)와 별개로 `phase_status`/`flag` 배지 클래스 함수 추가 → `templates.env.globals`(app.py:101 패턴)로 주입.
  - `plan-stale`·`no-phase-file`·`unknown`(phase_flag) 시각 구분(색/아이콘). 빈 페이즈는 `—` 대신 `no-phase-file` 명시.
- **그룹핑**: 앱별 그룹 + 동일 cwd 다중세션 그룹(R-005). Jinja `groupby`(app.py page_dashboard:108에서 정렬해 전달) 또는 템플릿 내 그룹.
- 진행률: `phases_done/total`로 미니 바 또는 `2/4` 텍스트.
- → 핵심 가치: **"어떤 에이전트가 어떤 프로젝트에 어떤 역할로, 어느 페이즈까지 진행 중인가"를 한눈에.**

---

## ★ out-of-scope (P2에서 손대지 말 것) ★
- 알림 발행/Notifier(P3), WebSocket 전환, DONE 로직 변경.
- 세션 통제·종료(영구 비목표), 대화 본문 파싱(Lv3, R-006), ChatGPT 채팅앱(비목표).
- Claude `title`/`completedTurns`/`planPath` 등 부가 필드 수집(YAGNI — 보드 가치는 PHASE.md 조인에서 나옴).
- state.vscdb DB 오픈/temp-copy(위 §2 결정으로 회피). 모노레포 다중 PHASE.md.

---

## [부착점 표] (시그니처/식별자로 재확인 후 부착 — 라인 이동 가능)
| 파일:라인(실측) | 작업 |
|---|---|
| `acp/collectors/claude.py` (신규) | `BaseCollector`(base.py:15) 구현. local_*.json rglob → SessionRecord |
| `acp/collectors/cursor.py` (신규) | `BaseCollector` 구현. workspace.json folder-uri 파싱 + state.vscdb mtime |
| `acp/timeutil.py` (신규) | `ms_to_dt` 공개 헬퍼(codex.py:73 로직 이관/복제) |
| `acp/join.py` (신규) | `join_phase` + `PhaseJoin`. cwd 정규화·상향탐색·plan-stale |
| `acp/store.py:19` `_SCHEMA_SQL` / `:71` executescript 직후 | 멱등 ALTER TABLE 마이그레이션 + phase 컬럼 |
| `acp/store.py:78` `upsert_session` | `phase=None` 추가 인자(호환) |
| `acp/poller.py:58–62` `_tick` | `join_phase` 호출 + `upsert_session(...,phase=)` |
| `acp/__main__.py:36–40` else 블록 | Claude/Cursor Collector register 2줄 |
| `acp/web/app.py:85,101,108` / `templates/dashboard.html:19–49` | phase 컬럼·배지·그룹핑 |

## [아키텍처 결정 — 고정]
- **Core(models/store 공개 sig/liveness/web 기존 엔드포인트) 변경 금지.** 새 앱 = Collector 추가만. store/upsert는 **additive(기본값 인자)**.
- Cursor 활동시각 = **state.vscdb mtime**(DB 미오픈). 원격 uri = `no-phase-file`.
- PHASE.md **미존재(`no-phase-file`)** 와 **파싱실패(`unknown`)** 는 다른 상태. plan-stale은 별도 bool 축.
- 조인은 cwd 정규화(resolve+casefold+슬래시) 후 `.git`/5단계 경계 내 상향 최근접 탐색.

## [불변 원칙]
- read-only(앱 파일·PHASE.md 수정 금지). silent fallback 금지(C3): 조인 실패·파싱 실패·경로 부재는 **명시 flag로 표면화**.
- 수집·판정·표시 경로에 **LLM 0회**.
- 스레드/asyncio 안전: collect()는 블로킹 파일 I/O지만 poller가 직렬 호출 — UI/이벤트루프 차단 주의(무거운 rglob는 codex의 mtime 캐시 패턴 차용 권장).
- **Timebox:** 같은 축(예: uri 정규화, ALTER 마이그레이션) L1 3회 안 풀리면 멈추고 실패 로그+원인+다음 접근 보고. 임의 우회/추정 채움 금지.

## [검증 / DoD]
1) **결정 테스트**(`tests/test_codex_collector.py` 스타일 + `tests/fixtures/` 패턴 모방, 신규 픽스처 추가):
   - claude: local_*.json 파싱(ms→dt, None pid), 파싱실패 skip+계속.
   - cursor: file:// 로컬 uri 디코드, **vscode-remote 원격 uri → no-phase-file 경로**, mtime 활동시각, workspace.json mtime 폴백.
   - `join_phase`: PHASE.md 있음(ok+필드결합) / 없음(no-phase-file) / 파싱실패(unknown) / **셋 구분** / plan-stale(updated_at 24h+ 과거, phases done) / cwd 대소문자·슬래시 정규화 매칭 / .git 경계·5단계 상한.
   - store: 멱등 ALTER(기존 DB에 컬럼 추가 후 재오픈 무오류), upsert phase 라운드트립, 기존 71개 회귀 통과.
2) **Live smoke**: 실제 Claude+Cursor+Codex 세션이 상황판에 **동시 표출** + 최소 1개 프로젝트의 현재 페이즈/역할 표시. 캡처 또는 `/api/sessions` JSON dump를 artifact 경로로 명시.
3) `pytest -q` 통과(명령 + 결과 수치 그대로 보고).
4) **리뷰 게이트:**
   - Nitpicker(기계, 기본 하드 게이트): 수정 파일마다 **로컬 LLM 모드**로 실행. repo 래퍼가 있으면 우선, 없으면
     `Nitpicker Daemon/bin/jemmin_cli.py --provider ollama --no-daemon` 계열 사용. `mini_nitpicker.py`/Gemini API 키 경로는 기본값 아님.
   - 별도 Reviewer(설계): L2 설계 변경, P1/P2급 저장·동시성·마이그레이션 변경, 사용자 요청, 또는 Nitpicker가 판단하기 어려운
     아키텍처 리스크가 있을 때 수행. P2는 store ALTER/poller/join이 있으므로 수행 권장. 무조건 하드 게이트로 일반화하지 말 것.
   - 로컬 LLM 서비스/모델이 준비되지 않았으면 `blocked: local LLM unavailable`로 보고하고 PASS를 주장하지 않는다.
5) **Sync-Out**: `PHASE.md` frontmatter(`current_phase: P2`/`phase_status: done`/`updated_at` 갱신) + §1 페이즈 개요 P2 ✅ +
   `docs/HANDOFF.md`(새 공개 API·검증증거·P3 준비) + `docs/lessons/p2-multiapp-join.md`(append: WHY/LESSON — 특히 state.vscdb mtime 결정, 원격uri, ALTER 함정). 커밋 확인 후 `git tag acp-phase-2`.

## [완료 보고 양식]
변경 파일 목록 / 조인·정규화·mtime·ALTER 결정 위치(file:line) / 검증(명령+출력 수치+Live smoke artifact 경로) /
**PASS는 어디까지·NOT CLAIMED(=뭐가 왜)·가정** 정직 표기 / P3(Notifier) 준비 상태.
