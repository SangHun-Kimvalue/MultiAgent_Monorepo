---
name: phase-cycle-orchestrator
description: >
  바깥 루프 오케스트레이터. 사용자가 "N페이즈 진행", "다음 페이즈까지 돌려", "phase-cycle-orchestrator로 진행",
  "설계→리뷰→구현→리뷰→커밋 게이트를 배선해"처럼 요청할 때, phase0/phased-handoff/ztr run-phase/preen/zrt-phase-commit을
  연결해 페이즈 간 산출물 전달, 실행 어댑터 치환, 페이즈 시작·종료의 휴먼 게이트 2곳, PASS/NOT CLAIMED 보고를 운전한다.
---

# Phase Cycle Orchestrator

이 스킬은 바깥 루프를 운전한다. 판단은 LLM 세션과 사람에게 두고, 스킬은 배선·전달·게이트·알림만 자동화한다.

## Canon And Boundaries

먼저 읽는다:
- `methodology/docs/PHASE_CYCLE_ORCHESTRATOR_DESIGN.md`
- `methodology/docs/EXECUTION_ADAPTER_CONTRACT.md`
- 프로젝트의 `.claude/phased-handoff.config.md` 또는 `methodology/config/project.config.example.md`
- `methodology/METHODOLOGY.md`와 `methodology/MULTI_AGENT.md`

불변:
- R5: 코드나 스킬이 LLM 산출물의 의미를 판정하지 않는다. 분기는 `status`, `exit_code`, `not_claimed`만 사용한다.
- 오케스트레이터는 설계리뷰 권위를 재정의하지 않는다. 설계+프롬프트 저작의 게이트는 `phased-implementation-handoff` §5.5를 호출해 상속한다.
- 오케스트레이터는 바깥 루프 운전자다. 직접 구현하거나 자기 설계·구현을 승인하지 않고, Reviewer 산문의 의미를 코드/스킬로 판정하지 않는다.
- 구현리뷰 기준은 "이미 캐논에 완결되어 있다"고 주장하지 않는다. ASM-3 상세 체크리스트는 임시 기술부채다.
- 구현리뷰 leg에는 `MULTI_AGENT.md`의 역할 독립성 + `phased-implementation-handoff` §8 체크리스트 + M2 5필드를 **review artifact 파일**로 주입한다.
- 큰 diff, 파일 본문, 리뷰 요청, 다중 문서 컨텍스트를 argv에 직접 싣지 않는다.
- 반복 실행 경로는 shell 문자열이 아니라 runner/subprocess argv 배열로 구성한다. PowerShell 변수에 JSON argv를 담아 재전달하지 않는다.
- exit code 0은 leg process 성공일 뿐, 의도한 파일 변경 성공을 자동 증명하지 않는다. driver는 changed paths/diff 존재 여부를 사실 증거로 별도 보고한다.
- LLM prose 기반 의미판정과 autofix loop는 금지한다. 다만 구조화 disposition 기반 T10 corrective는 승인된 scope/timebox 안에서 허용하며, 사람 확인은 페이즈 시작·종료 경계에서 받는다.

## Phase Loop

사용자가 `N`을 지정하지 않으면 1페이즈만 준비하고, 다음 페이즈 진행 여부를 휴먼 게이트에서 묻는다.

각 페이즈마다:

1. Sync-In
   - HANDOFF, roadmap, lessons, current diff, project config를 읽는다.
   - role assignment와 active skills/tools를 확인한다.
   - 전제나 scope가 갈리면 실행 전에 질문한다.

