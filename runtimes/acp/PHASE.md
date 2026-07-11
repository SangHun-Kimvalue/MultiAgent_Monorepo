---
# ── 관제소가 파싱하는 고정 블록 (acp:phase v1) — 이 프로젝트는 자기 포맷을 dogfood 한다 ──
acp_schema: "phase/1.0"
project_id: "AgentControlPlane"
roadmap_ref: "self"
current_phase: "V1"
phase_status: "review"
pass_level: "unit+e2e+sse-integration+reviewer; phase-nitpicker-blocked"
updated_at: "2026-06-11T17:45:00+09:00"
phases:
  - id: "P0"
    title: "코어 스캐폴드 (FastAPI+SQLite+PHASE 파서+대시보드 골격)"
    status: "done"
  - id: "P1"
    title: "Codex 수집 + 홀딩 탐지 (킬러 기능)"
    status: "done"
  - id: "P1.5"
    title: "홀딩/생존 판정 버그 패치 (in-turn 일반화 + tail 확대 폴백)"
    status: "done"
  - id: "P2"
    title: "멀티앱 확장(Claude/Cursor) + 세션×PHASE 조인"
    status: "done"
  - id: "P3"
    title: "알림 (좀비/에러/완료/홀딩 → 토스트·웹훅)"
    status: "review"
  - id: "U1"
    title: "상황판 뷰 토대 + KPI 요약 스트립 (Alpine reactive + Playwright e2e)"
    status: "review"
  - id: "U2"
    title: "상황판 필터/정렬"
    status: "review"
  - id: "V1"
    title: "검증 하니스 정비 + 전체 통합 E2E + 리뷰 게이트 클로즈"
    status: "review"
  - id: "U3"
    title: "보드 노이즈 정리 (오래된 세션 보존 컷오프 + 프로젝트 경로 단축)"
    status: "planned"
owner_session: "Implementer"
blocking: "V1 코드/테스트/Reviewer는 PASS. P3/U1/U2 과거 페이즈 Nitpicker 전체 클로즈는 local LLM stale analyzing/timeout으로 blocked(PASS 미주장). run.bat untracked는 의도 밖 파일로 제외."
---

# Agent Control Plane — 전체 페이즈 로드맵

작성일: 2026-06-09
문서 성격: 다중 세션 협업용 진행 기준(SoT 보조). **이 PHASE.md가 곧 경량 로드맵**(옵션 A).
대상/전제: 단일 PC(Windows), Owner 1인. 상용 GUI 앱(Codex/Claude/Cursor) 세션을 **읽기 전용 폴링**으로 종합.
상위 SoT: `discovery/requirements.md` · `discovery/design.md` · `discovery/validation_plan.md` ·
          `discovery/phase_md_format.md` · `discovery/open_items.md` · `discovery/risk_register.md`
방법론 캐논: `../cubi-skills/METHODOLOGY.md` (C1~C7), `../cubi-skills/DOC_TAXONOMY.md`.

> **한 줄 정의:** 여러 병렬 에이전트 / 데스크톱앱(Codex/Claude/Cursor) / 프로젝트를 **한 상황판에서 종합**하는
> 비-LLM **통합 정보 관제 SW**. 각 프로젝트가 규약(PHASE.md 등)으로 자기 상태를 폴더에 기록하면 지통소가
> 주기 폴링해 보드에 갱신: **진행상황·페이즈·세션 생존·에이전트↔프로젝트 바인딩·세션 역할**을 토큰 0원으로.
> **출발 동기:** ZTR가 GUI 세션의 입력대기(홀딩)를 감지 못해 무한대기에 빠진 사각지대. 단, 홀딩 탐지는
> 보드 위 **여러 상태 중 하나**이지 유일 목적이 아니다.

---

## 0. 확정 결정 로그 (변경 시 이 표를 갱신)

