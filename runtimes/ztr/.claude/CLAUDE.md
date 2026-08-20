# Zero-Token Roundtable — Claude Code 작업 지침

## 프로젝트 개요
- 멀티 에이전트 코드 리뷰/작성 자동화 엔진(v2). MAM 모노레포 `runtimes/ztr`로 흡수됨.
- 헤드리스 CLI leg 조율: **Claude CLI · Codex CLI · Ollama**(프리필터). run-phase relay가 핵심 표면.
- **⚠️ Gemini API 백엔드 금지**(크레딧 만료·Non-Goal 확정). 필요 시 Gemini **CLI** leg로만(D7 바인딩 값).
- 정액제(Max 구독)·CLI 기반 운용.
- **진행 SoT**: 스위트 오케스트레이터 트랙 = `../../methodology/docs/PHASE_CYCLE_ORCHESTRATOR_DESIGN.md §10`.
  ztr 내부 로드맵 = `docs/ROADMAP_V2.md`(Phase 1~8 完 — cross-컴포넌트 진행은 §10 포인터).

## 기술 스택
- **지원 런타임**: Python `>=3.12` (asyncio 기반, `pyproject.toml` 정본)
- **실행 인터프리터**: `runtimes/ztr/.venv`를 사용하고 세션 시작 시 버전을 실측한다(2026-08-10: Python 3.13.5).
- **설정**: PyYAML + Pydantic V2
- **HTTP**: httpx AsyncClient
- **테스트**: pytest + pytest-asyncio
- **타입 체크**: mypy strict, `python_version = "3.12"` 고정(최저 지원 런타임 기준 — **3.13 타깃 금지**, `docs/ROADMAP_V2.md` §0 결정 로그. 실행 인터프리터 버전과 별개)

## 교훈 문서 (필수 숙지)
- **docs/LESSONS_LEARNED.md** — 과거 실패와 교훈 기록. 새 세션 시작 시 반드시 읽을 것.
- 특히 LESSON-001(Windows .CMD), LESSON-002(asyncio.Lock), LESSON-003(순환 import)은 반복 발생 가능성 높음.
- 새 교훈 발견 시 LESSON-NNN 형식으로 해당 파일 맨 아래에 즉시 추가.

## 핵심 원칙
1. **OCP**: 새 에이전트 추가 시 파일 하나만 생성. `__init_subclass__`로 자동 등록.
2. **Subprocess 안전**: communicate + wait_for + finally kill 3단 방어.
3. **에러 3단 방어**: timeout → retry → fallback. 단 폴백은 반드시 명시적으로 보고하고 silent success를 금지한다(C3).
4. **실측 데이터로 판단**: 추측 금지, 로그부터 추가.
5. **3회 반복 금지**: 같은 축을 3번 파도 안 되면 다른 변수 탐색.
6. **교훈 즉시 기록**: 새 이슈 해결 시 LESSONS_LEARNED.md에 추가. "로그 부족 → 추가 → 재테스트" 반복 금지와 같은 맥락.

## 사용자 환경
- **언어**: 한국어 (Korean)
- **플랫폼**: Windows 11, bash shell
- **인코딩**: 파일 열 때 반드시 `encoding='utf-8'` 사용

## 커밋 규칙
- 한국어 커밋 메시지 사용
- feat/fix/refactor/docs 프리픽스 사용
