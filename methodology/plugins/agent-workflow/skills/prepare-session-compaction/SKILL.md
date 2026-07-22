---
name: prepare-session-compaction
description: >
  컨텍스트 압축(대화 요약) 직전에, 현재 작업의 durable 상태를 MAM Sync-Out 규약대로 문서/메모리에
  정리하고, 압축 이후 새 컨텍스트가 실제 SoT에서 상태를 재구성하도록 복사 가능한 재개 프롬프트를
  만든다. 다음처럼 사용자가 "압축 전 정리 + 압축 후 재개 준비"를 명시적으로 요청할 때 반드시 사용:
  "압축 준비해", "대화 압축 준비해", "컨텍스트 압축 전에 정리해줘", "압축 전에 메모리와 문서를
  업데이트해줘", "압축 이후에 네가 상기할 수 있도록 프롬프트 만들어줘", "새 세션에서 이어갈 수 있게
  정리해줘", "prepare for compaction", "make a resume prompt before compaction". 단순 "요약해줘",
  상태 질문, ZIP/7z 같은 파일 압축 요청은 대상이 아니다.
---

# Prepare Session Compaction — 압축 전 Sync-Out + 재개 프롬프트 어댑터

> 이 스킬은 multiagent-methodology **캐논의 얇은 실행 어댑터**다. 원칙·루프·역할·문서 소유권의
> 단일 소스(SSoT)는 레포 `METHODOLOGY.md`/`MULTI_AGENT.md`/`DOC_TAXONOMY.md`와 `AGENTS.md`/루트
> `CLAUDE.md`의 부트 절차다. 아래는 그 캐논을 "압축 직전"에 쉽게 호출하도록 담은 것이며, 규칙을 새
> 문장으로 재정의하지 않고 경로/절을 가리킨다. 충돌 시 캐논이 우선.

## 0. 이 스킬의 한 줄 정의

**압축하지 않는다.** 압축 전에 (1) 실제 완료/검증/다음 작업을 문서로 Sync-Out하고, (2) 호스트가
허용하면 안정 오리엔테이션만 메모리에 갱신하고, (3) 압축 후 새 컨텍스트가 SoT를 다시 읽게 하는
재개 프롬프트를 생성한다. 재개 프롬프트도 압축 요약도 새 정본(ground truth)이 아니다.

## 1. 트리거 / 비트리거

- **트리거**: `압축 준비해`(대표), `대화 압축 준비해`, `컨텍스트 압축 전에 정리해줘`,
  `압축 전에 메모리와 문서를 업데이트해줘`, `압축 이후에 네가 상기할 수 있도록 프롬프트 만들어줘`,
  `새 세션에서 이어갈 수 있게 정리해줘`, 영문 동등 표현(`prepare for compaction`,
  `make a resume prompt before compaction`).
- `압축 준비해`는 **문서 Sync-Out + 호스트가 허용하는 메모리 갱신 + 재개 프롬프트 생성**을
  한꺼번에 요청한 호출로 해석한다.
- **비트리거**: 단순 `요약해줘`, "지금 어디까지 됐어?" 같은 상태 질문, ZIP/7z/tar 등 파일 압축
  요청. 이때는 이 스킬을 실행하지 않는다.
- 런타임이 위 문구를 **정확히 리터럴 매칭**한다고 보장하지 않는다. 선택 신호는 넓게 두되,
  목적어(문서/메모리/재개 프롬프트)와 "압축 전/후" 맥락이 함께 있을 때만 이 스킬을 고른다.

## 2. 실행 순서 (Sync-In → 재개 프롬프트)

1. **Sync-In 실측**: `git status --short --branch`, `git log --oneline -15`, 진행 SoT 문서와
   대상 모듈 `lessons/<module>.md`를 직접 읽는다. 압축 요약·이전 보고를 신뢰하지 않는다.
2. **사실 분류**: 이번 세션에서 실제로 한 일 / 검증한 것 / 미검증 / 결정 / 다음 작업을 나눈다.
3. **증거 대조**: 대화의 주장과 git/코드/테스트/문서가 충돌하면 코드·테스트를 먼저 믿고 stale을
   보고한다. 계획을 완료로 승격하지 않는다.
4. **문서 라우팅/갱신**: §3 소유권 표에 따라 해당 문서만 갱신한다(같은 사실을 여러 문서에 복제 금지).
5. **메모리 갱신(조건부)**: 사용자 요청 + 활성 호스트 정책이 **둘 다** 허용할 때만, 안정
   오리엔테이션·SoT 포인터만 기록한다. 불가하면 `NOT CLAIMED`로 보고한다(silent success 금지).
