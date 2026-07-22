---
name: phase-cycle-orchestrator
description: >
  바깥 루프 오케스트레이터. 사용자가 "N페이즈 진행", "다음 페이즈까지 돌려", "phase-cycle-orchestrator로 진행",
  "설계→리뷰→구현→리뷰→커밋 게이트를 배선해"처럼 요청할 때, phase0/phased-handoff/ztr run-phase/preen/zrt-phase-commit을
  연결해 페이즈 간 산출물 전달, 실행 어댑터 치환, 휴먼 게이트 3곳, PASS/NOT CLAIMED 보고를 운전한다.
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
- 오케스트레이터는 설계리뷰 권위를 재정의하지 않는다. 설계+프롬프트 저작의 게이트는 `phased-implementation-handoff` §8을 호출해 상속한다.
- 오케스트레이터는 바깥 루프 운전자다. 직접 구현하거나 자기 설계·구현을 승인하지 않고, Reviewer 산문의 의미를 코드/스킬로 판정하지 않는다.
- 구현리뷰 기준은 "이미 캐논에 완결되어 있다"고 주장하지 않는다. ASM-3 상세 체크리스트는 임시 기술부채다.
- 구현리뷰 leg에는 `MULTI_AGENT.md`의 역할 독립성 + `phased-implementation-handoff` §8 체크리스트 + M2 5필드를 **review artifact 파일**로 주입한다.
- 큰 diff, 파일 본문, 리뷰 요청, 다중 문서 컨텍스트를 argv에 직접 싣지 않는다.
- 반복 실행 경로는 shell 문자열이 아니라 runner/subprocess argv 배열로 구성한다. PowerShell 변수에 JSON argv를 담아 재전달하지 않는다.
- exit code 0은 leg process 성공일 뿐, 의도한 파일 변경 성공을 자동 증명하지 않는다. driver는 changed paths/diff 존재 여부를 사실 증거로 별도 보고한다.
- 자동 수정 루프와 완전 무인 운영은 범위 밖이다. 페이즈 경계마다 사람 확인을 받는다.

## Phase Loop

사용자가 `N`을 지정하지 않으면 1페이즈만 준비하고, 다음 페이즈 진행 여부를 휴먼 게이트에서 묻는다.

각 페이즈마다:

1. Sync-In
   - HANDOFF, roadmap, lessons, current diff, project config를 읽는다.
   - role assignment와 active skills/tools를 확인한다.
   - 전제나 scope가 갈리면 실행 전에 질문한다.

2. Design And Prompt
   - `phased-implementation-handoff`를 사용해 설계, 구현 프롬프트, 검증 기준을 만든다.
   - `phased-implementation-handoff`의 §8 내장 Reviewer + Nitpicker 2-leg 게이트를 설계리뷰로 본다(오케스트레이터가 게이트를 자체 보유·재정의하지 않는다 — L23 권위 상속).
   - `cross-session-plan-review`를 중복 호출하지 않는다. 이미 작성된 외부 계획을 따로 검토하는 경우에만 사용한다.

3. Human Gate 1: Design PASS
   - 설계 PASS, scope, NOT CLAIMED, 다음 실행 명령 요약을 보여준다.
   - 사람이 go 하지 않으면 안쪽 루프를 실행하지 않는다.

4. Prepare Artifacts
   - 구현 프롬프트를 `{prompt_file}` artifact로 저장한다.
   - 구현리뷰 기준 bundle을 `{review_artifact}` artifact로 저장한다.
   - changed paths가 필요하면 `{changed_paths_file}` artifact로 저장한다.
   - artifact는 UTF-8로 쓰고, 외부 JSON/YAML 입력은 UTF-8-SIG를 허용한다.
   - reviewer/provider의 `working_directory`와 `artifact_root` 안에서 읽을 수 있는 경로에 artifact를 둔다.