| 항목 | 확정값 | 근거 |
|---|---|---|
| 수집 방식 | 로컬 아티팩트 **주기 폴링**(read-only). in-app push/프록시 금지 | 상용앱 코드주입 불가. design.md OI-001 |
| 언어/런타임 | Python 3.12, asyncio | ZTR 스택 재사용(학습비용 0) |
| 웹 | FastAPI + sse-starlette + Jinja2 + HTMX | ZTR `src/web/app.py` 패턴 복제 |
| DB | SQLite WAL, 동기 `sqlite3` | ZTR `src/engine/session_store.py` 패턴 복제 |
| 스키마 검증 | Pydantic v2 | ZTR 기 사용. 정규화 레코드/frontmatter 검증 |
| 폴링 모델 | asyncio 주기 루프(interval 설정값). **watchdog 미도입(YAGNI)** | 단순·결정적. C5 |
| 수집 경계 | `BaseCollector`(ABC) + 앱별 구현 1개씩 + Fake(테스트) | 실교체 경계. C5/C7. 새 앱=Collector 1개 추가 |
| 상태 enum | LIVE·RUNNING·IDLE·**HOLDING**·STALE·ERROR·DONE·UNKNOWN | design.md 상태전이. HOLDING=ZTR형 입력대기 |
| 홀딩 판정 | **jsonl 마지막 이벤트(last_evt) + 경과시간 조합**. task_complete+age>hold(턴간 망각) 또는 approval_request/턴중단(승인대기) → HOLDING | 실측 2026-06-09. osPid는 툴콜 서브프로세스라 휴지 시 항상 부재(판별불가). R-003 |
| osPid 용도 | **RUNNING 확인 전용 양성 신호**(살아있으면 능동 실행중). 부재=중립, orphan 가능 → 단독 판정 금지 | chat_processes.json osPid 실측: 9개 중 8개 죽음, 1개 hung ssh |
| 완료/에러 신호 | jsonl `event_msg.payload.type` 화이트리스트(`task_complete`/`task_aborted`/`error`)만. raw_status 부분문자열 매칭 폐기 | 문자열매칭 오탐. 명시 이벤트만(C3) |
| 임계값(기본, config 조정) | idle=300s(5분) / hold=900s(15분) / stale_ttl=3600s(60분) / poll=15s | 장시간 작업 위주. Owner 확정 2026-06-09 |
| DONE 상태 | Codex 세션엔 **미적용**(task_complete는 턴 단위). PHASE.md `phase_status=done` 표시에만 사용 | 세션은 완료 없이 STALE로 늙음 |
| 좀비 판정 | HOLDING이 stale_ttl 초과 → STALE | 홀딩 한계 초과 |
| 홀딩 알림 | 전이당 **1회**(dedupe 단순). 무한대기 허용 | Owner 확정: 1회면 충분 |
| last_activity 출처 | jsonl 마지막 이벤트 ts(정확) > chat_processes updatedAtMs(툴콜·지연). jsonl mtime 변동 시만 re-tail | I/O 절감 |
| 세션 식별 id | 앱 네이티브: Codex=conversationId, Claude=sessionId, Cursor=wsHash+folder | OI-007 |
| 페이즈 SSOT | 각 프로젝트 `PHASE.md` frontmatter(옵션 A) | phase_md_format.md |
| silent fallback | **금지(C3)**. 파싱실패/스키마불일치 → `UNKNOWN` 명시 + 사유 로깅 | 캐논 C3. R-001 |
| 노후 경고 | 세션 활동O인데 PHASE.md `updated_at` 오래됨 → `plan-stale` 배지 | 갱신누락 이중방어 |
| 알림 | Nitpicker `NotificationService`(토스트+웹훅) 패턴 재사용 | `Nitpicker Daemon/src/jemmin/services/notifier.py:88` |
| LLM 사용 | v1 수집·판정·표시 경로에 **0회**. 온디맨드 브리핑은 v2 | 토큰 0원 불변식 |
| 저장 경로 | `./.acp/acp.db`(WAL), 감사로그 `./.acp/events.jsonl` | ZTR `.ztr/` 관례 |
| 앱 아티팩트 경로 | `config/paths.yaml`로 외부화(기본=실측된 경로) | 하드코딩 금지. 재현성 C6 |
| 브랜치/경계 | 단일 `acp-v1`. **태그 미생성**(사용자 명시 요청 시에만). 페이즈 경계는 HANDOFF·커밋 메시지·결정 로그로 기록 | 스킬 §9 |
| 빌드/검증 | `pip install -e ".[dev]"` → `pytest -q` | ZTR 관례 |
| Nitpicker | 로컬 LLM 모드(`jemmin_cli.py --provider ollama`) 또는 repo 래퍼(있으면 우선). Gemini/API 키 경로는 기본값 아님 | 스킬 §8 |
| UI 기술스택 | 서버렌더(FastAPI+Jinja) 유지 + **Alpine.js 벤더링**(무빌드 reactive). 풀 SPA(React/Vue) 미도입 | KPI+필터/정렬엔 SPA 과설계. 무빌드·토큰0 철학 유지. Owner 확정 2026-06-11 |
| 실시간 일관성 모델 | **하이브리드**: `/api/sessions` 주기 스냅샷(poll_interval) 재fetch + SSE 델타 패치. KPI·테이블은 단일 모델의 파생뷰 | 스냅샷=신규/필드변경 진실원천, SSE=즉시성. 단독 사용 시 누락/지연 |
| 브라우저 테스트 | **Playwright e2e**(`[e2e]` optional-deps, dev 전용). 헤드리스 chromium, `--fake` 임시포트 기동 | 렌더·KPI·SSE 갱신 자동검증. 런타임 의존 아님 |
| 테스트 게이트 | `pytest -q`는 `-m "not e2e"`로 단위/비브라우저만, e2e는 `pytest -m e2e tests/e2e -q`로 별도 실행 | V1에서 U1 Reviewer MAJOR 해소. 수치 과대주장 방지 |
| KPI 프로젝트 수 | `no-project`도 프로젝트 버킷 1개로 센다 | 그룹/필터의 `no-project` 처리와 KPI 일관성 유지 |
| 세션 보존 컷오프 | last_activity 없음(None) **또는** last_activity/updated_at가 `max_age_days`(기본 7일) 초과 → **수집 단계 제외 + DB prune**(보드 미표시). 감사로그(events)는 보존 | 실데이터에서 오래된 STALE·활동시각없는 UNKNOWN 노이즈 폭주. **C3 UNKNOWN 표시를 보드 한정 의도적 우회**(사유 로깅 유지). Owner 확정 2026-06-12 |
| 프로젝트 경로 표시 | **마지막 2개 세그먼트**만(`…\Todo\AgentControlPlane`), full path는 title 툴팁. 필터/그룹 키는 full 유지(표시 전용) | 경로가 길어 가독성 저하. Owner 확정 2026-06-12 |
| 태그 | **미생성**(스킬 §9). 페이즈 경계는 커밋/HANDOFF/결정로그로 기록 | 사용자 명시 요청 시에만 |

