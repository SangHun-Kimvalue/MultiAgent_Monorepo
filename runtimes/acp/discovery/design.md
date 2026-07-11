# Design

## Architecture Summary

상용 GUI 앱은 in-app push가 불가능하므로, v1은 **읽기 전용 로컬 아티팩트 수집(harvest) 아키텍처**다.
"Push가 Poll을 이긴다"는 일반 원칙은 *내가 계측 가능한 에이전트*에만 적용된다. 상용 앱 도메인에서는
**로컬 아티팩트 주기 폴링이 유일하고 올바른 수집 방식**이며, 순수 파일 파싱이라 토큰 0원이다.

> **이 시스템의 존재 이유(재정의 2026-06-09):** 여러 병렬 에이전트 / 데스크톱앱(Codex/Claude/Cursor) /
> 프로젝트를 **한 상황판에서 종합**하는 비-LLM **통합 정보 관제 SW**다. 각 독립 프로젝트가 규약(PHASE.md 등)으로
> 자기 상태를 폴더에 기록하면 지통소가 주기적으로 풀해 보드에 갱신한다. 보여주는 것: **진행상황 · 페이즈 내용 ·
> 세션 생존 · 에이전트↔프로젝트 바인딩 · 세션 역할**. 홀딩/좀비 탐지(out-of-band liveness)는 그 보드 위의
> **여러 상태 중 하나**이지 유일 목적이 아니다(ZTR 무한대기 보완은 출발 동기였을 뿐). 에이전트는 자율형
> (LLM이 다음 페이즈를 유동 결정)이라 PHASE.md가 자주 바뀐다 → 노후 경고로 갱신 누락을 방어한다.

```
[데스크톱 앱 로컬 흔적 — read only]                       [프로젝트 산출물 — read only]
 Codex(최고 품질):                                          <project>/PHASE.md   (페이즈 SSOT, 캐논 정렬)
   ~/.codex/sessions/**/rollout-*.jsonl                       · 페이즈 개요/상태 + 결정로그
     · session_meta(id,cwd,model_provider,source)
     · event_msg(task_started/turn_id, ...)
   ~/.codex/process_manager/chat_processes.json
     · conversationId·cwd·command·osPid·updatedAtMs  ← 실시간 프로세스 레지스트리
 Claude: claude-code-sessions/**/local_*.json
     · cwd·lastActivityAt·model
 Cursor/VSCode: workspaceStorage/<h>/workspace.json + state.vscdb
        │  poll + watch                                          │ poll + watch
        ▼                                                        ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │ Collectors (BaseCollector 구현 N개)                                    │
 │  - CodexCollector (jsonl tail + chat_processes.json)  ← 1순위(가장 풍부)│
 │  - ClaudeSessionCollector   - CursorWorkspaceCollector                 │
 │  - PhaseFileCollector(PHASE.md)                                        │
 │  → 정규화 레코드(session_id, app, project, model, last_activity,       │
 │                  running_pid, running_cmd, raw_status)                 │
 └───────────────────────────────┬──────────────────────────────────────┘
                                 ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │ Control Tower Core (FastAPI, ZTR 스택 재사용)                          │
 │  - StateStore: SQLite(WAL)   - JoinSvc: 세션 × PHASE.md 결합            │
 │  - LivenessSvc: 활동시각 + osPid 생존 → LIVE/IDLE/HOLDING/STALE/ERROR/DONE│
 │  - Notifier: 좀비/에러/완료/홀딩 → 토스트·웹훅 (Nitpicker 자산 재사용)  │
 └───────────────────────────────┬──────────────────────────────────────┘
                                 ▼ SSE
 ┌──────────────────────────────────────────────────────────────────────┐
 │ Dashboard(상황판): 앱별 세션 인벤토리 · 프로젝트·모델 · 실행명령 ·      │
 │                    페이즈 계획/현재 페이즈 · 생존바 · 홀딩 경고         │
 └──────────────────────────────────────────────────────────────────────┘
```

## Ownership / SSOT