5. Build Inner-Loop Command
   - project config의 "안쪽 루프 백엔드" 블록을 읽는다.
   - `EXECUTION_ADAPTER_CONTRACT.md` §5에 따라 `{name}` 토큰을 단일 pass로 치환한다.
   - provider capability의 `model`, `approval_policy`, `sandbox_policy`, `working_directory`, `artifact_root`, `required_env`, `dangerous_bypass_required_for_dogfood`를 실행 전에 확인한다.
   - 미정의 토큰이 남으면 실행 전 `BLOCKED`로 멈춘다.
   - 조건부 flag는 빈 문자열로 치환하지 말고 argv 원소를 삽입하거나 생략한다.
   - 경로는 repo root 기준으로 정규화하고 argv 배열의 한 원소로 전달한다.
   - 반복 실행은 Python/Node runner의 argv 배열로 만든다. JSON argv는 상위 argv의 원소 하나로 전달하고, PowerShell one-liner에 재직렬화하지 않는다.
   - dogfood 전용 위험 bypass가 필요하면 capability에 `dangerous_bypass_required_for_dogfood: true`로 명시하고 보고서에 범위를 남긴다. 범용 기본값처럼 숨기지 않는다.

6. Run Inner Loop
   - **구현 위임의 본질 = 독립 Implementer 컨텍스트에 넘기는 것.** 아래 backend는 그 본질을 실현하는 *수단*이며, backend 하나가 막혀도 "구현 위임 자체가 불가"가 아니라 다음 backend로 내려간다(리뷰 leg 폴백 §5.5/§8과 **대칭**). relay는 backend 1종일 뿐 위임의 정의가 아니다.
   - **폴백 허용 사유 = "구현 시작 전 transport/capability 실패"에 한정**(classifier 거부·실행권한/sandbox 충돌·CLI 시작 불가). **Implementer가 실제 실행된 뒤의 코드·검증·리뷰 실패**(테스트 red·`CHANGES_REQUESTED`·repo 오류)는 backend 장애가 아니라 **기존 verdict대로 중단**한다 — 다른 backend에서 같은 구현을 반복해 fail-fast·timebox를 우회하지 않는다.
   - **Implementer backend 폴백 위계(4단계 — 계약 §3.1과 동일 번호, 막히면 한 칸씩만 내려간다)**:
     - ① `ztr run-phase` relay 기본 — config argv JSON으로 implementer/reviewer/mechanical leg 구동.
     - ② relay **안전 모드 재시도** — bypass가 classifier에 막히면 `workspace-write`로(파일편집만; self-verify 셸은 `--test-cmd` leg가 대행).
     - ③ ②도 transport로 막히면 **Agent 툴 subagent를 독립 Implementer 세션으로 구동** + **명시 어댑터로 완주**: subagent는 ztr Envelope을 내지 않으므로 **subagent 결과 자체는 PASS가 아니라 "구현 산출 대기"**다. 반드시 `[Implementer 산출 → 독립 Reviewer leg → Mechanical leg → 표준 verdict(enum/exit)]`를 완주해야 Step 7/Human Gate 2로 갈 수 있다. **Planner가 subagent의 자연어 완료 보고를 의미 판정해 PASS로 바꾸지 않는다(R5).**
     - ④ ①②③이 모두 불가할 때만 사용자 수동 Implementer 세션 위임(최후). ③(subagent+어댑터)를 건너뛰고 곧장 수동으로 후퇴하지 않는다.
   - **cross-lineage 검증(형식적 swap 금지, 캐논 §8 상속)**: 기본 요구는 **실제 reviewer 계열 ≠ 실제 implementer 계열** AND **reviewer 컨텍스트 ≠ implementer 컨텍스트**다. 이를 절차가 아니라 **실행 전·후 실측**으로 확인하고 — 완료 보고 evidence에 **양쪽 실제 계열·컨텍스트/session id와 비교 결과**를 필수 기록한다("계열이 안 바뀌었다"는 이유로 교차검증을 건너뛰지 않는다). cross-lineage를 확보할 수 없을 때만 §8 폴백 위계의 ③(같은 계열 독립 컨텍스트)로 내려가되, 이는 **예외 없는 불변식이 아니라 캐논 §8이 정의한 열화 모드**이므로 review 기록에 `same-lineage(계열 독립성 미확보)`를 명시한다(오케스트레이터는 §8 권위를 재정의하지 않는다). **Planner의 Reviewer 겸임 금지 = 컨텍스트 분리는 예외 없는 조건**(계열과 별개).
   - relay 경로에서: implementer, reviewer, mechanical/preen leg를 config의 argv JSON으로 전달한다.
   - resume는 `--session-map`, `--implementer-resume`, `--reviewer-resume`, `--*-resume-profile` 정책을 따른다.
   - `auto` resume 실패는 새 세션 폴백 + 맥락 손실 경고로 보고한다.
   - 명시 session id 실패 또는 다른 id 캡처는 `BLOCKED`로 보고한다.
   - Reviewer leg가 종료되면 verdict와 무관하게 Orchestrator가 제어권을 회수한다. Reviewer가 수정이나 다음 역할을 이어서 수행하게 두지 않는다.