### 앱별 아티팩트 부착점 (실측 2026-06-09, 경로 이동 가능 → 재확인 후 사용)

| 앱 | 소스 경로 | 핵심 필드 |
|---|---|---|
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | `session_meta`(id,cwd,model_provider,source) / `event_msg`(task_started,turn_id) |
| Codex(실시간) | `~/.codex/process_manager/chat_processes.json` | conversationId·cwd·command·**osPid**·startedAtMs·updatedAtMs |
| Claude | `%APPDATA%/Claude/claude-code-sessions/**/local_*.json` | sessionId·cwd·lastActivityAt·model |
| Cursor/VSCode | `%APPDATA%/Cursor/User/workspaceStorage/<hash>/workspace.json` + `state.vscdb` | folder(uri) / **활동시각=state.vscdb mtime** (실측: DB 내부에 깔끔한 활동시각 필드 없음 → mtime 채택, P2) |

> **Cursor 실측 보강(2026-06-10, P2 계획):** ① `workspace.json`은 `{"folder": "<uri>"}` 단일 키. uri는
> `file:///c:/...`(로컬) **또는 `vscode-remote://ssh-remote%2B<host>/...`(원격 SSH)** 일 수 있음 → 원격은 이 PC에
> cwd 부재 → `no-phase-file`. ② `state.vscdb` 테이블은 `ItemTable` 1개뿐, 시간성 키는 `history.entries`/`scm.history`로
> 단일 "마지막 활동시각" 추출 불가 → **DB 미오픈, state.vscdb 파일 mtime을 last_activity로 채택**(Owner 확정 2026-06-10).
> 이로써 R-002(state.vscdb 잠금) 리스크는 P2에서 **회피**(temp-copy read-only는 YAGNI로 보류).

---

## 1. 페이즈 개요