2. Design And Prompt
   - **작업 등급과 리뷰 라운드 예산을 먼저 선언한다.** 값·종료 경계·초과 시 처분의 권위는
     **캐논 `METHODOLOGY.md` §3 Timebox**다(여기서 숫자를 다시 정의하지 않는다).
     오케스트레이터가 할 일은 셋: ① 이 슬라이스의 등급을 정해 설계에 적고 ② 그 등급의
     리뷰 예산을 **선언한 뒤** ③ 라운드마다 소진량을 기록한다. 예산은 **슬라이스 단위**이며
      새 finding이 나왔다고 리셋하지 않는다. 자동 수정 라운드(`fix_rounds_max`)와는 다른 축이다.
      **카운터 SoT = phase 진행 문서의 단일 `review-budget` fenced block**이다(캐논 §3 · 결정 A5 —
      **별도 상태 파일을 만들지 않는다. SoT 분산이 더 큰 위험이다**). Planner는 Step 2에서 진행 문서 상단에
      아래 블록을 만든다. 블록이 0개거나 2개 이상이면 fail-closed다.

      ```review-budget
      slice_id: <생성 후 불변>
      work_grade: L0|L1|L2
      rounds_consumed: 0
      doc_rounds_consumed: 0
      ```

      **예산은 두 축이다**(캐논 §3, A8). **`--gate` 값과 카운터는 1:1로 고정**한다:

      | `--gate` | 카운터 | 예산 | 대상 |
      |---|---|---|---|
      | `output`(기본) | `rounds_consumed` | L0 2 / L1 3 / L2 4 | **구현 diff** 심사 |
      | `input` | `doc_rounds_consumed` | L0 1 / L1 2 / L2 3 | **문서·계획·프롬프트** 심사 |

      **축은 심사 대상이 가른다 — 호출자의 편의가 아니다.** 문서를 심사하면서 `--gate output`을
      고르면 구현 diff 축이 소비되고 문서 축이 남아 **축 간 잔여 차용 금지가 우회**된다.
      **두 축은 서로의 잔여를 빌려오지 않으며**,
      한 축을 소진했다고 같은 대상을 다른 축으로 재분류해 다시 심사하는 것은 **증축**이다.
      `budget_limit`은 `work_grade`에서 **파생**하며 문서 신고값을 신뢰하지 않는다.

      **집행은 사후 대조가 아니라 호출 전 차단이다.** 독립 Reviewer 호출은 반드시
      `phased-implementation-handoff/scripts/review_budget_preflight.py`를 통과한다(직접 호출 금지):
      `python <phased-implementation-handoff-skill-root>/scripts/review_budget_preflight.py --doc <phase 진행 문서> --slice <slice_id> --gate output|input -- <reviewer 명령>`
      ⚠ 스크립트는 **`phased-implementation-handoff` 스킬이 소유**한다. 이 스킬(`phase-cycle-orchestrator`)
      기준 `<skill-root>`로 풀면 파일을 찾지 못하고, 그대로 직접 Reviewer를 호출하면 **예산 집행이 우회**된다.
      preflight가 잠금 안에서 카운터를 재검증·증가·내구 저장한 **뒤에만** Reviewer가 뜬다.
      `next_round > budget_limit`이면 **Reviewer를 호출하지 않고** exit 3으로 끝난다.
      저장 실패(exit 4)는 호출 금지, launch 실패(5)·잠금 해제 실패(6)는 **소비를 유지**한 채
      Human Gate로 돌린다. 판정은 **exit code로만** 하고 산문을 재해석하지 않는다(R5).
      해당 축의 카운터 줄이 없으면 fail-closed다 — 줄을 지워 예산을 리셋할 수 없다.
      **소비된 라운드를 되돌리는 경로는 만들지 않는다**(증축 금지). 인프라 실패의 복구는
      자동 증축이 아니라 **Human Gate disposition**이다.
   - 실행 전에 사람/Planner가 `task_grade`를 `L0|L1|L2` enum으로 확정한다. 코드나 스킬이
     자연어에서 등급을 추론하지 않으며, 누락·모호한 값은 Claude reviewer 모델 선택에서 Opus로 취급한다.
   - `phased-implementation-handoff`를 사용해 설계, 구현 프롬프트, 검증 기준을 만든다.
   - 계획 리뷰는 `phased-implementation-handoff` §5.5 입력 게이트로 한 번 충족한다. 구현 후 출력은 §8의 내장 Reviewer + Nitpicker 2-leg 게이트를 유지한다(오케스트레이터가 게이트를 자체 보유·재정의하지 않는다).
   - `cross-session-plan-review`를 중복 호출하지 않는다. 이미 작성된 외부 계획을 따로 검토하는 경우에만 사용한다.
   - 라운드 예산과 별도로 `METHODOLOGY.md §3.1`의 Context / Token Budget을 적용한다. 리뷰 request에는
     `context_mode: initial|delta`, required-read 경로, 각 payload 문자 수를 기록한다. 상한 초과면
     dispatch 전에 compact·shard하며, 큰 입력을 이유로 모델만 승급하지 않는다.

3. Human Gate 1: Phase Start
   - 설계 PASS, scope, NOT CLAIMED, 다음 실행 명령 요약을 보여준다.
   - 사람이 go 하지 않으면 안쪽 루프를 실행하지 않는다.
   - go 시 acceptance contract를 동결한다. 이후 Reviewer가 새 acceptance/invariant/test matrix를 blocker로
     추가하지 않는다. 보안·인증·데이터 손실·재현된 false PASS 때문에 변경이 필요하면 현재 slice를
     중단하고 새 scope로 Human Gate 1을 다시 받는다.

4. Prepare Artifacts
   - 구현 프롬프트를 `{prompt_file}` artifact로 저장한다.
   - 구현리뷰 기준 bundle을 `{review_artifact}` artifact로 저장한다.
   - changed paths가 필요하면 `{changed_paths_file}` artifact로 저장한다.
   - initial bundle은 canonical anchor와 frozen contract를 path+digest로 참조하고 본문을 복제하지 않는다.
     corrective review bundle은 unresolved finding, disposition, 이번 delta, 새 검증 결과만 포함한다.
     resolved history와 raw logs는 저장 경로만 전달한다.
   - artifact는 UTF-8로 쓰고, 외부 JSON/YAML 입력은 UTF-8-SIG를 허용한다.
   - reviewer/provider의 `working_directory`와 `artifact_root` 안에서 읽을 수 있는 경로에 artifact를 둔다.

