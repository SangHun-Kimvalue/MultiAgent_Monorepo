작업: Agent Control Plane — Phase P1.5 (P1 리뷰 수정 — **홀딩/생존 판정 버그 패치**). ★실데이터에서 RUNNING/HOLDING 판정이 실제로 동작하도록 결정테이블을 고친다★. 한국어로 응답/주석.

[브랜치] 단일 브랜치 `acp-v1` 유지. 새 브랜치 금지. 완료 시 기존 `acp-phase-1` 태그를 이 커밋으로 이동(`git tag -f acp-phase-1`). 시작 전 `git status`.

[전제] P1 완료·커밋된 상태에서 시작 — **시작점: `acp-v1` @ `0a278a5`**(tag `acp-phase-1`). 즉 `CodexCollector`/`derive_state` 결정테이블/`is_pid_alive`/`SessionRecord.last_event`/픽스처(repo 내 fake codex home)/테스트 60개가 이미 통과 중이다. 아직이면 P1부터.
- ⚠️ 기존 `0a278a5` 커밋 메시지에 `Co-authored-by: Copilot ...` 트레일러가 섞여 있다(사용자 규약 위반). **너의 P1.5 커밋에는 어떤 Copilot/Co-authored-by 트레일러도 절대 넣지 마라.** 과거 커밋의 트레일러 제거는 Planner가 후속 정리하므로 건드리지 말 것.

[배경 — 왜 P1.5가 필요한가] P1을 독립 리뷰한 결과 **MAJOR 1 + MINOR 3**이 나왔다. 핵심은 "킬러 기능(진행중 턴의 RUNNING/HOLDING 판정)이 실데이터에서 무력화"된다는 것이다. 테스트 60개가 통과하는 이유는 픽스처가 `task_complete`로 끝나는 휴지 세션만 담아서 구멍을 못 잡기 때문이다. 이 페이즈는 **그 구멍을 메우는 surgical patch + 테스트 보강**이다. 신규 기능 추가 금지.

[SoT — 반드시 읽을 것]
- `AgentControlPlane/discovery/design.md` "State Transitions" truth table(실측 신호모델 SSoT) — 턴 생명주기 `task_started → agent_message → token_count → task_complete`.
- `AgentControlPlane/PHASE.md` §0 결정 로그(홀딩 판정 = 마지막 event_msg + age).
- `AgentControlPlane/acp/liveness.py`(현재 결정테이블), `AgentControlPlane/acp/collectors/codex.py`(`_read_last_event`/`_tail_lines`).
- `AgentControlPlane/tests/test_liveness.py`, `tests/test_codex_collector.py`(픽스처 구조).

[전략 리마인더] minimal-first, 버그 패치 한정. 기존 통과 테스트(60개)와 P0 호환 동작을 **깨지 말 것**. 결정테이블의 의미만 정확히 일반화한다. 새 상태/새 수집기/알림은 손대지 않는다.

[이번 범위]

★ MAJOR-1 [필수] — 진행중 턴 판정 일반화 (`acp/liveness.py`, 현재 약 :84 `if last_ev == "task_started":`)
문제: 결정테이블이 "진행중 턴"을 `last_event == "task_started"` **단일 리터럴**로만 매칭한다. 그러나 실제 진행중 세션의 마지막 `event_msg`는 대개 `agent_message` / `token_count` / `patch_apply_end` / `context_compacted`다(`task_started`는 턴 맨 앞 1회뿐). 이들은 84번·92번(task_complete) 분기를 **모두 건너뛰고** 103-110 시간폴백(LIVE/IDLE/UNKNOWN만)으로 빠져 **RUNNING/HOLDING을 영영 못 만든다**.
수정: "완료/에러/승인 이외의 모든 non-None `last_event`"를 **in-turn(턴 진행중)** 으로 일반화한다. 권장 구조(순서 중요):
1. `error`/`task_aborted` → ERROR (기존 early-return 유지)
2. `*approval_request` → HOLDING (기존 early-return 유지)
3. `last_activity is None` → UNKNOWN (기존 유지)
4. age 계산, 음수 → UNKNOWN (기존 유지)
5. **`task_complete` → 나이 기반**(LIVE/IDLE/HOLDING/STALE) ← in-turn 분기보다 **먼저** 둘 것
6. **그 외 `last_event is not None`(= in-turn: task_started/agent_message/token_count/patch_apply_end/context_compacted/user_message 등)** →
   - `is_alive(running_pid)` True → RUNNING
   - dead + age > hold_threshold → HOLDING
   - dead + age ≤ hold_threshold → RUNNING(잠정)
7. `last_event is None` → (MINOR-2의 raw_status 폴백) → 시간 폴백(LIVE/IDLE/UNKNOWN)
- 핵심: 6번을 `task_started` 한정에서 "완료 이벤트(task_complete)가 아닌 모든 진행 이벤트"로 넓힌다. error/aborted/approval은 이미 1·2에서 반환되므로 6번에 도달하지 않는다.
- docstring 결정테이블(파일 상단 7-19행)도 `task_started` 행을 **`in-turn`(task_complete/error/approval 이외의 event_msg)** 로 문구 보강.

