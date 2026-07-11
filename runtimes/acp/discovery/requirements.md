# Requirements

## Problem

사용자는 여러 **상용 GUI AI 데스크톱 앱**(Claude Desktop, Cursor, **Codex Desktop**)에서 동시에 여러
세션을 띄워 서로 다른 프로젝트를 병렬로 진행한다. 현재는 어떤 앱의 어떤 세션이 **어느 프로젝트에
묶여**, **어떤 페이즈를** 진행 중이며, **살아있는지/멈췄는지(특히 입력 대기로 '홀딩'됐는지)**를 한눈에
볼 방법이 없다. 머릿속 관제가 프로젝트 3개를 넘어가는 순간 한계에 도달한다.

### 트리거 사건 (왜 지금 필요한가)

**ZTR(ZeroTokenRoundtable)가 무한 홀딩 상태에 빠졌다.** 멀티에이전트 오케스트레이션이 상용 GUI 앱
세션에 의존하는 구간에서, 그 세션이 *입력 대기/멈춤*에 들어가도 ZTR은 그 사실을 알 길이 없어 **영원히
기다린다.** 즉 이 관제소의 **킬러 기능은 단순 모니터링이 아니라 "GUI 세션이 홀딩/멈춤에 빠진 순간을
외부에서 탐지"** 하는 것이다. 사용자가 제안한 "주기적 polling SW"는 바로 이 사각지대를 뚫는 처방이다.

## Root Cause

- 상용 GUI 앱은 **코드 주입(in-app push)이 불가능**하다 → 앱이 스스로 "나 지금 멈췄다"를 보고하지 못한다.
- 그래서 ZTR 같은 오케스트레이터가 그 세션을 기다리면 **홀딩을 감지할 내부 신호가 없다**(좀비 대기).
- 페이즈 계획은 앱이 아니라 **사람이 정의하는 작업 산출물**에 있는데, 이게 프로젝트마다 흩어져 있다.
- 따라서 "현황"이 (a) 앱의 로컬 흔적 (b) 프로젝트의 페이즈 문서 두 곳에 분리 저장되어, 종합된 단일
  뷰가 존재하지 않는다.
- **수집 방식의 정답:** 앱을 바꿀 수 없으므로 **외부에서 로컬 아티팩트를 주기적으로 polling**하는 것이
  유일·안전·0토큰 경로다(사용자의 최초 제안이 이 도메인에선 정답).

## Goals

1. 멀티 데스크톱 앱(Claude / Cursor / **Codex**)의 세션을 **읽기 전용으로 수집**해 한 화면에 종합한다.
2. 각 세션을 **프로젝트(PHASE.md)에 바인딩**하고, 페이즈 계획 + 현재 페이즈를 함께 표시한다. (Lv2+)
3. **생존/좀비/에러/완료**에 더해 **'홀딩(입력 대기 멈춤)'** 을 판정하고, 좀비/에러/완료/홀딩 시
   **토스트·웹훅 알림**을 발행한다. → ZTR가 빠졌던 무한대기를 사람이 즉시 인지.
4. 상태 추적 전 과정에서 **LLM 토큰 0원**을 유지한다. (수집은 순수 파일 파싱)

## Non-Goals (v1에서 명시적으로 안 한다)

- ❌ 세션 **통제**(일시정지/강제종료/프롬프트 주입). 상용 앱은 사실상 불가 → v2+ 검토.
- ❌ **Lv3 대화 내용** 파싱(현재 메시지/주제). v1은 메타데이터(cwd/모델/활동시각)까지만.
- ❌ 클라우드/도커 **분산 관제**. v1은 단일 PC localhost.
- ❌ 암호화·잠금된 앱 DB의 리버스 엔지니어링.
- ❌ ChatGPT **순수 채팅** 데스크톱 앱(현재 미설치). GPT 계열은 **Codex Desktop**으로 커버.

## Success Criteria

| Criterion | Required evidence | Validation level |
|---|---|---|
| Codex Desktop 세션을 cwd·모델·활동시각·실행명령으로 파싱 | 실제 `sessions/*.jsonl` + `process_manager/chat_processes.json` 파싱 캡처 | Live smoke |
| Claude Desktop 세션을 cwd·모델·활동시각으로 정확히 파싱 | 실제 세션 JSON 1건 이상 정상 파싱 캡처 | Live smoke |
| Cursor/VS Code 워크스페이스를 폴더 경로로 바인딩 | workspace.json → 폴더 매핑 결과 캡처 | Live smoke |
| 세션 ↔ PHASE.md 조인으로 페이즈 표시 | 1개 프로젝트에서 페이즈 계획+현재 페이즈 표시 | Deterministic |
| **홀딩 판정** (활동 멈춤 + 실행 프로세스 부재 + 미완료) | 입력대기 세션을 HOLDING으로 분류하는 결정 테스트 | Deterministic |
| 좀비(STALE) 판정 정확성 | last_activity+osPid 부재 조합 결정 테스트 | Deterministic |
| 좀비/에러/완료/홀딩 알림 발행 | 토스트 또는 웹훅 1회 발행 로그 | Live smoke |
| 토큰 0원 | 수집·판정·표시 경로에 LLM 호출 없음(코드 검사) | Unit/정적 |

## Assumptions

- A1. 앱별 로컬 스키마는 당분간 유지된다:
  - Claude: `claude-code-sessions/**/local_*.json` (`cwd`,`lastActivityAt`,`model`)
  - Codex: `~/.codex/sessions/**/rollout-*.jsonl`(`session_meta`,`event_msg`) + `process_manager/chat_processes.json`(`cwd`,`command`,`osPid`,`updatedAtMs`)
  - Cursor/VSCode: `workspaceStorage/<hash>/workspace.json` + `state.vscdb`
- A2. 사용자는 관제 대상 프로젝트에 `PHASE.md`를 cubi-skills 캐논에 정렬된 형식으로 유지한다.
- A3. v1은 Owner의 단일 PC·단일 사용자 환경.

## Stakeholders / Audience

- 1차: Owner(유일 사용자).
- 2차: 향후 Planner/Implementer 세션(본 문서를 입력으로 사용).
