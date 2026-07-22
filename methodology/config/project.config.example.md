# 프로젝트 설정 (per-repo) — 템플릿

이 파일을 작업 리포의 `.claude/phased-handoff.config.md`로 복사해 **각 항목의 `<...>`를 그 프로젝트 값으로 채운다.**
스킬/어댑터는 이 파일 → 리포 agent 지침(CLAUDE.md/AGENTS.md) → (없으면) 사용자 질문 순으로 설정을 읽는다.
아래는 **범용 placeholder**다. 언어/스택 특화 값만 여기에 두고, 방법론 원칙 자체는 캐논(`METHODOLOGY.md`)을 따른다.

## 형상관리
- 현재 체크아웃된 작업 브랜치 유지.
- 새 브랜치 생성, 브랜치 전환, 태그 생성은 사용자 명시 요청이 있을 때만 수행.
- 페이즈 경계는 `<HANDOFF/로드맵/커밋 메시지 기록 방식>`으로 남김.

## 빌드/검증 명령
- 빌드: `<프로젝트 빌드 명령>`
- 테스트/정적검사: `<test 명령>` / `<lint·type 명령>`
- (선택) 독립/격리 검증: `<앱 전체 링크 없이 일부만 빌드·실행하는 명령>`

## 버전/릴리스 규칙
- 버전 기록: `<버전/RC 파일 + 동기화 대상>` (예: CHANGELOG / RC_HISTORY + 버전 상수)
- 미통합 골격(런타임 영향 0)은 버전 bump 보류, 실제 동작 변경 시 부여(선택 규칙).
- 커밋은 리뷰 통과 후 **사용자 확인** 받고 진행.

## Corrective round 정책 (phase-cycle-orchestrator)
- `fix_rounds_max: 3`
- 키가 없으면 기본값은 `3`이다. 값은 bool/문자열이 아닌 exact integer `1..5`만 허용한다.
- 중복·모호한 정의나 범위 밖 값은 fix-round 전에 `BLOCKED`한다. 최종 범위 검증 권위는 ztr runtime validator다.

## 문서·형상관리 파일 위치 (Sync-In/Sync-Out 대상)
- HANDOFF: `<docs/HANDOFF.md 등>` (없으면 생성). 다중 세션이면 필수.
- lessons: `<docs/lessons/<module>.md 등>` (append-only, WHY/LESSON).
- 결정 기록: 작은 건 로드맵 결정 로그 / 큰 건 `<docs/decisions/ADR-NNNN-<slug>.md>`.
- (선택, IP 민감 시) 외부 공개 분류: `<DISCLOSURE 문서>`.

## 기계 리뷰(Nitpicker 등) — 있으면
- 도구/경로: `<리뷰 도구 경로>`
- provider/모델: `<예: ollama / 로컬 모델명>`
- **repo 래퍼 우선:** 셸에서 `--diff "$(git diff)"` 직접 전달은 인코딩/따옴표가 깨질 수 있음 → 가능하면 repo 래퍼 스크립트 사용.
- 직접 호출 예시(파일마다):
  ```bash
  # <리뷰 도구의 단일 파일 실행 명령>. 신규 파일이면 git diff --no-index -- /dev/null <파일>
  ```
  REJECT→수정→재실행 ALL PASS까지.

## 안쪽 루프 백엔드 (phase-cycle-orchestrator)

이 블록은 바깥 루프 스킬이 구현→리뷰→Mechanical(preen) 안쪽 루프를 호출할 때 쓰는
프로젝트별 바인딩이다. 백엔드는 교체 가능하지만, 반드시
`stdout=단일 Envelope JSON`과 exit code `0/1/2/124/70` 계약을 만족해야 한다.