5. Build Inner-Loop Command
   - project config의 "안쪽 루프 백엔드" 블록을 읽는다.
   - `EXECUTION_ADAPTER_CONTRACT.md` §5에 따라 `{name}` 토큰을 단일 pass로 치환한다.
   - provider capability의 `model`, `approval_policy`, `sandbox_policy`, `working_directory`, `artifact_root`, `required_env`, `dangerous_bypass_required_for_dogfood`를 실행 전에 확인한다.
   - Claude가 **독립 Reviewer leg**인 경우에만 공식 Claude Code CLI argv를 사용하고 각 호출에
     `--model`을 명시한다. 기본(L1/L2 및 등급 누락·모호)은 `opus`, L0 리뷰 작업만 사람이
     명시적으로 `sonnet`을 선택한다. 직접 SDK/HTTP/API transport로 바꾸지 않는다.
   - 기본 L1/L2 run의 `--timeout 900`과 사람이 명시한 L0 run의 `--timeout 300`은 reviewer
     전용이 아니라 그 run의 implementer/reviewer/mechanical/test 등 모든 leg 각각에 적용되는
     전역 per-leg wall timeout이다. 이번 단계에서는 등급 기반 timeout을 자동 선택하지 않는다.
   - execution-preflight v1은 `opus`/`sonnet` moving profile과 선택적 단일
     `--output-format json`을 허용해 기본 Opus reviewer binding과 정렬됐다. 이는 source validation
     범위만 갱신한 것이므로 자동 end-to-end Opus 실행은 별도 live relay 증거 전까지 `NOT CLAIMED`다.
     설치본 갱신도 별도 승인·검증 전까지 `NOT CLAIMED`다. 상세 정본과 후속 경계는
     `EXECUTION_ADAPTER_CONTRACT.md` §3.3이다.
   - 미정의 토큰이 남으면 실행 전 `BLOCKED`로 멈춘다.
   - 조건부 flag는 빈 문자열로 치환하지 말고 argv 원소를 삽입하거나 생략한다.
   - 경로는 repo root 기준으로 정규화하고 argv 배열의 한 원소로 전달한다.
   - 반복 실행은 Python/Node runner의 argv 배열로 만든다. JSON argv는 상위 argv의 원소 하나로 전달하고, PowerShell one-liner에 재직렬화하지 않는다.
   - dogfood 전용 위험 bypass가 필요하면 capability에 `dangerous_bypass_required_for_dogfood: true`로 명시하고 보고서에 범위를 남긴다. 범용 기본값처럼 숨기지 않는다.

6. Run Inner Loop
   - **실행 단계 모드**: 각 leg·단계는 시작 전에 `completion_mode=unattended|interactive_checkpoint`를 명시한다. `unattended`에서는 확인창 가능 표시 호출을 하지 않고 비대화형 검증만 수행하며, 불가한 검증은 증거와 `NOT CLAIMED` 또는 명시적 human checkpoint로 넘긴다. 세부 어댑터 예외·저장·전달 정책은 [LESSON-M061](../phased-implementation-handoff/references/unattended-completion.md)을 따른다.
   - **구현 위임의 본질 = 독립 Implementer 컨텍스트에 넘기는 것.** 아래 backend는 그 본질을 실현하는 *수단*이며, backend 하나가 막혀도 "구현 위임 자체가 불가"가 아니라 다음 backend로 내려간다(리뷰 leg 폴백 §5.5/§8과 **대칭**). relay는 backend 1종일 뿐 위임의 정의가 아니다.
   - **폴백 허용 사유 = "구현 시작 전 transport/capability 실패"에 한정**(classifier 거부·실행권한/sandbox 충돌·CLI 시작 불가). **Implementer가 실제 실행된 뒤의 코드·검증·리뷰 실패**(테스트 red·`CHANGES_REQUESTED`·repo 오류)는 backend 장애가 아니라 **기존 verdict대로 중단**한다 — 다른 backend에서 같은 구현을 반복해 fail-fast·timebox를 우회하지 않는다.
   - **Implementer backend 폴백 위계(4단계 — 계약 §3.1과 동일 번호, 막히면 한 칸씩만 내려간다)**:
     - ① `ztr run-phase` relay 기본 — config argv JSON으로 implementer/reviewer/mechanical leg 구동.
     - ② relay **안전 모드 재시도** — bypass가 classifier에 막히면 `workspace-write`로(파일편집만; self-verify 셸은 `--test-cmd` leg가 대행).
     - ③ ②도 transport로 막히면 **Agent 툴 subagent를 독립 Implementer 세션으로 구동** + **명시 어댑터로 완주**: subagent는 ztr Envelope을 내지 않으므로 **subagent 결과 자체는 PASS가 아니라 "구현 산출 대기"**다. 반드시 `[Implementer 산출 → 독립 Reviewer leg → Mechanical leg → 표준 verdict(enum/exit)]`를 완주해야 Step 7/Human Gate 2로 갈 수 있다. **Planner가 subagent의 자연어 완료 보고를 의미 판정해 PASS로 바꾸지 않는다(R5).**
       Agent 도구는 `fork_turns="none"` 또는 필요한 최소 recent turns를 기본으로 하고, 전체 transcript 상속은
       bounded bundle로 표현할 수 없는 근거가 있을 때만 예외적으로 사용한다.
     - ④ ①②③이 모두 불가할 때만 사용자 수동 Implementer 세션 위임(최후). ③(subagent+어댑터)를 건너뛰고 곧장 수동으로 후퇴하지 않는다.
   - **cross-lineage 검증(형식적 swap 금지, 캐논 §8 상속)**: 기본 요구는 **실제 reviewer 계열 ≠ 실제 implementer 계열** AND **reviewer 컨텍스트 ≠ implementer 컨텍스트**다. 이를 절차가 아니라 **실행 전·후 실측**으로 확인하고 — 완료 보고 evidence에 **양쪽 실제 계열·컨텍스트/session id와 비교 결과**를 필수 기록한다("계열이 안 바뀌었다"는 이유로 교차검증을 건너뛰지 않는다). cross-lineage를 확보할 수 없을 때만 §8 폴백 위계의 ③(같은 계열 독립 컨텍스트)로 내려가되, 이는 **예외 없는 불변식이 아니라 캐논 §8이 정의한 열화 모드**이므로 review 기록에 `same-lineage(계열 독립성 미확보)`를 명시한다(오케스트레이터는 §8 권위를 재정의하지 않는다). **Planner의 Reviewer 겸임 금지 = 컨텍스트 분리는 예외 없는 조건**(계열과 별개).
   - relay 경로에서: implementer, reviewer, mechanical/preen leg를 config의 argv JSON으로 전달한다.
   - Claude reviewer의 stdout silence만으로 hang/`BLOCKED`/강제 종료/동일 조건 재시도를 판정하지
     않는다. terminal process 상태 또는 전체 wall timeout만 종료 근거이며, `stream-json`은 선택적
     관측 수단일 뿐 실시간 flush나 liveness의 권위가 아니다.
   - resume는 `--session-map`, `--implementer-resume`, `--reviewer-resume`, `--*-resume-profile` 정책을 따른다.
   - `auto` resume 실패는 새 세션 폴백 + 맥락 손실 경고로 보고한다.
   - 명시 session id 실패 또는 다른 id 캡처는 `BLOCKED`로 보고한다.
   - Reviewer leg가 종료되면 verdict와 무관하게 Orchestrator가 제어권을 회수한다. Reviewer가 수정이나 다음 역할을 이어서 수행하게 두지 않는다.