| Phase | 내용 | 상태 |
|---|---|---|
| P0 | 코어 스캐폴드: 패키지/DB/PHASE 파서/대시보드 골격 + FakeCollector로 E2E 골격 | ✅ done (검증·커밋·tag) |
| P1 | **Codex 수집 + 생존/홀딩 판정**: CodexCollector + derive_state(last_evt truth table) | ✅ done (검증·커밋·tag) |
| P2 | 멀티앱: Claude·Cursor Collector + 세션×PHASE.md 조인(현재페이즈·진행률·역할) + 상황판 완성 | ✅ done (구현·pytest 93·live smoke / Reviewer+Nitpicker PASS — 구현세션 ollama 실행·사용자 확인) |
| P3 | 알림: 좀비/에러/홀딩 → 토스트·웹훅(Notifier) + dedupe(전이당 1회) | 🟨 review (기능 구현·pytest·live smoke·Reviewer PASS / 과거 페이즈 Nitpicker 전체 PASS 미주장: stale analyzing/timeout blocked) |
| U1 | 상황판 뷰 토대(Alpine reactive·하이브리드 실시간 일관성) + KPI 요약 스트립 + Playwright e2e 토대 | 🟨 review (V1에서 테스트 게이트 MAJOR·KPI MINOR 해소, Reviewer PASS / 과거 페이즈 Nitpicker 전체 PASS 미주장) |
| U2 | 상황판 필터/정렬(앱·상태·프로젝트 필터, 심각도/활동 정렬, "행동 필요만" 토글) | 🟨 review (구현·e2e·Reviewer PASS / 과거 페이즈 Nitpicker 전체 PASS 미주장) |
| V1 | 검증 하니스 정비(단위·e2e 게이트 분리) + 전체 통합 E2E + P3/U1/U2 리뷰 게이트 클로즈 | 🟨 review (V1 코드·테스트·실앱 API smoke·Reviewer·V1 diff Nitpicker PASS / 과거 페이즈 Nitpicker closeout blocked) |
| U3 | 보드 노이즈 정리: 오래된 세션 보존 컷오프(7일·활동시각없음 → 수집제외+prune) + 프로젝트 경로 단축(마지막 2세그먼트) | ⬜ planned |

> **UI 트랙(U1~U2):** v1 백엔드(P0~P3) 위 additive. `/api/sessions`+SSE 계약 위에 얹히므로 P3 Nitpicker 종료와 독립. 무빌드(Alpine 벤더링), 풀 SPA 미도입.

> **전략:** minimal-first. "수집 가능 ≠ 지금 다 구현." P1에서 **Codex 하나만** 끝까지 파서 홀딩탐지 가치를
> 먼저 증명하고, P2에서 앱을 늘린다. 각 페이즈는 독립적으로 Live smoke 가능해야 한다.

---

## 2. 완료 산출물 (기준 코드) — 공개 API(이후 페이즈에서 변경 금지)

P0 완료 시 다음 공개 경계가 고정된다(P1~P3는 이걸 구현/주입). **단 추가(additive) 확장은 허용**: P1에서
`SessionRecord.last_event`(신규 필드) + `derive_state`에 `is_alive` 콜러블 인자를 더한다. 기존 필드/시그니처는
변경 금지, 추가만 한다(하위호환).

```python
# acp/collectors/base.py
class BaseCollector(ABC):
    app_name: str
    @abstractmethod
    def collect(self) -> list[SessionRecord]: ...   # 순수 read-only, 예외는 전파(C3)

# acp/models.py  (Pydantic v2)
class SessionRecord(BaseModel):
    schema_version: str; app: str; session_id: str; project_path: str | None
    model: str | None; last_activity: datetime | None
    running_pid: int | None; running_cmd: str | None
    raw_status: str | None           # 앱이 준 원시 표식(완료/에러 등). 없으면 None
    last_event: str | None           # (P1 확장) jsonl 마지막 event_msg.payload.type — 생존판정 1차 신호
    source_file: str                 # 부착 아티팩트 경로(감사용)

class SessionState(StrEnum):
    LIVE; RUNNING; IDLE; HOLDING; STALE; ERROR; DONE; UNKNOWN
    # 주: DONE은 Codex 세션엔 미적용(PHASE.md 완료 표시 전용). 세션은 STALE로 늙음.

# acp/store.py        : SQLite WAL. upsert_session(record,state,phase=None) / list_sessions / append_event
# acp/liveness.py     : derive_state(record, now, cfg, is_alive) -> SessionState  (순수함수)
#                       last_evt + age + alive 결정테이블. is_alive는 콜러블 주입(RUNNING 확인 전용).
# acp/phase.py        : parse_phase_md(path) -> PhaseDoc | None           (frontmatter만)
# acp/join.py         : join_phase(record,state) -> PhaseJoin             (P2, PHASE.md 조인)
# acp/notify.py       : Notifier.notify(StateTransitionEvent)             (P3, toast/webhook)
# acp/dedupe.py       : NotificationDedupe.should_notify/should_reset     (P3, transition dedupe)
# acp/web/app.py      : FastAPI. GET / (대시보드), /api/sessions, /api/live/stream(SSE)
```