7. Route Envelope
   - stdout의 단일 Envelope JSON을 읽는다.
   - `PASS`: 휴먼 게이트 2로 이동한다.
   - `CHANGES_REQUESTED`는 Envelope의 exact `status`/`exit_code` 조합으로만 route한다. 자연어 완료 보고나 finding 본문을 PASS로 재분류하지 않는다.
   - 자동 수정은 시작하지 않고 현재 run을 멈춘 뒤, Orchestrator가 모든 finding을 기존 four-way `methodology/artifacts/finding-disposition.md`에 기록한다.
     - 허용값은 `ACCEPT`, `REJECT_FALSE_POSITIVE`, `DEFER_OUT_OF_SCOPE`, `REJECT_OVERENGINEERING`이다. evidence·rationale·owner가 누락되면 corrective round는 `BLOCKED`다.
     - `REJECT_FALSE_POSITIVE`와 `REJECT_OVERENGINEERING`에는 반증 evidence가 필수다. `DEFER_OUT_OF_SCOPE`는 결함 부정이 아니며 owner와 후속 위치가 필수다.
     - disposition은 수정 대상 선별일 뿐 phase 승인이나 자기 구현 승인이 아니다.
     - 사람의 명시 trigger와 필수 evidence가 없으면 `BLOCKED`다. 이 workflow gate는 human-presence의 암호학적 증명이 아니며 B-3은 **NOT CLAIMED**다.
     - 기존 `methodology/tools/remediation_adapter.py --human-triggered`를 호출하고 stdout의 structured `PASS/0`만 수용한다. `--accept-leg orchestrator-accepted-review`는 adapter가 만든 accepted-only synthetic report를 선택할 뿐 새 disposition 권위가 아니다.
     - project config의 `fix_rounds_max`를 읽는다. 키가 없으면 기본 `3`; 값은 bool/문자열이 아닌 exact integer `1..5`여야 한다. 범위 밖·중복·모호한 정의는 fix-round 호출 전에 `BLOCKED`다. 최종 범위 권위는 runtime validator다.
     - `reapply-status`로 원장을 먼저 읽는다. terminal이면 새 round를 발행하지 않고 사람에게 에스컬레이션한다.
     - accepted findings, 다음 round index, max, input digest, NOT CLAIMED를 사람에게 제시하고 그 round에 대한 trigger를 한 번 받는다.
     - 다음 명령 shape로 정확히 1회만 실행한다: `ztr fix-round ... --accept-leg orchestrator-accepted-review --record --max-rounds <N>`. 별도 while/retry/autofix는 금지한다.
     - fix-round Envelope `PASS/0`이면 Human Gate 2로 간다. `CHANGES_REQUESTED/1`이면 제어권을 회수해 새 disposition부터 다시 사람에게 맡긴다. `BLOCKED/2`, operational `70/124`, terminal 상태는 artifact와 함께 사람에게 에스컬레이션한다.
     - 수정 뒤 원 페이즈와 동일한 결정론 검증 및 독립 Reviewer/Mechanical gate가 모두 구조화 PASS여야 한다. Reviewer 종료 뒤 Orchestrator가 제어권을 회수하며, natural-language completion은 PASS가 아니다.
   - `BLOCKED`: blocker, stderr 요약, artifact 경로, 재현 명령을 보고하고 멈춘다.
   - `not_claimed`는 PASS처럼 말하지 않는다.
   - PASS라도 driver evidence를 별도 기록한다: changed paths 또는 diff artifact 존재 여부, session-map 경로/id, prompt/review artifact 경로, run dir, 각 leg envelope 경로.
   - changed paths/diff 존재 여부는 사실 증거로만 보고한다. LLM prose를 파싱해 "요구사항 충족"으로 자동 재분류하지 않는다.

8. Human Gate 2: Commit
   - 사람이 커밋을 요청할 때만 `zrt-phase-commit`을 사용한다.
   - 커밋 전 변경 범위, 검증 증거, HANDOFF/lessons/roadmap 상태를 다시 확인한다.
   - AI attribution 없는 한국어 커밋 메시지만 허용한다.