7. Route Envelope
   - stdout의 단일 Envelope JSON을 읽는다.
   - `PASS`: **곧바로 휴먼 게이트 2로 가지 않는다.** 먼저 `phased-implementation-handoff/SKILL.md` **§8.5 통합 게이트**를 통과해야 한다(보고 verdict 정직성·로드맵 정합·문서 갱신 + **잔여 목록 stale 차단 게이트**). **집행자는 §8.5의 모드별 분담을 따르고**(수동 모드=Implementer 집행 / 릴레이·오케스트레이터 모드=Planner 집행), **Planner는 그 집행 증적을 확보해 최종 판정한다**. §8.5 성공 조건을 **전부** 충족했을 때만 휴먼 게이트 2로 이동하고, `BLOCKED`(미열거 active-state 후보·설명 불가 hit·집계 불일치 등)면 **커밋 승인을 요청하지 않고** 구현 세션 또는 입력 정의로 되돌린다. Step 9 Sync-Out은 이 게이트의 **결과 기록**만 담당한다(거기서 처음 집행하면 이미 커밋 뒤라 늦다).
   - `CHANGES_REQUESTED`는 Envelope의 exact `status`/`exit_code` 조합으로만 route한다. 자연어 완료 보고나 finding 본문을 PASS로 재분류하지 않는다.
   - 현재 run은 종료하고 Orchestrator가 모든 finding을 기존 four-way `methodology/artifacts/finding-disposition.md`에 기록한다. 페이즈 시작 승인이 유효하고 scope/timebox가 유지되면 corrective round는 별도 Human Gate 없이 이어간다.
     - 실제 재현 없는 P2/P3는 blocker나 corrective trigger가 아니라 backlog다.
     - Reviewer가 frozen acceptance contract 밖의 일반 강건성을 요구하면 `REJECT_OVERENGINEERING` 또는
       `DEFER_OUT_OF_SCOPE`로 처분한다.
     - Mechanical finding은 `severity / evidence_or_repro / impact / recommendation`이 모두 있어야 수정
       트리거다. 누락되거나 verdict token과 prose가 충돌하면 raw bytes/token은 보존하고 adapter degradation으로만 기록한다.
     - 허용값은 `ACCEPT`, `REJECT_FALSE_POSITIVE`, `DEFER_OUT_OF_SCOPE`, `REJECT_OVERENGINEERING`이다. evidence·rationale·owner가 누락되면 corrective round는 `BLOCKED`다.
     - `REJECT_FALSE_POSITIVE`와 `REJECT_OVERENGINEERING`에는 반증 evidence가 필수다. `DEFER_OUT_OF_SCOPE`는 결함 부정이 아니며 owner와 후속 위치가 필수다.
     - disposition은 수정 대상 선별일 뿐 phase 승인이나 자기 구현 승인이 아니다.
     - 페이즈 시작 승인 reference와 필수 evidence가 없으면 `BLOCKED`다. corrective round별 새 trigger는 요구하지 않는다. 이 workflow gate는 human-presence의 암호학적 증명이 아니며 B-3은 **NOT CLAIMED**다.
     - 기존 `methodology/tools/remediation_adapter.py --human-triggered`를 호출하고 stdout의 structured `PASS/0`만 수용한다. 이 호환 flag는 **페이즈 시작 승인 reference가 존재함**을 기록하며 round별 신규 승인을 뜻하지 않는다. `--accept-leg orchestrator-accepted-review`는 adapter가 만든 accepted-only synthetic report를 선택할 뿐 새 disposition 권위가 아니다.
     - project config의 `fix_rounds_max`를 읽는다. 키가 없으면 기본 `3`; 값은 bool/문자열이 아닌 exact integer `1..5`여야 한다. 범위 밖·중복·모호한 정의는 fix-round 호출 전에 `BLOCKED`다. 최종 범위 권위는 runtime validator다.
      - `reapply-status`로 runtime 원장을 먼저 읽는다. terminal이면 새 round를 발행하지 않고 사람에게 에스컬레이션한다.
      - **순서 고정**: `ztr fix-round` → 결정론 검증(Mechanical·test) → **그 다음에** preflight가
        Reviewer를 호출한다. **preflight는 예약만 하는 도구가 아니라 카운터 저장 직후 Reviewer를
        직접 실행**하므로, 수정 전에 돌리면 예산은 소비되고 **수정 결과는 심사되지 않는다.**
        preflight가 **exit 3(소진)이면 수정본은 미심사 상태 그대로** phase-end Human Gate에 반환한다
        — 그 상태를 PASS로 합성하지 않는다.
      - 독립 Reviewer 호출은 **preflight를 통과해야만** 발생한다. preflight가 진행 문서의
        `review-budget` 블록을 잠금 안에서 재파싱해 `slice_id`·`work_grade`를 대조하고, 해당 축 카운터를
        증가시켜 내구 저장한 뒤 Reviewer를 띄운다. **exit 3(예산 소진)이면 새 round를 발행하지 않고**
        현재 verdict·미해결 finding·NOT CLAIMED를 그대로 phase-end Human Gate에 반환한다(종료 경계).
        블록 중복·형식 위반·등급 불일치는 exit 2로 fail-closed다. **리뷰 예산과 `fix_rounds_max`는 다른 축이라
        어느 하나만 소진돼도 멈춘다**(캐논 §3 중간 에스컬레이션 ⑤·⑥).
      - semantic review terminal 총량이 L0 2/L1 3/L2 4에 도달하면 증축하지 않는다. PASS를 강제하지 않고
        현재 verdict와 finding을 phase-end Human Gate에 반환한다. 허용 예외는 별도 emergency slice다.
      - 같은 시점에 runtime `reapply-status`의 structured exact integer `fix_round_index`, `fix_rounds_max`와 terminal state를 별도 읽어 `fix_round_index <= fix_rounds_max`를 검사한다. runtime 필드 missing/non-integer 또는 fix-round 축 거짓은 `BLOCKED` 또는 `TIMEBOX_EXHAUSTED`다. 두 축을 prose·finding 의미·자연어 완료 보고로 보완하거나 뒤집지 않으며, review budget 값을 runtime 원장에 쓰지 않는다.
      - accepted findings, 다음 round index, max, input digest, NOT CLAIMED는 runtime 원장에 기록하고, 유효한 페이즈 시작 승인 범위 안에서 다음 명령 shape를 정확히 1회 실행한다: `ztr fix-round ... --accept-leg orchestrator-accepted-review --record --max-rounds <N>`.
     - 한 round 안의 while/retry/prose-autofix는 금지한다. 다만 round 종료 후 구조화 verdict가 `CHANGES_REQUESTED/1`이면 새 four-way disposition을 작성하고, scope/timebox가 유지되는 동안 다음 단일 round를 자동 발행한다.
     - fix-round Envelope `PASS/0`이면 **Human Gate 2로 직행하지 않고 Step 7의 §8.5 통합 게이트를 재집행**한다(수정 라운드가 문서를 바꿨을 수 있어 잔여 상태가 새로 stale해질 수 있다). 성공 조건 충족 시에만 Human Gate 2로 간다. `CHANGES_REQUESTED/1`이면 제어권을 회수해 새 disposition으로 다음 round를 준비한다. `BLOCKED/2`, operational `70/124`, scope 확대, disposition 모호성, terminal/timebox 소진 상태만 artifact와 함께 사람에게 에스컬레이션한다.
      - 수정 뒤 원 페이즈와 동일한 결정론 검증 및 독립 Reviewer/Mechanical gate가 모두 구조화 PASS여야 한다.
        **라운드 소비는 preflight가 호출 시점에 이미 기록**했으므로 Planner가 사후에 카운터를 다시 쓰지 않는다
        (이중 기록·사후 조정 금지). Reviewer 종료 뒤 Orchestrator가 제어권을 회수하며,
        natural-language completion은 PASS가 아니다.
      - ⚠ **NOT CLAIMED — 예약·실행·결과의 사후 대조는 아직 없다.** 카운터 저장 직후 프로세스가
        강제 종료되면 라운드는 소비됐는데 Reviewer는 뜨지 않고 `exit 5`도 관찰되지 않는다.
        반대로 preflight를 우회한 직접 호출은 결과 artifact만 남고 카운터가 늘지 않는다.
        **둘 다 현재는 정상 이력과 구별되지 않는다.** 카운터를 되돌리지 않는 **읽기 전용 정합 검사**가
        후속 과제이며, 그 전까지 "카운터가 맞으니 리뷰가 실제로 돌았다"고 주장하지 않는다.
   - `BLOCKED`: blocker, stderr 요약, artifact 경로, 재현 명령을 보고하고 멈춘다.
   - `not_claimed`는 PASS처럼 말하지 않는다.
   - PASS라도 driver evidence를 별도 기록한다: changed paths 또는 diff artifact 존재 여부, session-map 경로/id, prompt/review artifact 경로, run dir, 각 leg envelope 경로.
   - changed paths/diff 존재 여부는 사실 증거로만 보고한다. LLM prose를 파싱해 "요구사항 충족"으로 자동 재분류하지 않는다.