★ MINOR-2 [필수] — raw_status 폴백 위치 (`acp/liveness.py` 현재 :63-69)
문제: `raw_status` 부분문자열 폴백(done/complete→DONE, error→ERROR)이 `last_event` 분기보다 **앞**에 있다. Codex는 `raw_status=None`이라 지금은 안전하지만, last_event가 있는데 raw_status에 "complete"가 섞이면 DONE 오반환 잠재버그.
수정: raw_status 폴백 블록을 **last_event in-turn 분기(MAJOR-1의 6번) 뒤, 최종 시간 폴백 직전**으로 이동. 이렇게 하면 `last_event is None`(FakeCollector·non-Codex 하위호환)일 때만 도달한다. **P0 호환 테스트 `test_done_raw_status`/`test_error_raw_status`(둘 다 last_event=None, last_activity 존재)가 계속 통과해야 한다** — 이동 후 반드시 재확인.

★ MINOR-3 [필수] — tail 윈도우 확대 폴백 (`acp/collectors/codex.py` 현재 `_TAIL_LINES=30` :41, `_read_last_event` :240-265)
문제: 꼬리 30줄만 본다. 마지막 `event_msg`가 30줄 밖(예: 대량 `response_item` 뒤)이면 `None` 반환 → UNKNOWN 오분류.
수정: `_read_last_event`에서 첫 윈도우(30줄)로 event_msg를 못 찾으면 **윈도우를 1회 확대 재시도**(예: 500줄 또는 256KB 바이트 상한). 무한 확대 금지 — 상한 1회까지만. 전체 로드 절대 금지(끝부분 seek 유지).

☆ MINOR-4 [선택/지양] — `is_alive` 기본값(`acp/liveness.py` :41 `lambda _: False`)
리뷰는 required kwarg 권장이나, **이번엔 손대지 말 것**. P0 호환 테스트 다수가 `is_alive` 미주입으로 호출하므로 required로 바꾸면 16개+ 깨진다. 기본값은 유지하되, 주석으로 "주입 누락 시 PID 신호 없음(보수적 False)" 의도만 1줄 명시. (out-of-scope에 준함)

[검증/DoD]
1) ★테스트 보강(이게 이 페이즈의 핵심)★ `tests/test_liveness.py`에 **in-turn 케이스** 추가:
   - `last_event="agent_message"`, is_alive=True → **RUNNING**
   - `last_event="token_count"`, is_alive=False, age > hold → **HOLDING**
   - `last_event="patch_apply_end"`, is_alive=False, age ≤ hold → **RUNNING(잠정)**
   - (회귀 방지) 기존 `task_started` 케이스 3개는 그대로 통과해야 함.
2) `tests/test_codex_collector.py` 픽스처에 **진행중 턴 jsonl**(마지막 줄 = `event_msg` payload.type ∈ {agent_message, token_count}) 1개 추가 → `_read_last_event`가 그 타입을 반환하는지 단정. (기존 task_complete 픽스처는 보존.)
3) (MINOR-3) event_msg가 30줄 밖에 있는 긴 jsonl 픽스처로 tail 확대 폴백이 동작하는지 단정.
4) `pytest -q` 통과(명령+결과 붙일 것). 기존 60개 + 신규 케이스 = 전부 green.
5) ★Live smoke 재확인★: 실제 `~/.codex` 1회 수집 → 진행중 세션이 있으면 RUNNING/HOLDING으로 잡히는지(이전엔 시간폴백으로 샜음) JSON/스크린샷 artifact 경로 보고. 진행중 세션이 없으면 "현재 전부 휴지(task_complete)"임을 명시.
6) 리뷰: Nitpicker local LLM PASS(repo 래퍼 우선). 별도 Reviewer 세션은 L2 설계 변경, P1/P2급 리스크, 사용자 요청, 또는 Nitpicker가 판단하기 어려운 아키텍처 이슈가 있을 때 수행. 필요 시 **결정테이블 분기 순서**와 **P0 호환 테스트 보존**을 집중 확인.
7) Sync-Out: `docs/lessons/p1-codex-holding.md`에 append(WHY/LESSON: "in-turn 판정을 task_started 리터럴로 좁히면 실데이터에서 무력화 — 마지막 event_msg는 보통 agent_message/token_count"). HANDOFF 갱신(P1.5 done + 실제 커밋 SHA). `discovery/design.md` truth table의 `task_started` 행 표현을 `in-turn`으로 보강. **커밋은 사용자 확인 후**, `git tag -f acp-phase-1` 이동.

[불변 원칙]
- read-only(앱 파일 수정 금지). 파싱 실패→UNKNOWN+경고, silent fallback 금지(C3).
- `derive_state` 순수함수 유지(now·cfg·is_alive 전부 주입, 내부 OS/시계 호출 금지).
- 수집·판정에 LLM 0회.
- **신규 기능/새 상태/새 수집기 추가 금지** — 버그 패치 + 테스트 한정.
- 커밋 메시지에 Copilot/Co-authored-by 트레일러 금지.
- Timebox: 결정테이블 수정이 3회 안에 안 풀리면(기존 테스트가 계속 깨지면) 멈추고 실패 로그+원인+다음 접근 보고.

[완료 보고] 변경 파일 / MAJOR-1 분기 재구성 전후 의사코드 / 추가한 in-turn 테스트 목록 / `pytest` 출력 / Live smoke artifact(진행중 세션 유무) / PASS는 어디까지·NOT CLAIMED·가정 / P2 준비 상태.