| Value / state / artifact | Owner | Source of truth | Writer rule |
|---|---|---|---|
| 페이즈 계획 + 현재 페이즈 | Owner | 각 프로젝트 `PHASE.md` (cubi-skills 캐논 정렬) | 사람만 수정. 관제소는 read-only |
| 세션↔프로젝트 바인딩, 활동시각, 모델, 실행명령 | 데스크톱 앱 | 앱 로컬 파일(Codex jsonl+chat_processes / Claude JSON / Cursor workspaceStorage) | 앱만 기록. 관제소는 read-only 파싱. Cursor state.vscdb는 DB 미오픈, mtime만 |
| 실행 프로세스 생존(osPid) | OS | `chat_processes.json`의 osPid + OS 프로세스 테이블 | 관제소는 조회만 |
| 집계 상태(LIVE/HOLDING/STALE 등) | 관제소 코어 | SQLite(파생 데이터, 비권위) | 코어만 기록. 원본 아님 — 언제든 재생성 가능 |
| 알림 발행 이력 | 관제소 코어 | append-only 로그(JSONL) | 코어만 append |

## Boundaries

| Boundary | Responsibility | Adapter / interface | Out of scope |
|---|---|---|---|
| Collector | 앱별 로컬 포맷 → 정규화 레코드 | `BaseCollector` (앱마다 1 구현) | 앱 제어, 네트워크 가로채기 |
| Core | 저장·조인·생존판정·알림 | FastAPI 라우트 + SQLite | 페이즈 계획 *작성* |
| Notifier | 토스트/웹훅 발행 | Nitpicker `notifier.py` 재사용 | 양방향 통제 |
| Dashboard | 표시(SSE) | ZTR 웹 스택 재사용 | 세션 조작 |

> Adapter 경계 검증: `BaseCollector`는 "이름만 래퍼"가 아니라 **실교체 경계**다. 새 앱(예: 향후 ChatGPT
> 앱) 지원 = 새 Collector 1개 추가로 끝나야 하며 Core는 무변경이어야 한다.

## State Transitions (Codex 실측 기반 신호모델, 2026-06-09)

### 정찰 증거 (osPid/jsonl 실측)
- `~/.codex/process_manager/chat_processes.json` 엔트리 = `conversationId:turnId:call_xxx` + `command` + `osPid`.
  → **osPid = 개별 툴콜(셸 명령) 서브프로세스 PID**(앱도 턴도 아님). 실측 9개 중 8개 죽음, 살아있는 1개는
  멈춘 orphan `ssh`. → **osPid 부재는 휴지의 정상상태(중립 신호)**, 살아있어도 hung orphan 가능 → 단독 신뢰 불가.
  osPid는 오직 **RUNNING 확인용 양성 신호**로만 쓴다.
- `~/.codex/sessions/**/rollout-*.jsonl`: `event_msg.payload.type` ∈ {task_started, task_complete,
  agent_message, user_message, token_count, patch_apply_end, context_compacted, (exec/patch approval_request)}.
  턴 생명주기 `task_started → agent_message → token_count → task_complete`.
  ★ started/complete **카운트 짝맞춤은 취약**(휴지인데 OPEN=21 사례) → **마지막 이벤트 타입**으로 판정한다.
- **last_activity 출처**: chat_processes `updatedAtMs`(툴콜 단위·지연)보다 **jsonl 마지막 이벤트 ts**가 정확.
  I/O 절감: jsonl `mtime` 변동 시에만 re-tail.

### 판정 입력 (3 신호)
- `last_evt`: jsonl 꼬리의 마지막 `event_msg.payload.type` (SessionRecord 필드로 승격)
- `age = now - last_activity`
- `alive`: 해당 conversationId의 osPid 생존(`is_alive` 콜러블 주입) — **RUNNING 확인 전용**

### 결정 테이블 (derive_state 순수함수)
| last_evt | alive | age | → state | 비고 |
|---|---|---|---|---|
| `error` / `task_aborted` | — | — | **ERROR** | 명시 표식만 |
| `exec_approval_request` / `*_approval_request` | — | — | **HOLDING** | 턴중단·승인대기(되살림 가능) |
| **in-turn** (non-None, `task_complete` 제외) | true | — | **RUNNING** | 능동 실행중. 예: `agent_message`/`token_count`/`task_started` 등 |
| **in-turn** (non-None, `task_complete` 제외) | false | > hold | **HOLDING** | 턴중단·멈춤 |
| **in-turn** (non-None, `task_complete` 제외) | false | ≤ hold | **RUNNING** | 방금 spawn(잠정), age로 승급 |
| `task_complete` | — | < idle | **LIVE** | 최근 응답, 사용자 곁 가능 |
| `task_complete` | — | idle~hold | **IDLE** | 식는 중 |
| `task_complete` | — | hold~stale | **HOLDING** | **턴간·망각 ← ZTR 케이스** |
| `task_complete` | — | ≥ stale | **STALE** | 좀비 |
| 파싱불가 / 이벤트 없음 | — | — | **UNKNOWN** | C3, 추정 금지 |