8. Human Gate 2: Phase End
    - 사람이 커밋을 요청할 때만 `zrt-phase-commit`을 사용한다. 커밋하지 않기로 한 결정도 phase-end evidence로 남긴다.
   - 커밋 전 변경 범위, 검증 증거, HANDOFF/lessons/roadmap 상태를 다시 확인한다.
    - AI attribution 없는 한국어 커밋 메시지만 허용한다.
    - **다음 대상을 추천하기 전에 Target Evidence Gate를 집행한다.** 항목·판정 규칙·0건 처리(보류가 기본, 예외 3종)의 권위는 **캐논 `METHODOLOGY.md` §2-1**이다. 오케스트레이터는 후보별 측정을 실제로 돌리고 결과를 그대로 보여준다. 설계·구현은 교차 검토를 받지만 대상 선정은 검토 없이 사람에게 간다.
    - 커밋 여부와 다음 페이즈 진행 여부를 함께 결정한다. 다음 페이즈의 시작 승인은 그 페이즈의 Human Gate 1에서 새로 받는다.

9. Sync-Out
   - **tracked 상태 문서 갱신은 전부 커밋 *전*(Step 7/§8.5)에 끝낸다** — HANDOFF·lessons·결정 로그·ADR/PHASES의 현재 상태뿐 아니라 **진행 SoT의 완료 표시·현재 위치**(DOC_TAXONOMY §3 지정 = `methodology/docs/PHASE_CYCLE_ORCHESTRATOR_DESIGN.md §10`의 완료 페이즈 ✅)도 포함한다. 컴포넌트 로드맵은 포인터만. 커밋 후로 미루면 stale이 이미 커밋됐거나, 게이트 없는 권위 상태 편집이 작업트리에 남는다.
   - **Step 9 Sync-Out은 tracked 상태 문서를 편집하지 않는다**(모두 커밋 전에 끝났다). 커밋 후 상태의 정본은 **커밋 자체와 커밋 메시지, 그리고 커밋 전에 갱신된 HANDOFF·진행 SoT**다 — 새 기록 채널을 만들지 않는다. 다음 페이즈로 이어갈 때 필요한 것(커밋 SHA·Human Gate 결정·§8.5 증적 경로·다음 owner)은 **사람에게 보고**하고, 영속이 필요하면 **다음 페이즈의 커밋 전 문서 갱신**에 포함시킨다.
   - phase SoT/HANDOFF에는 `status / next owner / NOT CLAIMED / evidence pointer`만 남긴다. 라운드별
     review history와 raw verdict는 `.ztr/orchestrator/<phase>/` 같은 phase-local artifact에 두고 본문에 누적하지 않는다.
   - **잔여 목록 stale 차단은 여기가 아니라 Human Gate 2 직전에 이미 집행됐어야 한다** — 정본 = `phased-implementation-handoff/SKILL.md` §8.5의 **Planner 소유 하드 게이트**(active-state anchor 열거·canonical 문자열·표기 변형 검색·집계 재계산·미분류 hit 0, 위반 시 `BLOCKED`). 커밋 후 Sync-Out으로 미루면 이미 stale이 커밋된 뒤다. 여기서는 그 게이트 결과(검색 명령·범위·hit 분류)를 기록만 한다.
    - 다음 페이즈가 결정되면 session-map과 HANDOFF를 유지해 다음 루프로 간다.
   - 중단하면 Sync-Out으로 종료한다.