---

## 3. 페이즈별 구현 프롬프트

각 페이즈 프롬프트는 `prompts/phase-P<x>.md`에 있다(Implementer 세션에 그대로 전달).

| Phase | 프롬프트 | 한 줄 |
|---|---|---|
| P0 | `prompts/phase-P0.md` | 코어 스캐폴드 + FakeCollector로 수집→저장→상태→SSE 골격 |
| P1 | `prompts/phase-P1.md` | CodexCollector + LivenessSvc(HOLDING/STALE) + 실 Codex Live smoke |
| P2 | `prompts/phase-P2.md` | Claude/Cursor Collector + 세션×PHASE 조인 + 상황판 |
| P3 | `prompts/phase-P3.md` | Notifier(토스트+웹훅) + 상태전이 트리거 + dedupe |
| U1 | `prompts/phase-U1.md` | Alpine reactive 모델 + KPI 스트립 + Playwright e2e 토대 |
| U2 | `prompts/phase-U2.md` | 필터/정렬 + "행동 필요만" 토글 + e2e 확장 |
| V1 | `prompts/phase-V1.md` | 테스트 게이트 격리 + 통합 E2E + 리뷰 게이트 클로즈 |
| U3 | `prompts/phase-U3.md` | 오래된 세션 보존 컷오프(수집제외+prune) + 프로젝트 경로 단축 |

---

## 4. 불변 원칙 (전 페이즈 공통)

- **C3 Fail-fast / silent fallback 금지:** 파싱 실패·파일 잠금·스키마 불일치는 `UNKNOWN`으로 명시 강등 +
  사유 로깅. 절대 옛 데이터를 현재처럼 표시하지 않는다.
- **읽기 전용:** 앱 아티팩트·PHASE.md를 **수정하지 않는다.** Cursor `state.vscdb`는 DB 오픈 없이 mtime만 읽는다(P2 R-002 회피).
- **토큰 0원:** 수집·판정·표시 경로에 LLM 호출 금지.
- **결정적 판정:** `liveness.derive_state`는 순수함수(입력=레코드+now+cfg). 시간은 주입(테스트 mock 가능).
- **스레드/asyncio 안전:** 폴링 루프는 비차단. SQLite 접근은 단일 커넥션 직렬화 또는 커넥션-per-thread.
- **Timebox:** 같은 축 수정이 L1 3회 / L2 checkpoint 안 풀리면 멈추고 실패 로그+원인+다음 접근 보고.
- **Sync-Out 의무:** 페이즈 종료 시 이 PHASE.md frontmatter(`current_phase/phase_status/updated_at`) 갱신 +
  `docs/HANDOFF.md` 갱신 + `docs/lessons/<module>.md` append.

## 5. 페이즈 공통 DoD

- [ ] 해당 페이즈 공개 API 구현 + Pydantic 모델 검증
- [ ] unit/deterministic 테스트(픽스처 기반). mock은 명시적 test adapter(silent fallback 아님)
- [ ] `pytest -q` 통과(실행 증거: 명령+결과)
- [ ] (P1+) 실제 앱 1회 Live smoke — 캡처/로그 artifact 경로 명시
- [ ] 리뷰: Nitpicker local LLM PASS(기본 하드 게이트). 별도 Reviewer 세션은 L2 설계 변경, P1/P2급 저장·동시성·마이그레이션 변경, 사용자 요청, 또는 Nitpicker가 판단하기 어려운 아키텍처 리스크가 있을 때 수행한다.
- [ ] PHASE.md frontmatter + HANDOFF + lessons 갱신 (페이즈 경계는 커밋/HANDOFF로 기록 — 태그 미생성, 스킬 §9)