6. **재개 프롬프트 생성**: `references/resume-prompt-template.md`를 채워 (a) 채팅에 복사 가능한
   fenced code block으로 출력하고, (b) 동일 내용을 workspace root의 고정 artifact
   `.agent-workflow/compaction/resume-prompt.md`에 UTF-8 atomic overwrite한다.
7. **보고**: PASS 범위 / NOT CLAIMED / 가정 / Open Items / 첫 번째 다음 작업을 분리해 남긴다.

문서 후보나 소유권이 모호하거나 대화 주장이 실제 상태와 충돌하면 **조용히 추정하지 말고 질문/중단**한다.

## 3. 저장 위치 라우팅 계약 (소유권)

| 정보 | 정본 위치 |
|---|---|
| 실제 완료·산출물·검증·다음 작업 | 프로젝트 `HANDOFF.md` |
| 페이즈 상태·작은 결정 | 진행 ROADMAP / 결정 로그 |
| 큰 아키텍처 결정 | ADR / DESIGN |
| 비자명한 WHY·재발 방지 | `lessons/<module>.md` (append-only) |
| 안정적 오리엔테이션·SoT 포인터 | 활성 호스트가 허용하는 memory channel |
| 휘발성 진행률·임시 계획 | 메모리에 기록 금지 |
| 미검증 내용 | 가정 / unknown / NOT CLAIMED |

- 메모리 쓰기는 **사용자 요청 + 활성 호스트 정책**이 모두 허용할 때만 한다.
- 메모리 도구/쓰기 채널이 없으면 문서와 재개 프롬프트는 계속 완성하고 메모리만 `NOT CLAIMED`로 보고한다.
- Codex의 특정 메모리 파일 경로, Claude auto-memory 내부 경로, 앱 전용 directive를 이 공통 스킬에
  하드코딩하지 않는다(provider-neutral 유지).
- 같은 진행 상태를 여러 문서에 복제하지 말고 한 SoT + 포인터로 둔다.

## 4. 고정 artifact와 자동 부트 훅

- 재개 프롬프트의 고정 사본 = `<workspace_root>/.agent-workflow/compaction/resume-prompt.md`.
  `workspace_root`는 Git work tree면 `git rev-parse --show-toplevel`, 아니면 현재 `cwd`.
- artifact 상단에는 `generated_at_utc`, `workspace_root`, `captured_head`(non-git이면
  `NOT_AVAILABLE`)를 넣어, 부트 훅이 오래된 파일(stale)인지 판별하게 한다. 이 파일은 다음 컨텍스트를
  위한 bootstrap artifact이지 프로젝트 진행상태의 SoT가 아니다.
- read-only 환경이라 artifact를 쓸 수 없으면 채팅 출력까지 완료하고
  `automatic resume artifact: NOT CLAIMED`로 명시한다. 경로를 임의의 다른 위치로 조용히 바꾸지 않는다.
- **plugin으로 활성화된 Claude/Codex surface**에는 공용 `SessionStart(compact)` 훅
  (`hooks/hooks.json` + `scripts/emit_compaction_boot_context.py`)이 있어, 압축 직후 다음 model
  request에 live git facts와 이 artifact 경로/excerpt를 자동 주입한다. 훅은 developer context를
  주입할 뿐 **새 user message나 zero-turn assistant 실행을 만들지 않는다**. standalone
  `.claude/skills`/`.codex/skills`만 설치된 환경에는 훅이 없으므로, 사용자는 이 스킬의 수동
  `압축 준비해`를 쓰고 재개 프롬프트/artifact 경로 안내를 따른다.
- Claude `/compact [텍스트]`와 Codex `compact_prompt`/`experimental_compact_prompt_file`은 압축
  요약 방향을 바꾸는 채널이지, 압축 후 새 user 프롬프트를 자동 제출하는 채널이 아니다.

## 5. 정직성 불변

- 검증하지 못했거나 실패한 것을 PASS로 말하지 않는다(`BLOCKED`/`NOT CLAIMED`).
- 사용자 변경을 되돌리거나 unrelated dirty 파일을 정리하지 않는다.
- 문서와 코드가 충돌하면 코드·테스트를 먼저 믿고 stale을 보고한다.
- 커밋/태그/푸시는 사용자가 명시적으로 요청할 때만(`zrt-phase-commit` 규약).
