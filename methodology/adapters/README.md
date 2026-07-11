# adapters/ — 생태계별 어댑터

캐논(`METHODOLOGY.md`/`MULTI_AGENT.md`/`DOC_TAXONOMY.md`)이 **단일 소스(SSoT)**. 어댑터는 각 도구 생태계가
같은 방법론을 따르도록 캐논을 **요약 + 출처 인용**한 얇은 진입점이다. 어댑터끼리 규약을 다르게 정의하지 않는다(drift 금지).

| 생태계 | 어댑터 | 배포/사용 |
|---|---|---|
| **Claude surface** | `plugins/agent-workflow/skills/phased-implementation-handoff/` (플러그인 스킬) | `/plugin marketplace add … → /plugin install` (PC마다 1회, 전 프로젝트 자동 적용) |
| **Claude Code CLI beta** | `adapters/claude/CLAUDE.md` + `.claude/skills/*` + `nitpicker/` | `./install.sh --target <project> --mode lite --with-nitpicker --provider ollama` (리포마다) |
| **Codex/GPT surface** | `adapters/codex/AGENTS.md` | **작업 리포 루트에 복사** → Codex가 자동 로드 (리포마다) |
| **Cursor 등** | (선택, 실수요 시) `.cursor/rules` | 후속 |

## 자기완결 원칙
- Claude 플러그인과 Codex/GPT `AGENTS.md`는 각자 **설치/복사된 위치에서 self-contained**여야 한다
  (Claude는 플러그인 폴더만, Codex는 현재 리포만 읽음 → 루트 캐논에 못 닿을 수 있음).
- 그래서 어댑터는 캐논의 **핵심 불변식을 inline 요약**으로 담고, 전체는 multiagent-methodology 캐논을 출처로 가리킨다.
- 캐논이 바뀌면 어댑터 요약도 갱신(분량이 작아 수동으로 충분, C5).

## 새 작업 리포 온보딩 (기본)
1. `adapters/codex/AGENTS.md` → 그 리포 루트에 복사.
   - 이 파일은 구현 세션 고정값이 아니라 role router다. 같은 리포 안의 세션도 사용자 지시에 따라 Discovery/Planner/Implementer/Reviewer로 나뉜다.
   - `Adapter-Version`이 중앙 레포와 다르면 stale copy다. 중앙본을 재복사한다.
2. `config/project.config.example.md` → 그 리포 `.claude/phased-handoff.config.md`로 복사 후 값 채움.
3. (Claude surface를 쓰면) ① 그 PC에 agent-workflow 플러그인 1회 설치 + ② 리포 `CLAUDE.md`에 방법론 포인터 한 줄 추가.
   - 예: "이 리포는 multiagent-methodology를 따른다 — 역할/원칙은 `AGENTS.md` + `.claude/phased-handoff.config.md` 참조, Discovery/Planner는 해당 스킬 사용."
   - **이유(실적용 교훈):** Codex/GPT는 리포 루트 `AGENTS.md`를 자동 로드하지만 **Claude는 `AGENTS.md`를 자동으로 읽지 않는다.** CLAUDE.md 훅이 없으면 Claude 세션엔 방법론이 스킬 트리거로만 적용되고 ambient 규약(역할/C-원칙)은 빠진다.
4. 작업 시작 시 Role Assignment를 적는다. 예: Discovery=GPT session A, Planner=Claude/GPT session B, Implementer=Codex workspace session, Reviewer=GPT session C.
→ 모델명이 아니라 지정 세션의 역할 계약으로 동작한다.

## Claude Code CLI 베타 온보딩

Claude Code CLI를 WSL/Linux/macOS에서 주로 쓰는 팀원에게는 plugin 설치만으로 끝내지 말고 프로젝트 루트에 adapter와 skills를 복사한다.

```bash
./install.sh --target /path/to/project --mode lite --with-nitpicker --provider ollama
```

설치 결과:
- `CLAUDE.md`: Claude Code용 role router.
- `.claude/skills/*`: Discovery, handoff, Nitpicker review skills.
- `nitpicker/`: Ollama/mock 기반 local review wrapper.
- `.agent-workflow-backup/<timestamp>/`: 기존 파일 백업.

1차 베타에서는 hooks를 강제하지 않는다. 자세한 배포 기준은 `docs/CLAUDE_CODE_OLLAMA_BETA_DEPLOYMENT.md`를 따른다.

## Stale adapter 갱신 규칙

작업 리포의 `AGENTS.md`는 중앙 어댑터에서 복사된 derived asset이라 자동 갱신되지 않는다.
아래 상황에서는 중앙본을 재복사한다.

- 중앙 `adapters/codex/AGENTS.md` 또는 `adapters/claude/CLAUDE.md`의 `Adapter-Version`이 바뀜
- `MULTI_AGENT.md`의 역할/금지/Role Resolution 규칙이 바뀜
- 프로젝트에서 리뷰/문서/Discovery 세션이 구현 흐름으로 오염됨
- 오래된 프로젝트를 재개하거나 새 프로젝트를 온보딩함

세부 shared/derived 경계와 ownership matrix는 `docs/TEAM_ASSET_GOVERNANCE.md`를 따른다.