> ⚠️ **P1.5 수정**: 구 결정테이블은 `task_started` 리터럴 단독으로 in-turn을 인식했으나,
> 실데이터에서 진행중 세션의 마지막 event_msg는 보통 `agent_message`/`token_count`다.
> → **in-turn = last_event is not None AND last_event ∉ COMPLETE_EVENTS**로 일반화.
> `task_started` 리터럴 한정은 실데이터에서 RUNNING/HOLDING 판정을 완전 무력화한다.

> **임계값(확정, config 조정가능):** `idle=300s(5분)` / `hold=900s(15분)` / `stale=3600s(60분)`, `poll=15s`.
> 모두 poll_interval 배수. 장시간 작업 위주 워크플로 기준.

> **DONE 상태 뉘앙스:** Codex 세션엔 "세션 완료" 표식이 없다(`task_complete`는 **턴 단위**). 따라서 8상태
> enum의 `DONE`은 **Codex 세션 상태엔 거의 미적용**이며, **PHASE.md `phase_status=done`** 표시에만 의미를
>갖는다. 세션은 완료가 아니라 그냥 STALE로 늙는다. (다른 앱에서 명시 종료 표식이 있으면 그때 DONE 사용)

> **HOLDING vs STALE.** HOLDING = 되살릴 수 있는 멈춤(턴간 망각 또는 턴중단 승인대기). STALE = stale_ttl
> 초과 좀비. 둘 다 알림하되 우선순위/문구 차등. **시간 단독 판정 금지**(반드시 last_evt와 조합).

> **Silent fallback 금지(C3):** 파싱 실패·파일 잠금·스키마 불일치는 절대 옛 데이터를 현재처럼 표시하지 않고
> `UNKNOWN` 명시 강등 + 사유 로깅. PHASE.md 없음(`no-phase-file`)과 파싱 실패(`UNKNOWN`)는 구분한다.

## Decisions

| Decision | Alternatives | Rationale |
|---|---|---|
| 상용 앱은 로컬 아티팩트 주기 폴링(harvest) | in-app push, 네트워크 프록시(mitmproxy) | 앱 코드 주입 불가. 폴링이 유일·안전·0토큰. 프록시는 침습적·취약 |
| **Codex Collector 1순위 구현** | Claude/Cursor 먼저 | Codex가 가장 풍부(jsonl 이벤트 + osPid 실시간 레지스트리). 홀딩 탐지 PoC를 가장 빨리 증명 |
| 홀딩 판정 = **last_evt + age** 조합(osPid는 RUNNING 확인 전용) | 시간 단독; osPid 부재 신호 | 실측: osPid는 툴콜 서브프로세스(휴지 시 항상 부재). 홀딩은 jsonl 마지막 이벤트(task_complete/approval) + 경과시간으로 판정. raw_status 문자열매칭은 취약 → 폐기 |
| 페이즈 SSOT = 프로젝트 `PHASE.md`, **cubi-skills 캐논 정렬** | 신규 포맷 발명, 앱 대화 추론 | 캐논(DOC_TAXONOMY: 로드맵=진행/결정 SSoT)을 따라야 drift 없음. 대화 추론은 토큰+부정확 |
| 코어 스택 = FastAPI+SQLite+SSE | 신규 스택, Redis | ZTR에 이미 구현·검증됨. 학습비용 0. 단일 PC엔 SQLite 충분 |
| Cursor state.vscdb는 DB 미오픈, mtime만 사용 | 복사 후 읽기/직접 열기 | 실측상 단일 활동시각 필드가 없어 DB 접근 실익이 낮고, mtime으로 잠금/손상 위험 회피 |
| 알림 = Nitpicker notifier 재사용 | 신규 구현 | 토스트+웹훅 이미 구현됨 |
| LLM은 온디맨드 브리핑에만(v1 범위 밖) | 상태 추적에 LLM 사용 | 토큰 0원 불변식 보호 |
