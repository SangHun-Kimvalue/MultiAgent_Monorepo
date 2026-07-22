# 재개 프롬프트 템플릿 (provider-neutral)

압축 후 새 컨텍스트(같은 세션의 다음 model request 또는 완전히 새 세션)가 그대로 복사해 쓰는
bootstrap 프롬프트다. **이 프롬프트도 압축 요약도 진행상태의 새 정본이 아니다** — SoT 경로와 재측정
순서를 담을 뿐이다.

## 사용 규칙 (스킬이 지킬 것)

- 아래 `RESUME PROMPT` 블록의 `<...>` 자리를 이번 세션 실측값으로 채운다.
- 최종 재개 프롬프트는 채팅에 **복사 가능한 fenced code block**으로 반드시 출력한다.
- 같은 내용을 workspace root의 고정 artifact `.agent-workflow/compaction/resume-prompt.md`에 UTF-8로
  atomic overwrite한다(`git rev-parse --show-toplevel`, 아니면 `cwd`). 상단 metadata 3줄
  (`generated_at_utc`/`workspace_root`/`captured_head`)을 유지해야 부트 훅이 stale 여부를 판별한다.
- 쓰기 불가(read-only) 환경이면 채팅 출력까지 완료하고 `automatic resume artifact: NOT CLAIMED`로
  보고한다. 경로를 임의로 바꾸지 않는다.
- non-git이면 `captured_head`를 `NOT_AVAILABLE`로 둔다.

## 템플릿 본문 (artifact + 채팅 공용)

```
generated_at_utc: <UTC ISO8601, 예 2026-07-13T04:20:00Z>
workspace_root: <절대경로, git toplevel 또는 cwd>
captured_head: <현재 HEAD 40자 SHA, non-git이면 NOT_AVAILABLE>

# RESUME PROMPT — 압축 후 재개

압축 요약은 손실적이므로 ground truth로 신뢰하지 말고, 아래 SoT와 저장소를 직접 재측정한 뒤 시작하라.
계획을 완료로 간주하지 말고, unrelated dirty 변경을 수정/되돌리지 말며, 문서와 코드가 충돌하면
코드·테스트를 먼저 믿고 stale을 보고하라.

- 저장소(절대경로): <repo abs path>
- 브랜치 / HEAD: <branch> / <HEAD sha>
- 현재 역할: <Discovery | Planner | Implementer | Reviewer>
- 대상 모듈 · 페이즈: <module / phase>

가장 먼저 읽을 SoT (이 순서로):
  1. <루트 CLAUDE.md / AGENTS.md 부트 절차>
  2. <진행 SoT 문서 + 절, 예 methodology/docs/....md §10>
  3. <대상 로드맵 / lessons/<module>.md>
  4. <관련 코드/테스트 경로>

착수 전 재측정(실행):
  - git status --short --branch
  - git log --oneline -15
  (아래 "확정 사실"과 다르면 실측을 믿고 이 프롬프트의 stale을 보고하라.)

확정 사실(검증된 것만): <fact 1> / <fact 2> / ...
실제 완료 작업(이번 세션): <done 1> / <done 2> / ...
실행 검증(명령 + 결과 + artifact 경로): <cmd → result> / ...
PASS(어디까지): <PASS 범위>
NOT CLAIMED(주장 안 함): <미검증/미실행 항목 + 재현 명령>
가정: <assumption> (없으면 "없음")
Open Items(TBD/결정 대기): <item 1> / ...
첫 번째 다음 작업: <가장 먼저 할 한 가지>
```

## 필수 필드 체크리스트

재개 프롬프트에 아래가 모두 있어야 한다(누락 금지):
저장소 절대경로 · 브랜치/HEAD · 현재 역할 · 대상 모듈/페이즈 · 첫 읽기 SoT 순서 ·
`git status`/`git log` 재측정 지시 · 확정 사실 · 실제 완료 작업 · 실행 검증 · PASS ·
NOT CLAIMED · 가정 · Open Items · 첫 번째 다음 작업 · 첫 문단의 "압축 요약 불신·SoT 재측정" 지시.