### 실행 계약
- 계약 문서: `methodology/docs/EXECUTION_ADAPTER_CONTRACT.md`
- exact binding 정형 입력: repo 내부의 `execution-preflight.json` 같은 JSON 파일을 두고, 예시는 `methodology/config/execution-preflight.example.json`을 참조한다. 이 Markdown에 JSON schema를 복제하지 않는다.
- expensive Implementer/Reviewer/Mechanical leg를 시작하기 전에 `methodology/tools/execution_preflight.py`로 선택된 정형 binding의 deterministic preflight를 통과해야 한다. provider version/auth까지 요구하는 실행은 `--live` PASS를 선행한다.
- 기본 런타임: `<예: ztr>`
- relay envelope 계약: `status`/`exit_code`/`not_claimed`만 분기 조건으로 사용한다.
- R5 경계: 세션 id·payload·review body는 불투명 사실로 전달하며, 코드가 의미를 파싱하지 않는다.
- payload 정책: 긴 diff/파일 본문/리뷰 요청/다중 문서 컨텍스트는 argv 금지. artifact 파일로 저장하고 argv에는 경로와 짧은 지시문만 둔다.

### 치환 규칙 (확정)
- 치환 주체: `phase-cycle-orchestrator`의 실행 어댑터. `project.config` 로더나 ztr 런타임은 치환하지 않는다.
- 치환 방식: `{name}` 토큰의 단일 pass, 정확 일치 치환. 값 내부의 `{...}`는 재귀 치환하지 않는다.
- 이스케이프: 리터럴 중괄호가 필요하면 `{{`와 `}}`를 쓴다.
- 미정의 토큰: 실행 전 `BLOCKED`로 중단한다. 조용히 빈 문자열로 치환하지 않는다.
- argv 조립: 반복 실행 경로는 JSON argv 배열을 표준으로 한다. shell 문자열 조립은 임시 수동 검증에만 허용한다.
- 경로 값: 실행 어댑터가 repo root 기준으로 정규화하고 Windows에서는 공백/백슬래시가 보존되도록 argv 배열 원소 하나로 전달한다.
- stdin 구분: ztr relay가 leg에 넘기는 내부 stdin은 검증된 런타임 계약이다. provider CLI를 ztr 밖에서 직접 호출할 때의 native stdin prompt 지원은 별도 live smoke 전까지 미검증으로 둔다.

사용 가능한 표준 변수:
- `{repo_root}`: 현재 프로젝트 루트 절대 경로.
- `{phase_id}`: phase 식별자.
- `{prompt_file}`: 구현 프롬프트 artifact 파일.
- `{review_artifact}`: 구현리뷰 입력 artifact 파일. 없으면 생성 후 경로를 넣는다.
- `{implementer_cmd}`: implementer provider argv JSON.
- `{reviewer_cmd}`: reviewer provider argv JSON.
- `{mechanical_cmd}`: Mechanical/preen argv JSON.
- `{session_map}`: 역할별 resume id JSON 파일.
- `{run_output_dir}`: relay 캡처 디렉터리.
- `{record_flag}`: 기록을 켤 때 `--record`, 아니면 빈 배열 원소가 아니라 옵션 자체를 생략한다.
- `{changed_paths_file}`: 변경 파일 목록 artifact. 없으면 생성 후 경로를 넣는다.
- `{implementer_resume}`: implementer resume 정책. `new`, `auto`, 또는 명시 session id.
- `{reviewer_resume}`: reviewer resume 정책. `new`, `auto`, 또는 명시 session id.
- `{implementer_resume_profile}`: implementer resume argv 변형 profile. `none`, `claude`, 또는 `codex`.
- `{reviewer_resume_profile}`: reviewer resume argv 변형 profile. `none`, `claude`, 또는 `codex`.

