# Zero-Token Roundtable — Codex 작업 지침

## 프로젝트 개요
- 멀티 에이전트 코드 리뷰/작성 자동화 시스템
- Codex CLI, Gemini API, Ollama를 조율하여 코드 품질 향상
- 정액제(Max 구독) 기반 운용, API 종량제는 옵션

## 기술 스택
- **언어**: Python 3.12+ (asyncio 기반)
- **설정**: PyYAML + Pydantic V2
- **HTTP**: httpx AsyncClient
- **테스트**: pytest + pytest-asyncio
- **타입 체크**: mypy (strict)

## 교훈 문서 (필수 숙지)
- **docs/LESSONS_LEARNED.md** — 과거 실패와 교훈 기록. 새 세션 시작 시 반드시 읽을 것.
- 특히 LESSON-001(Windows .CMD), LESSON-002(asyncio.Lock), LESSON-003(순환 import)은 반복 발생 가능성 높음.
- 새 교훈 발견 시 LESSON-NNN 형식으로 해당 파일 맨 아래에 즉시 추가.

## 핵심 원칙
1. **OCP**: 새 에이전트 추가 시 파일 하나만 생성. `__init_subclass__`로 자동 등록.
2. **Subprocess 안전**: communicate + wait_for + finally kill 3단 방어.
3. **에러 3단 방어**: timeout → retry → fallback.
4. **실측 데이터로 판단**: 추측 금지, 로그부터 추가.
5. **3회 반복 금지**: 같은 축을 3번 파도 안 되면 다른 변수 탐색.
6. **교훈 즉시 기록**: 새 이슈 해결 시 LESSONS_LEARNED.md에 추가. "로그 부족 → 추가 → 재테스트" 반복 금지와 같은 맥락.

## 사용자 환경
- **언어**: 한국어 (Korean)
- **플랫폼**: Windows 11, Python 3.13, bash shell
- **인코딩**: 파일 열 때 반드시 `encoding='utf-8'` 사용

## 커밋 규칙
- 한국어 커밋 메시지 사용
- feat/fix/refactor/docs 프리픽스 사용
