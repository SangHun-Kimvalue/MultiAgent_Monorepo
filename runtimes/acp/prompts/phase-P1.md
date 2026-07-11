작업: Agent Control Plane — Phase P1 (Codex 수집 + 생존/홀딩 판정). ★보드의 실데이터 첫 공급 + 홀딩 신호 증명★. 한국어로 응답/주석.

[브랜치] 단일 브랜치 `acp-v1` 유지. 새 브랜치 금지. 완료 시 `git tag acp-phase-1`. 시작 전 `git status`.

[전제] P0 완료·머지·리뷰 PASS 상태에서 시작 — **시작점: `acp-v1` @ `3ac5a25`**(P0 스캐폴드 + 설계 재정비 머지됨).
즉 `BaseCollector`/`SessionRecord`/`SessionState`/`store`/`liveness.derive_state`/`poller`/`web` 골격과 테스트
25개가 이미 통과 중이고, `config/paths.yaml`에 Codex 경로 placeholder가 있다. 아직이면 P0부터.

[SoT — 반드시 읽을 것]
- `AgentControlPlane/PHASE.md` §0 결정 로그(특히 홀딩/좀비 판정·앱별 부착점 표) + §4 불변 원칙
- `AgentControlPlane/discovery/design.md` "State Transitions"(HOLDING vs STALE 정의)
- `AgentControlPlane/discovery/requirements.md`(ZTR 홀딩 트리거 = 존재 이유)
- 실측 대상(읽기만): `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`,
  `~/.codex/process_manager/chat_processes.json`

[전략 리마인더] minimal-first. **Codex 하나만** 끝까지 판다(Claude/Cursor는 P2). 목표 둘: ① 보드에 실제 Codex
세션을 프로젝트(cwd)·모델·실행명령·상태로 **표출**(통합 관제의 첫 실데이터), ② last_event+age 결정테이블로
**홀딩/생존을 정확히 판정**해 증명. 홀딩은 여러 상태 중 하나이지 유일 목적이 아니다.

[이번 범위]
할 것:
1. `acp/collectors/codex.py` — `CodexCollector(BaseCollector)`:
   - `~/.codex/process_manager/chat_processes.json` 파싱: 항목 = `conversationId:turnId:call_xxx` + `command`·
     **osPid**·startedAtMs·**updatedAtMs**. ★주의: 이 파일은 **툴콜(셸 명령) 실행 로그**다. osPid는 개별 명령
     서브프로세스 PID(앱도 턴도 아님) → **RUNNING 확인 용도로만** 쓴다(살아있으면 능동 실행중). conversationId별로
     가장 최근 osPid/command를 running_pid/running_cmd로.
   - `~/.codex/sessions/**/rollout-*.jsonl` **tail** 파싱: 마지막 `event_msg.payload.type`(= **last_event**)와
     그 timestamp를 추출. `session_meta`로 cwd·model_provider. (전체 로드 금지 — 끝부분만. jsonl `mtime`이
     직전 폴 이후 안 바뀌었으면 re-tail 건너뛰기.)
   - conversationId 기준 두 소스 머지 → `SessionRecord`: running_pid=osPid(생존 시), running_cmd=command,
     **last_activity = jsonl 마지막 이벤트 ts**(우선) `else` updatedAtMs, **last_event = 마지막 payload.type**,
     raw_status는 더 이상 신뢰 신호 아님(있으면 보존만).
   - 스키마 불일치/파일 잠금/깨진 줄 → 해당 레코드 last_event=None+UNKNOWN 표기 + 경고 로깅(C3, silent 금지).
     전체 수집 중단 아님.
2. `acp/models.py` 확장 — `SessionRecord`에 **`last_event: str | None`** 추가(additive, 기존 필드 불변).
3. `acp/liveness.py` 확장 — `derive_state` 시그니처에 **`is_alive` 콜러블 주입** 추가하고 아래 **결정 테이블**을 구현
   (P0의 TODO 해소). **순수함수 유지**(now·cfg·is_alive 전부 주입, 내부 OS/시계 호출 금지):

   | last_event | is_alive(pid) | age=now-last_activity | → SessionState |
   |---|---|---|---|
   | `error` / `task_aborted` | — | — | ERROR |
   | `*_approval_request`(exec/patch 승인대기) | — | — | HOLDING |
   | `task_started`(완료 이벤트 없음) | True | — | RUNNING |
   | `task_started`(완료 없음) | False | > hold_threshold | HOLDING |
   | `task_started`(완료 없음) | False | ≤ hold_threshold | RUNNING(잠정) |
   | `task_complete` | — | < idle_threshold | LIVE |
   | `task_complete` | — | idle~hold | IDLE |
   | `task_complete` | — | hold~stale_ttl | HOLDING ← ZTR 망각 케이스 |
   | `task_complete` | — | ≥ stale_ttl | STALE |
   | None/미파싱 또는 last_activity None | — | — | UNKNOWN |
   - 음수 age(시계역전) → UNKNOWN. **DONE은 Codex에 쓰지 않는다**(task_complete는 턴 단위 ≠ 세션 완료).
4. `acp/proc.py` — `is_pid_alive(pid: int | None) -> bool` (None→False. Windows: psutil 또는 ctypes OpenProcess).
   liveness에 **콜러블로 주입**(테스트에서 fake 가능). osPid 생존은 RUNNING 확인에만 기여.