## Review Artifact Requirements

### Token-efficient input contract

- control bundle 기본 상한은 12,000 UTF-8 문자, diff/hunk payload 상한은 leg당 40,000 문자다.
- 첫 리뷰 이후에는 full design/review history를 다시 보내지 않고 unresolved findings와 delta만 보낸다.
- 큰 diff는 module/file shard로 나누되 동일 canonical bundle을 shard마다 복제하지 않는다. 최종 통합자는
  shard finding 요약과 risk-relevant hunk를 보고, 모든 원문 리뷰를 다시 읽지 않는다.
- test/tool raw output은 파일로 보존하고 prompt에는 command, exit, count, path, digest만 넣는다.
- 상한 초과는 PASS/FAIL 판정이 아니라 dispatch 전 compact/split 신호다. 줄일 수 없으면 사람에게 비용과 이유를 보고한다.

- **리뷰어가 검증을 실행하지 못하면 그 사실이 열화다** — "정직 보고"로 끝내지 않는다.
  read-only sandbox 등으로 reviewer가 게이트를 재실행할 수 없을 때 **무엇을 고를지는
  선택이 아니라 아래 표로 정해진다**(아무거나 고를 수 있으면 규칙이 아니다):

  | 상황 | 처리 |
  |---|---|
  | 제출된 **실행 artifact가 없다**, 또는 정적 근거와 **충돌해 판정이 뒤집힌다** | **독립 실행자를 배선**한다. 못 하면 `BLOCKED`로 올린다 |
  | 재현 가능한 artifact(명령·출력·경로)가 **충분하다** | reviewer는 재실행하지 않아도 되며, 판정에 **증거 범위**(무엇까지 확인했는지)를 명시한다 |

  reviewer 샌드박스가 read-only인 것은 흔하다 — **결과 의존 검증마다 실행자를 강제하면
  평소 리뷰가 환경 문제로 `BLOCKED`에 빠진다.** 승격 기준은 "실행 결과가 **판정을 바꾸는가**"다.

  정적 검토만 반복하면 라운드는 늘어나는데 실행 증거는 한 번도 늘지 않는다.

