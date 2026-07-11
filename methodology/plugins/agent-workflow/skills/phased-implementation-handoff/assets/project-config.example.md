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
