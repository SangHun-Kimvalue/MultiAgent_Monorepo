# `cross_runtime_lint.py` — cross-runtime 정적 게이트

> **이 문서가 T4 트랙의 정본이다.** `methodology/docs/PHASE_CYCLE_ORCHESTRATOR_DESIGN.md` §10의
> T4 행은 여기를 가리키는 포인터만 갖는다(2026-07-31 분리 — §10은 부팅 스캔용 진행 SoT라
> 검증 상세를 담는 자리가 아니다).

## 무엇인가

모노레포의 런타임(acp/ztr)이 **각자 다른 venv**를 쓰기 때문에, 루트에서 한 번 도는 린트로는
런타임 변경을 못 잡는다. 이 CLI가 runtime을 매핑해 각 venv로 위임하는 **결정론 게이트**다.

- **runtime 매핑**: `acp`/`ztr` → 각 venv
- **범위**: ruff = **변경 파일** · mypy = **runtime 전체**
- **계약**: exit `0/1/2` + **마지막 stdout 줄에 JSON**(R5 — 코드는 이 둘만 분기한다)
- skip한 경우 **사유를 기록**한다(조용한 skip 금지)

```bash
python methodology/lint/cross_runtime_lint.py --repo . --files acp
```

## 핸드오프 바인딩

`.claude/phased-handoff.config.md:127`에 lint leg로 바인딩돼 있다(test-cmd 체인 패턴).
⚠️ **cwd는 relay가 상속하는 repo 루트 그대로**다 — leg는 cwd를 바꾸지 않는다
(ztr `LESSON-028`. 외부 repo Mechanical은 타깃을 명시해야 한다).

## 완료 기록

### 도구 구현 (2026-07-01, `f7dadf0`)

오케스트레이터 드라이브로 만들었다: codex implementer → **독립 Claude 리뷰 2라운드**.

1R이 **실측으로 P2×4 적발**:
- git 예외 누출
- 삭제된 파일이 `E902`로 오분류
- **cp949 JSON 오염**(Windows 기본 인코딩)
- 빈 `--files`에서 diff 폴백

→ 전부 수정, 2R `REVIEW_PASSED`.

게이트: pytest **16** · ruff/mypy green · 스모크 3분기.

### 바인딩 (2026-07-10)

14일간 고아 상태였던 WIP를 **사용자 승인 후 채택**(`4836add`) →
mypy 4건 narrowing 해소 + **strict 승격**(`50cc7bf` — `--strict` 26파일 **0 에러**, pytest 250) →
`cross_runtime_lint --files acp` **exit 0** 전제 충족 → `.claude/phased-handoff.config.md`에 lint leg 바인딩.

### 부수 적발 → 해소: 전면 스테일 경로 (2026-07-10)

바인딩 중에 config의 relay 템플릿·leg argv가 **`D:\MultiAgent_Monorepo`·`C:\Users\user` 시절
절대경로**로 전면 스테일임이 드러났다. 전 경로를 현 루트 기준으로 **재실측**해 재바인딩했다:

- codex/claude = npm shim `0.142.2` / `2.1.176`
- node = Program Files (구버전 데스크톱 exe 배제)
- `gpt-5.5` pin 제거 — `0.142.2` 무pin 정상 세션 실측

재바인딩 템플릿으로 **결정론 relay 스모크 3-leg PASS** 실증. **T4 전체 종결.**

## 관련

- 구현 지시서: `docs/handoff/T4_CROSS_RUNTIME_LINT_PROMPT.md`
- 바인딩: `.claude/phased-handoff.config.md`
- 트랙 로그 원문: `methodology/docs/archive/T4_TRACK_LOG_20260731.md`