구현리뷰 artifact는 짧은 argv prompt가 가리키는 파일이어야 한다. 최소 포함:

```text
review_role: independent implementation reviewer
source_author: <implementer/session>
phase_id: <phase>
decision_sources:
  - phased-implementation-handoff §8
  - MULTI_AGENT.md §1.2 / §4
  - METHODOLOGY.md C1-C7
finding_format: severity / finding / evidence_or_repro / impact / recommendation
checklist:
  - attachment accuracy
  - no unintended body edits
  - decision compliance
  - safety and regression risk
  - missing tests or verification gaps
  - thread/race/concurrency risks where relevant
  - over-engineering and under-engineering
not_claimed_boundary:
  - ASM-3 상세 구현리뷰 SSoT는 아직 임시 주입이다.
  - 이 artifact는 기준 주입이며 캐논 완결을 주장하지 않는다.
payload:
  - changed files
  - core diff path or summary
  - verification output summary
  - relevant prompt/handoff excerpts
output_contract:
  - 리뷰 결론은 stdout **마지막 줄에 단독으로** 다음 토큰 하나만 emit한다(마크다운/접두 금지):
    ZTR_VERDICT: PASS | ZTR_VERDICT: CHANGES_REQUESTED | ZTR_VERDICT: BLOCKED
  - relay(verdict_source=stdout_token)가 exit code가 아니라 이 단독 라인 토큰으로 분기한다.
    토큰이 없거나 프로세스가 non-zero exit면 fail-closed BLOCKED.
  - 애매하면 PASS가 아니라 CHANGES_REQUESTED/BLOCKED로 닫는다.
```

Reviewer argv 예시는 짧게 유지한다:

```json
["claude", "-p", "Review artifact: {review_artifact}", "--model", "opus", "--output-format", "json"]
```

reviewer leg는 `--reviewer-verdict-source=stdout_token`(기본)으로 돈다. 따라서 위 output_contract의
`ZTR_VERDICT` 단독 라인을 **반드시** 출력해야 하며, exit code는 verdict로 쓰지 않는다(`claude -p`는
verdict=BLOCKED여도 exit 0). 근거: `EXECUTION_ADAPTER_CONTRACT.md §2.1`.

## Execution Adapter Rules

- 표준 변수는 project config의 목록을 따른다: `{repo_root}`, `{phase_id}`, `{prompt_file}`, `{review_artifact}`, `{implementer_cmd}`, `{reviewer_cmd}`, `{mechanical_cmd}`, `{session_map}`, `{run_output_dir}`, `{record_flag}`, `{changed_paths_file}`, `{implementer_resume}`, `{reviewer_resume}`, `{implementer_resume_profile}`, `{reviewer_resume_profile}`.
- 리터럴 중괄호는 `{{`와 `}}`로 쓴다.
- 값 내부의 `{...}`는 재귀 치환하지 않는다.
- provider capability의 `verified_cli_version`과 `supports_*`는 실측한 것만 믿는다.
- Claude 독립 Reviewer leg의 모델·CLI·무출력·timeout 상세 정본은
  `EXECUTION_ADAPTER_CONTRACT.md` §3.3을 따른다. 이 규칙을 Claude implementer나 기타 호출로 확대하지 않는다.
- stdin prompt 지원은 provider CLI를 ztr 밖에서 직접 호출할 때 미검증으로 본다. ztr relay 내부 stdin은 ztr 계약이다.
- shell 문자열 대신 argv 배열을 우선한다.
- Windows에서 Node 기반 Codex CLI는 node PATH가 필요할 수 있다.
- Gemini는 CLI provider만 허용한다. API key/backend로 우회하지 않는다.
- 드라이버/어댑터 stdout은 utf-8 강제(`sys.stdout.reconfigure(encoding="utf-8")`) 또는 결과를 파일로 캡처해 Read. cp949 콘솔에 envelope·비-ASCII 직접 print 금지(크래시). [LESSON-008/016]