9. Human Gate 3: Continue
   - 다음 페이즈 진행 여부를 묻는다.
   - **페이즈 완료 시 Sync-Out**: HANDOFF·lessons 갱신 + **진행 SoT 갱신**(DOC_TAXONOMY §3 지정 = `methodology/docs/PHASE_CYCLE_ORCHESTRATOR_DESIGN.md §10`에 완료 페이즈 ✅·현재 위치). 컴포넌트 로드맵은 포인터만.
   - 진행하면 session-map과 HANDOFF를 유지해 다음 루프로 간다.
   - 중단하면 Sync-Out으로 종료한다.

## Review Artifact Requirements

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
["claude", "-p", "Review artifact: {review_artifact}", "--output-format", "json"]
```

reviewer leg는 `--reviewer-verdict-source=stdout_token`(기본)으로 돈다. 따라서 위 output_contract의
`ZTR_VERDICT` 단독 라인을 **반드시** 출력해야 하며, exit code는 verdict로 쓰지 않는다(`claude -p`는
verdict=BLOCKED여도 exit 0). 근거: `EXECUTION_ADAPTER_CONTRACT.md §2.1`.

## Execution Adapter Rules

- 표준 변수는 project config의 목록을 따른다: `{repo_root}`, `{phase_id}`, `{prompt_file}`, `{review_artifact}`, `{implementer_cmd}`, `{reviewer_cmd}`, `{mechanical_cmd}`, `{session_map}`, `{run_output_dir}`, `{record_flag}`, `{changed_paths_file}`, `{implementer_resume}`, `{reviewer_resume}`, `{implementer_resume_profile}`, `{reviewer_resume_profile}`.
- 리터럴 중괄호는 `{{`와 `}}`로 쓴다.
- 값 내부의 `{...}`는 재귀 치환하지 않는다.
- provider capability의 `verified_cli_version`과 `supports_*`는 실측한 것만 믿는다.
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

- 설계리뷰를 오케스트레이터가 새로 정의한다.
- 구현리뷰 기준이 캐논에 완결되어 있다고 말한다.
- diff나 리뷰 기준을 긴 argv로 전달한다.
- PowerShell one-liner나 shell 문자열에 JSON argv, diff, 리뷰 본문을 넣어 반복 실행 경로로 삼는다.
- provider의 model pin, 승인 정책, sandbox, working directory, artifact root, required env를 capability 밖의 암묵 전제로 둔다.
- dogfood 전용 dangerous bypass를 일반 실행 기본값처럼 숨긴다.
- exit code 0만 보고 파일 변경 성공이나 요구사항 충족을 주장한다.
- 미정의 `{token}`을 빈 문자열로 바꾼다.
- `CHANGES_REQUESTED` 뒤 disposition·Human trigger 없이 자동 수정 루프를 시작하거나 Reviewer가 직접 수정한다.
- rejected/deferred finding을 `ztr fix-prompt`에 섞거나 corrective round의 독립 재리뷰를 생략한다.
- 사람 승인 없이 커밋하거나 push한다.
- `not_claimed`를 PASS처럼 요약한다.
- **relay(ztr) 실행이 환경 제약(auto-mode bypass 거부 등)으로 막힌 것을 "구현 위임 자체가 불가"로 오판**하고, Step 6-③ 독립 subagent Implementer 폴백을 **건너뛴 채 곧장 사용자에게 "수동으로 하라"로 떠넘긴다.** (relay는 backend 1종일 뿐 위임의 정의가 아니다 — 위계를 한 칸 내려가라.)
- **subagent Implementer 폴백(③)에서 독립 Reviewer·Mechanical 완주 없이** subagent의 자연어 완료 보고를 PASS로 의미 판정하거나 곧장 Human Gate 2(커밋)로 간다(R5·자기 구현 자기 승인 금지 위반 — subagent 결과는 "구현 산출 대기"이며 표준 verdict 전엔 PASS 아님).
- **Implementer가 실제 실행된 뒤의 코드·검증·리뷰 실패를 backend 장애로 오분류**해 다른 backend에서 같은 구현을 반복한다(폴백은 구현 시작 전 transport/capability 실패에만).
