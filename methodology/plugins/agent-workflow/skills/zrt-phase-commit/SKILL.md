---
name: zrt-phase-commit
description: >
  ZRT/MM phased work에서 사용자가 "커밋해", "커밋까지 진행", "phase 커밋", "검토 후 커밋"처럼 명시적으로 커밋을 요청했을 때 사용한다.
  git status와 변경 범위, 검증 증거, ROADMAP/HANDOFF/LESSONS 상태, NOT CLAIMED 정직성을 확인한 뒤 한국어 커밋 메시지를 작성하고,
  AI/모델/봇 attribution 없이 사용자 승인 범위의 변경만 커밋한다.
---

# ZRT Phase Commit

이 스킬은 Phase 종료 커밋을 위한 closeout 게이트다. 목적은 "좋은 커밋 메시지"만이 아니라, 커밋 직전 변경 범위와 검증 증거가 실제 상태와 맞는지 확인하는 것이다.

## Hard Gates

- 사용자가 커밋을 명시적으로 요청하지 않았으면 커밋하지 않는다. 필요하면 커밋 메시지 초안만 제안한다.
- 시작 시 `git status --short --branch`로 브랜치와 dirty 상태를 확인한다.
- 현재 브랜치를 유지한다. 새 브랜치, 태그, push, PR은 사용자가 따로 요청한 경우에만 한다.
- 관련 없는 변경은 스테이징하지 않는다. 이미 staged에 무관한 파일이 있으면 커밋 전에 범위를 사용자에게 보고한다.
- 사용자 변경을 되돌리지 않는다. 정리가 필요하면 먼저 설명하고 확인을 받는다.
- 검증을 실행하지 못했거나 실패했으면 PASS처럼 말하지 않는다. `BLOCKED` 또는 `NOT CLAIMED`로 보고한다.
- **구현(코드/스크립트) 변경은 독립 design-review 없이 커밋하지 않는다.** 커밋 범위에 코드·스크립트(예: `install.sh`/`package.sh`/모듈) 변경이 있으면 **author≠reviewer 독립 design-review leg(별도 컨텍스트 서브에이전트) PASS 증거**가 있어야 커밋한다. `pytest`/`ruff`/`mypy`/`bash -n`/`--dry-run` 같은 self-verify는 필요조건일 뿐 충분조건이 아니다(2-leg 하드게이트). 증거 없으면 커밋 중단 → 리뷰 먼저. 순수 docs/메모리 변경은 예외. **Planner/orchestrator 세션이 직접 구현했어도 면제되지 않는다**("tooling이라 사소함"도 면제 아님).
- 커밋 메시지에 AI attribution을 넣지 않는다.

금지 문구:

```text
Generated with Claude Code
Generated with Codex
Generated with GitHub Copilot
Co-Authored-By: Claude
Co-Authored-By: Codex
Co-Authored-By: GitHub Copilot
Co-Authored-By: OpenAI
copilot
bot@
model:
model name:
assistant:
```

## Workflow

1. **Scope 확인**
   - `git status --short --branch`
   - `git diff --stat`
   - 필요하면 `git diff -- <path>`와 `git diff --cached -- <path>`를 확인한다.

2. **문서 상태 확인**
   - Phase 작업이면 ROADMAP/HANDOFF/프롬프트/LESSONS가 실제 완료 상태와 맞는지 본다. **다컴포넌트/스위트면 진행 SoT(DOC_TAXONOMY §3 지정 — 현 스위트=설계 §10)도 완료 페이즈 ✅·현재 위치로 갱신했는지 확인**한다(컴포넌트 로드맵은 포인터만).
   - `커밋 대기`, `예정` 같은 임시 상태가 남아 있으면 커밋 전 수정한다.
   - 검증하지 않은 항목은 `NOT CLAIMED`로 남긴다.

3. **검증 확인**
   - 프로젝트가 ZRT라면 우선 다음을 확인한다.

```bash
python -m ruff check src tests
python -m mypy src
python -m pytest -q
python -m src review --changed
python -m src verify --post-merge --changed
python -m src invariants
```

   - 프로젝트별 명령이 다르면 AGENTS/CLAUDE/project config의 명령을 우선한다.
   - 이미 사용자가 같은 턴에서 신뢰 가능한 검증 출력을 제공했더라도, 커밋 전 cheap check는 재실행하는 편을 우선한다.

4. **Stage**
   - 변경 목적에 맞는 파일만 `git add <paths...>`로 스테이징한다.
   - `git add .`는 변경 범위가 작고 전부 관련 있음이 확인된 경우에만 쓴다.
   - staging 후 `git status --short`로 범위를 다시 확인한다.

5. **Commit**
   - 제목은 한국어로 작성한다.
   - 형식은 `<type>: <한글 요약>`을 쓴다.
   - 허용 type: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`.
   - 작은 커밋은 제목만 허용한다.
   - Phase/설계/운영 교훈이 있는 커밋은 아래 본문 포맷을 쓴다.

6. **Report**
   - commit hash, 브랜치, clean 여부, 실행한 검증, NOT CLAIMED를 짧게 보고한다.

## Commit Message Format

제목:

```text
<type>: <한글 요약>
```

본문이 필요한 경우:

```text
원인:
- 왜 이 변경이 필요했는지 적는다.

수정사항:
- 실제로 바꾼 파일/계약/동작을 적는다.

시행착오:
- 커밋 가치가 있는 오류, 우회, 플랫폼 이슈만 적는다.
- 단순 오타나 내부 사고 과정은 쓰지 않는다.

수정 관점:
- 어떤 설계 원칙으로 반영했는지 적는다.
- 예: 의미 해석 금지, 파일시스템 사실 검사만, 사용자 승인 게이트 유지.

검증:
- 실제 실행한 명령과 결과를 적는다.

NOT CLAIMED:
- 검증하지 않은 환경/장비/E2E 항목만 적는다.
```

빈 섹션은 넣지 않는다. 본문은 감사 로그로 가치가 있을 때만 쓴다.

## Examples

작은 기능 커밋:

```text
feat: Phase 5 run-phase 구현
```

본문 있는 Phase 커밋:

```text
feat: Phase 5 run-phase 구현

원인:
- Phase 5에서 fake/live CLI relay의 결정론적 실행 경계가 필요했다.

수정사항:
- phase_relay 엔진을 추가하고 ztr run-phase CLI를 배선했다.
- fake CLI 기반 PASS/CHANGES_REQUESTED/BLOCKED/timeout 테스트를 추가했다.

시행착오:
- PowerShell JSON 배열 인자 이스케이프가 깨지기 쉬워 shell-like 입력 smoke도 확인했다.

수정 관점:
- 리뷰 자연어 의미 해석은 하지 않고 child exit code와 timeout만 verdict로 라우팅했다.

검증:
- ruff PASS
- mypy PASS
- pytest 189 passed
- ztr verify/invariants/review PASS

NOT CLAIMED:
- full E2E 자동 관통
- Sonnet 리뷰 탐지 품질
```