5. `acp/config.py` — Codex 경로(`~/.codex/...`)를 `config/paths.yaml` 기본값으로. 임계값 기본 **확정**:
   `idle_threshold=300`(5분) `hold_threshold=900`(15분) `stale_ttl=3600`(60분) `poll_interval=15`. (config 조정가능)
6. poller에 `CodexCollector` 등록(FakeCollector는 테스트 전용으로 강등). 대시보드에 running_cmd·상태 배지·
   last_event·last_activity 노출(템플릿 컬럼 추가).

★ out-of-scope ★: Claude/Cursor Collector(P2), 세션×PHASE.md 조인 표시(P2), 알림 발행(P3),
대화 본문 파싱(Lv3, 영구 비목표), DONE 상태 로직(Codex 미적용). 손대지 말 것.

[부착점/대상] (라인 이동 가능 → 시그니처로 재확인 후 부착)
| 파일 | 작업 |
|---|---|
| `acp/models.py: SessionRecord` | `last_event: str | None` 필드 추가(additive) |
| `acp/liveness.py: derive_state` | 시그니처 `(record, now, cfg, is_alive)`로 확장 + 결정테이블 구현 |
| `acp/collectors/codex.py` | 신규. chat_processes.json + rollout jsonl tail 머지 |
| `acp/proc.py` | 신규. is_pid_alive(주입용) |
| `acp/poller.py` | collector 목록에 Codex 등록 + derive_state에 is_alive 전달 |
| `acp/web/templates/...` | 상태 배지·실행명령·last_event 컬럼 |

[아키텍처 결정] (가장 자주 틀리는 지점)
- **osPid는 홀딩 판별자가 아니다.** 실측 결과 osPid=툴콜 서브프로세스(휴지 시 항상 부재, 살아있어도 hung orphan
  가능). osPid는 오직 **RUNNING 확인용 양성 신호**. 홀딩은 **last_event(jsonl) + age**로 판정한다.
- **HOLDING ≠ STALE.** HOLDING=되살릴 수 있는 멈춤(task_complete 후 망각 또는 승인대기). STALE=stale_ttl 초과 좀비.
- **신호는 마지막 이벤트 타입.** started/complete **카운트 짝맞춤 금지**(휴지인데 OPEN>0 사례 실측). 꼬리 1개로 판정.
- `derive_state`는 **순수함수**: 프로세스 생존조회는 `is_alive` 콜러블 주입(내부 OS 호출 금지).
- jsonl은 **끝부분만** 읽는다(seek/역방향). mtime 변동 시에만 re-tail. 큰 세션 파일 전체 로드 금지.
- conversationId가 머지 키. 동일 cwd 다중 세션은 conversationId로 분리(중복 금지).

[불변 원칙]
- read-only(앱 파일 수정 금지). 파싱 실패→UNKNOWN+경고, silent fallback 금지(C3).
- 수집·판정에 LLM 0회.
- Timebox: 홀딩 판정 로직이 3회 안 풀리면 멈추고 실패 로그+원인+다음 접근 보고.

[검증/DoD]
1) 결정 테스트(`tests/`): 픽스처(chat_processes.json 샘플 + rollout jsonl 샘플)로
   - 결정테이블 각 행을 last_event 픽스처 + 시간 mock + fake is_alive로 단정:
     RUNNING(task_started+alive) / LIVE·IDLE·HOLDING·STALE(task_complete+age 경계) /
     HOLDING(approval_request) / HOLDING(task_started+dead+age>hold) / ERROR(task_aborted) / UNKNOWN(미파싱).
   - **DONE은 Codex 픽스처에 두지 않는다**(턴 단위 task_complete ≠ 세션 완료).
   - CodexCollector 머지(두 소스 conversationId 결합), last_activity=jsonl ts 우선, 깨진 줄·미지원 스키마 → UNKNOWN.
2) ★Live smoke(킬러 검증)★: 실제 `~/.codex` 대상 1회 수집 → 상황판에 현재 Codex 세션들이 프로젝트(cwd)·
   실행명령·상태로 표출. **의도적으로 입력대기로 멈춘 세션이 HOLDING으로 잡히는지** 캡처(스크린샷/JSON 로그 artifact 경로).
3) `pytest -q` 통과(명령+결과). 
4) 리뷰: Nitpicker local LLM PASS(repo 래퍼 우선). 별도 Reviewer 세션은 L2 설계 변경, P1/P2급 리스크, 사용자 요청, 또는 Nitpicker가 판단하기 어려운 아키텍처 이슈가 있을 때 수행. REJECT→수정→재실행.
5) Sync-Out: PHASE.md frontmatter(current_phase=P1, phase_status=done, updated_at) +
   HANDOFF 갱신 + `docs/lessons/p1-codex-holding.md` append(WHY/LESSON: 홀딩 판정 신호조합). 커밋 사용자 확인 후. `git tag acp-phase-1`.

[완료 보고] 변경 파일 / 홀딩 판정 신호조합·부착 위치 / 검증(명령+출력+Live smoke artifact) /
PASS는 어디까지·NOT CLAIMED(Claude·Cursor 미수집, 알림 미구현)·가정 / P2 준비 상태.