### relay 명령 템플릿
```json
[
  "ztr",
  "run-phase",
  "--phase-id", "{phase_id}",
  "--prompt-file", "{prompt_file}",
  "--implementer-cmd", "{implementer_cmd}",
  "--reviewer-cmd", "{reviewer_cmd}",
  "--mechanical-cmd", "{mechanical_cmd}",
  "--session-map", "{session_map}",
  "--implementer-resume", "{implementer_resume}",
  "--reviewer-resume", "{reviewer_resume}",
  "--implementer-resume-profile", "{implementer_resume_profile}",
  "--reviewer-resume-profile", "{reviewer_resume_profile}",
  "--output-dir", "{run_output_dir}",
  "--record"
]
```
> 주: 위 템플릿은 `--record`를 **항상 켠다**. 조건부로 켜고 끄려면 `{record_flag}`를 쓰되, 고정 JSON 배열로는 "원소 생략"을 표현할 수 없으므로 — 계약 §5대로 **실행 어댑터가 argv 원소를 삽입/생략**한다(빈 문자열 치환 금지). `{record_flag}`는 그 조건부 메커니즘의 표준 변수다.

### leg 바인딩 예시
- implementer provider: `<codex|claude|gemini-cli|custom>`
- implementer argv JSON: `["codex", "exec", "--json"]`  (ztr가 `{prompt_file}` 내용을 첫 leg stdin으로 전달)
- reviewer provider: `<claude|codex|custom>`
- reviewer argv JSON: `["claude", "-p", "Review artifact: {review_artifact}", "--output-format", "json"]`
- Mechanical(preen) argv JSON: `["ztr", "review", "--changed", "--record"]`

### resume 정책
- session map: `<.ztr/orchestrator/session-map.json>`
- implementer resume: `<new|auto|명시 session id>`
- reviewer resume: `<new|auto|명시 session id>`
- implementer resume profile: `<none|claude|codex>`
- reviewer resume profile: `<none|claude|codex>`
- 정책 의미: `new`는 새 세션을 만들고 id를 기록한다. `auto`는 map에 id가 있으면 resume하고 실패 시 새 세션으로 폴백하되 맥락 손실 경고를 남긴다. 명시 id는 실패하거나 다른 id가 캡처되면 `BLOCKED`로 승격한다.
- JSON 출력: resume 추적 provider는 id가 stdout에 나오도록 `claude --output-format json` 또는 `codex --json`에 준하는 구조화 출력 옵션을 포함해야 한다.

### provider capability 예시
```yaml
providers:
  codex:
    verified_cli_version: "<현 repo에서 실측한 버전>"
    model: "<반복 실행에서 사용할 모델 pin 또는 unverified>"
    known_argv_shape: ["codex", "exec", "--json", "<short prompt or relay stdin>"]
    max_inline_prompt_chars: 2000
    supports_stdin_prompt: "unverified"
    supports_prompt_file: "adapter-file-reference"
    requires_json_output: true
    json_output_args: ["--json"]
    windows_path_strategy: "argv-array; node PATH 명시 필요"
    required_env:
      PATH:
        - "<node 실행 파일 디렉터리, 필요 시>"
      PYTHONPATH: []
    working_directory: "<repo root>"
    artifact_root: "<repo root 또는 provider가 읽을 수 있는 artifact root>"
    approval_policy: "unverified"
    sandbox_policy: "workspace-write"
    dangerous_bypass_required_for_dogfood: false
    config_schema_smoke:
      status: "not_claimed"
      command:
        argv: ["codex", "exec", "--json", "<short config-schema smoke prompt or artifact path>"]
        payload: "artifact-file"
      result: "not_claimed: service_tier config key compatibility not measured in this repo"
      not_claimed:
        - "service_tier schema compatibility"
  claude:
    verified_cli_version: "<현 repo에서 실측한 버전>"
    model: "<profile/model pin 또는 unverified>"
    known_argv_shape: ["claude", "-p", "<short prompt with artifact path>", "--output-format", "json"]
    max_inline_prompt_chars: 2000
    supports_stdin_prompt: "unverified"
    supports_prompt_file: "adapter-file-reference"
    requires_json_output: true
    json_output_args: ["--output-format", "json"]
    windows_path_strategy: "argv-array"
    required_env:
      PATH: []
      PYTHONPATH: []
    working_directory: "<repo root 또는 artifact를 읽을 수 있는 cwd>"
    artifact_root: "<review_artifact가 위치한 readable root>"
    approval_policy: "not-applicable"
    sandbox_policy: "external"
    dangerous_bypass_required_for_dogfood: false
    config_schema_smoke:
      status: "unverified"
      command:
        argv: ["claude", "-p", "<short config-schema smoke prompt with artifact path>", "--output-format", "json"]
        payload: "artifact-file"
      result: "unverified"
      not_claimed: []
  gemini-cli:
    verified_cli_version: "<현 repo에서 실측한 버전>"
    model: "<CLI model pin 또는 unverified>"
    known_argv_shape: ["gemini", "<cli args>"]
    max_inline_prompt_chars: 2000
    supports_stdin_prompt: "unverified"
    supports_prompt_file: "adapter-file-reference"
    requires_json_output: false
    windows_path_strategy: "argv-array; API/key backend 금지"
    required_env:
      PATH: []
      PYTHONPATH: []
    working_directory: "<repo root 또는 artifact를 읽을 수 있는 cwd>"
    artifact_root: "<artifact root>"
    approval_policy: "unverified"
    sandbox_policy: "external"
    dangerous_bypass_required_for_dogfood: false
    config_schema_smoke:
      status: "unverified"
      command:
        argv: ["gemini", "<short config-schema smoke prompt or artifact path>"]
        payload: "artifact-file"
      result: "unverified"
      not_claimed: []
```