## Reporting

각 checkpoint와 페이즈 종료 때 프로젝트 roadmap/PHASES의 전체 phase ledger를 갱신·보고한다. 필드 스키마는 이 스킬에서 재정의하지 않고 [`MULTI_AGENT.md#phase-ledger-canon`](../../../../MULTI_AGENT.md#phase-ledger-canon)을 따른다.

각 페이즈 종료 보고:

```text
Phase:
Phase ledger: MULTI_AGENT.md#phase-ledger-canon 기준 현재 인스턴스
Design gate:
Inner loop:
Envelope:
Driver evidence:
Human gates:
Artifacts:
Validation:
Reviewer/Mechanical:
PASS:
NOT CLAIMED:
Next:
```

전체 종료 보고:
- 완료한 페이즈 수
- 사용한 session-map 경로
- 생성한 prompt/review artifacts
- PASS와 CHANGES_REQUESTED/BLOCKED 이력
- 휴먼 게이트 결정
- dogfood 전까지 주장하지 않는 항목: full N-phase automation, 절감 정량, 비-ztr backend 다형성, ACP 관제 연동
- 변경 경로/diff 존재 여부와 그 증거 경로. 이는 사실 보고이며 요구사항 만족 판정이 아니다.

## Anti-Patterns

- 리뷰에서 **새 finding이 나왔다는 이유로 라운드 예산을 리셋**하고 `APPROVED`까지 반복한다
  (라운드마다 무언가는 나온다 — 예산은 슬라이스 단위다).
- 슬라이스 **주제를 벗어난 일반 강건성 지적**을 `DEFER_OUT_OF_SCOPE` 없이 그 슬라이스에서
  전부 해결하려 한다.
- **측정하지 않은 채** 다음 대상의 가치·우선순위를 단정한다("사용자에게 안 보인다" 포함).
- 대상의 **전제를 확인하지 않고**(해당 데이터가 실제로 존재하는지) 설계·리뷰를 진행한다.
- 독립성 확보를 이유로 모든 subagent에 전체 대화 이력을 fork한다.
- corrective R2+에 해결된 finding, 과거 Reviewer 산문, full test log와 full diff를 누적 재전송한다.
- payload 상한을 넘긴 채 compact/shard 대신 고비용 모델 호출이나 중복 Reviewer fan-out으로 밀어 넣는다.

- 설계리뷰를 오케스트레이터가 새로 정의한다.
- 구현리뷰 기준이 캐논에 완결되어 있다고 말한다.
- diff나 리뷰 기준을 긴 argv로 전달한다.
- PowerShell one-liner나 shell 문자열에 JSON argv, diff, 리뷰 본문을 넣어 반복 실행 경로로 삼는다.
- provider의 model pin, 승인 정책, sandbox, working directory, artifact root, required env를 capability 밖의 암묵 전제로 둔다.
- dogfood 전용 dangerous bypass를 일반 실행 기본값처럼 숨긴다.
- exit code 0만 보고 파일 변경 성공이나 요구사항 충족을 주장한다.
- 미정의 `{token}`을 빈 문자열로 바꾼다.
- `CHANGES_REQUESTED` 뒤 four-way disposition·유효한 페이즈 시작 승인 reference·두 축의 structured budget 검사 없이 자동 수정 루프를 시작하거나 Reviewer가 직접 수정한다.
- rejected/deferred finding을 `ztr fix-prompt`에 섞거나 corrective round의 독립 재리뷰를 생략한다.
- 사람 승인 없이 커밋하거나 push한다.
- `not_claimed`를 PASS처럼 요약한다.
- **relay(ztr) 실행이 환경 제약(auto-mode bypass 거부 등)으로 막힌 것을 "구현 위임 자체가 불가"로 오판**하고, Step 6-③ 독립 subagent Implementer 폴백을 **건너뛴 채 곧장 사용자에게 "수동으로 하라"로 떠넘긴다.** (relay는 backend 1종일 뿐 위임의 정의가 아니다 — 위계를 한 칸 내려가라.)
- **subagent Implementer 폴백(③)에서 독립 Reviewer·Mechanical 완주 없이** subagent의 자연어 완료 보고를 PASS로 의미 판정하거나 곧장 Human Gate 2(커밋)로 간다(R5·자기 구현 자기 승인 금지 위반 — subagent 결과는 "구현 산출 대기"이며 표준 verdict 전엔 PASS 아님).
- **Implementer가 실제 실행된 뒤의 코드·검증·리뷰 실패를 backend 장애로 오분류**해 다른 backend에서 같은 구현을 반복한다(폴백은 구현 시작 전 transport/capability 실패에만).
- `completion_mode=unattended` 단계에서 확인창 가능 표시 호출을 하거나, 미검증 전달 채널을 기다려 종료를 홀딩한다([LESSON-M061](../phased-implementation-handoff/references/unattended-completion.md)).
