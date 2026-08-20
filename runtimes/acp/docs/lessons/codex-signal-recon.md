# Lessons — Codex 생존/홀딩 신호 정찰 (2026-06-09)

> append-only. WHAT이 아니라 WHY/LESSON.

## L1. osPid는 홀딩 판별자가 아니다 (원 설계 가정이 틀렸다)
- **WHAT:** `~/.codex/process_manager/chat_processes.json`의 엔트리는 `conversationId:turnId:call_xxx` +
  `command` + `osPid` 구조. 즉 **개별 툴콜(셸 명령) 서브프로세스**의 PID다(앱 전체도, 턴도 아님).
- **증거:** 실측 9개 중 8개 osPid 죽음. 살아있는 1개는 어제 시작된 `ssh ... ConnectTimeout`의 **hung orphan**.
- **WHY 중요:** 원 설계는 "osPid 부재 + 활동멈춤 + 미완료 → HOLDING"이었다. 그러나 휴지 상태(턴 끝나고 입력 대기)에선
  osPid가 **거의 항상 부재**다 → 양성 판별 불가. 게다가 hung orphan을 RUNNING으로 오판할 위험.
- **LESSON:** osPid는 **RUNNING 확인용 양성 신호로만** 쓴다(살아있으면 능동 실행). 부재는 중립. 단독 판정 금지.

## L2. 홀딩/생존 신호 = jsonl "마지막 이벤트 타입" + 경과시간 (카운트 짝맞춤 금지)
- **WHAT:** `rollout-*.jsonl`의 턴 생명주기 = `task_started → agent_message → token_count → task_complete`.
  마지막 `event_msg.payload.type`가 세션의 현재 휴지/실행 상태를 알려준다.
- **함정:** `task_started`/`task_complete` **카운트 짝맞춤으로 "열린 턴"을 판정하면 틀린다.** 실측에서 휴지 세션인데
  started−complete=21인 사례(abort/compaction/중단 누적). → 반드시 **꼬리 1개 이벤트**로 판정.
- **LESSON:** `task_complete`(휴지) + age로 LIVE/IDLE/HOLDING/STALE 승급. `*_approval_request` 또는
  `task_started`(완료없음)+osPid死+age>hold = 턴중단 홀딩. raw_status 문자열매칭(done/error)은 취약 → 폐기.

## L3. DONE은 Codex 세션에 적용되지 않는다
- **WHAT:** `task_complete`는 **턴 단위**이지 세션 종료가 아니다. Codex 세션엔 "세션 완료" 표식이 없다.
- **LESSON:** 8상태 enum의 `DONE`은 Codex 세션 상태엔 미사용. 세션은 완료 없이 `STALE`로 늙는다.
  `DONE`은 **PHASE.md `phase_status=done`** 표시에만 의미. 알림도 Codex DONE 전이 없음.

## L4. last_activity 출처 + I/O 절감
- chat_processes `updatedAtMs`는 툴콜 단위라 세션 활동을 지연/과소반영. **jsonl 마지막 이벤트 ts가 더 정확.**
- 매 폴마다 모든 jsonl을 tail하면 I/O 부담 → **jsonl `mtime`이 직전 폴 이후 바뀐 세션만 re-tail**.

## L5. 확정 임계값 (상훈, 장시간 작업 워크플로)
- idle=300s(5분) / hold=900s(15분) / stale_ttl=3600s(60분) / poll=15s. 전부 config 조정가능, poll 배수.
- 홀딩 알림 = 전이당 1회. 무한대기 허용(반복 리마인드 없음).

## L6. 방법론 교훈 — "추정 금지, 실측" 이 한 페이즈를 절약했다
- P1 앞에 별도 관측 PoC(P1a)를 둘 뻔했으나, Planner 세션의 즉석 정찰(파일 실측)로 신호모델을 확정 →
  P1a 불필요. 남은 미검증(턴중단 승인대기 케이스)만 P1 Live smoke로 확인하면 된다.