provider capability의 `verified_cli_version`은 실측하지 않았으면 값을 채우지 않는다. 미검증 capability는
`unverified`로 남기고, 그 기능에 의존하는 실행은 `BLOCKED` 또는 파일 기반 fallback을 선택한다.
`config_schema_smoke` 역시 실측 전에는 `verified`로 쓰지 않는다. 특히 Codex의 `service_tier` 같은 config key 호환성은
live smoke 결과가 없으면 `not_claimed` 또는 `unverified`로 남긴다. smoke 명령도 긴 payload를 argv에 싣지 않고 artifact 파일 경로와 짧은 지시문만 전달한다.
`model`, `approval_policy`, `sandbox_policy`, `working_directory`, `artifact_root`, `required_env`는 실행 가능한 capability의 일부다.
특히 dogfood에서만 필요한 위험한 bypass는 `dangerous_bypass_required_for_dogfood: true`로 명시하고 범용 기본값처럼 숨기지 않는다.
reviewer가 읽어야 하는 `review_artifact`는 reviewer process의 `working_directory`/`artifact_root` 안에 있어야 한다.

## 코드 컨벤션 (프로젝트 언어/스택)
- 객체/모듈 패턴: `<예: 싱글톤 GetInstance/Init/UnInit, 또는 DI/factory>`
- 직렬화/주요 라이브러리: `<예: nlohmann/json, pydantic ...>`
- 경로/저장 규칙: `<예: PathManager 경유, application-owned output 영역>`
- 빌드 특이점: `<예: 강제 선행 include(pch), 플랫폼 전용 헤더 → 신규 파일 자기완결>`
- 스레드/동시성: `<예: 콜백 스레드에서 UI·블로킹 금지, I/O는 bounded queue/writer로 분리>`

## 프로젝트 전략 (프롬프트 [전략 리마인더]에 못박을 것)
- 수집/구현 전략: `<예: collect-max-then-prune, 또는 minimal-first + 안전 가드>` — "수집/구현 가능 ≠ 지금 다 구현."
- 무수정 대상: `<손대면 안 되는 본문 로직·매크로·외부 제어 경로>`
- 검증 레벨 정의: `<unit/deterministic/live/E2E 중 이 프로젝트의 PASS 기준>`
