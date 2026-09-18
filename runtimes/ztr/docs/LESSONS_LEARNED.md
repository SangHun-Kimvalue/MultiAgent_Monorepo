# Zero-Token Roundtable — 교훈 문서

> 이 문서는 개발 과정에서 발견한 실패와 교훈을 기록한다.
> **새 세션의 Claude는 반드시 이 문서를 숙지하고 같은 실수를 반복하지 않아야 한다.**

---

## 인덱싱 규칙

- ID 형식: `LESSON-NNN` (순번)
- 카테고리 태그: `[환경]` `[비동기]` `[설계]` `[Python]` `[Windows]` `[통합]`
- 각 교훈은 **상황 → 증상 → 원인 → 해결 → 원칙** 구조로 기록
- 새 교훈 추가 시 이 파일 맨 아래에 append

---

## LESSON-001: Windows `.CMD` 확장자와 subprocess [환경] [Windows]

**상황**: `ClaudeCodeAgent`에서 `shutil.which("claude")`로 바이너리 확인 후, `asyncio.create_subprocess_exec("claude", "--version")`을 호출.

**증상**: `shutil.which()`는 `C:\...\claude.CMD`를 찾아서 성공. 하지만 `subprocess_exec("claude")`는 `[WinError 2] 지정된 파일을 찾을 수 없습니다` 에러.

**원인**: Windows에서 `shutil.which()`는 PATHEXT(`.CMD`, `.EXE` 등)를 포함하여 탐색하지만, `create_subprocess_exec()`는 정확한 파일명이 필요하다. `"claude"`만으로는 `claude.CMD`를 찾지 못함.

**해결**: `initialize()`에서 `shutil.which()` 결과(전체 경로)를 `self._binary_path`에 저장하여 이후 모든 호출에서 사용.

```python
resolved = shutil.which(self._binary_path)
self._binary_path = resolved  # "claude" → "C:\...\claude.CMD"
```

**원칙**: Windows에서 subprocess 호출 시 `shutil.which()`로 resolved된 전체 경로를 항상 저장하라. 짧은 이름("claude")을 그대로 쓰면 안 된다.

---

## LESSON-002: asyncio.Lock()은 이벤트 루프 안에서만 생성 가능 [비동기] [Python]

**상황**: `AgentRegistry`의 클래스 변수로 `_lock = asyncio.Lock()`을 선언.

**증상**: Python 3.12+에서 모듈 import 시 `RuntimeError` 또는 `DeprecationWarning`. 특히 pytest가 테스트를 collect하는 시점(이벤트 루프 없음)에 발생.

**원인**: Python 3.10부터 `asyncio.Lock()`은 실행 중인 이벤트 루프가 필요. 모듈 레벨(클래스 변수)에서 생성하면 import 시점에 이벤트 루프가 없으므로 실패.

**해결**: lazy 초기화 패턴 사용. 첫 사용 시점(항상 async 컨텍스트)에 생성.

```python
_lock: ClassVar[asyncio.Lock | None] = None

@classmethod
def _get_lock(cls) -> asyncio.Lock:
    if cls._lock is None:
        cls._lock = asyncio.Lock()
    return cls._lock
```

**원칙**: `asyncio.Lock`, `asyncio.Event`, `asyncio.Queue` 등의 동기화 객체를 모듈/클래스 레벨에서 직접 생성하지 마라. 항상 lazy init 또는 async 컨텍스트 내에서 생성.

---

## LESSON-003: __init_subclass__ ↔ Registry 순환 import [설계] [Python]

**상황**: `IAgent.__init_subclass__`에서 `AgentRegistry.register_class()`를 직접 호출하려 함. `base.py`에서 `from registry import AgentRegistry`, `registry.py`에서 `from base import IAgent` → 순환.

**증상**: `ImportError: cannot import name 'AgentRegistry' from partially initialized module`.

**원인**: `base.py`와 `registry.py`가 서로를 import하면 Python의 모듈 로딩 순서에서 한쪽이 아직 완전히 로드되지 않은 상태에서 다른 쪽을 참조하게 됨.

**해결**: `_DEFERRED_REGISTRATIONS` 버퍼 패턴. `base.py`에 리스트를 두고, `__init_subclass__`는 여기에 append만 함. `registry.py`의 `discover()`가 이 리스트를 드레인.

```python
# base.py
_DEFERRED_REGISTRATIONS: list[tuple[str, type[IAgent]]] = []

def __init_subclass__(cls, **kwargs):
    if cls.agent_type:
        _DEFERRED_REGISTRATIONS.append((cls.agent_type, cls))

# registry.py
@classmethod
def discover(cls) -> int:
    while _DEFERRED_REGISTRATIONS:
        agent_type, klass = _DEFERRED_REGISTRATIONS.pop(0)
        cls.register_class(agent_type, klass)
```

**원칙**: 자동 등록 패턴(metaclass, `__init_subclass__`)에서 등록 대상(Registry)을 직접 import하지 마라. 버퍼를 두고 나중에 드레인하는 패턴을 쓰라.

---

## LESSON-004: conftest의 autouse fixture와 auto-registration 충돌 [설계] [Python]

**상황**: `conftest.py`에 `@pytest.fixture(autouse=True)` `reset_registry()`가 매 테스트 전에 `AgentRegistry.reset()`을 호출. `test_claude_code.py`에서 `AgentRegistry.discover()`를 호출해도 `claude_code_cli`가 등록 안 됨.

**증상**: `assert "claude_code_cli" in AgentRegistry._class_registry` 실패. `_class_registry`에 `FakeAgent`만 있음.

**원인**: `reset()`이 `_class_registry`와 `_DEFERRED_REGISTRATIONS` 모두 초기화. `ClaudeCodeAgent`의 `__init_subclass__`는 모듈 import 시 딱 1번만 실행되므로, reset 후에는 deferred 버퍼에도 없고 registry에도 없는 상태.

**해결**: 해당 테스트에서 명시적으로 `AgentRegistry.register_class("claude_code_cli", ClaudeCodeAgent)` 호출.

**원칙**: autouse fixture로 전역 상태를 리셋할 때, `__init_subclass__` 같은 1회성 등록이 소실되는지 확인하라. 필요하면 테스트에서 명시적으로 재등록.

---

## LESSON-005: Nitpicker Python import 시 sys.path 오염 주의 [통합] [Python]

**상황**: `NitpickerAgent.initialize()`에서 `sys.path.insert(0, nitpicker_src)`로 Nitpicker 패키지를 import 가능하게 함.

**잠재 위험**: Nitpicker의 의존성(google-genai, watchdog, pyzmq 등)이 ZeroTokenRoundtable의 네임스페이스에 노출됨. 버전 충돌 가능.

**현재 대응**: `sys.path` 추가 전에 중복 확인 (`if nitpicker_src not in sys.path`). Nitpicker의 핵심 모듈(`jemmin.agents.fast_gate`, `jemmin.models`)만 import하고 전체 orchestrator는 사용하지 않음.

**원칙**: 외부 프로젝트를 sys.path로 통합할 때는 최소한의 모듈만 import하라. 전체 패키지를 끌어오면 의존성 지옥에 빠진다. 장기적으로는 Nitpicker를 `pip install -e` 가능한 패키지로 만드는 것이 안전.

---

## LESSON-006: Pydantic extra="forbid"와 점진적 스키마 확장 충돌 [설계] [Python]

**상황**: `RoundtableConfig`에 `model_config = ConfigDict(extra="forbid")`를 설정한 상태에서, `agents.config.yaml`에 `prompt_adapter` 섹션을 추가.

**증상**: `pydantic_core.ValidationError: Extra inputs are not permitted [prompt_adapter]`

**원인**: `extra="forbid"`는 스키마에 정의되지 않은 필드를 모두 거부한다. 새 설정 섹션을 YAML에 먼저 추가하고 Pydantic 모델에 반영하지 않으면 즉시 에러.

**해결**: `PromptAdapterConfig(BaseModel)`을 정의하고 `RoundtableConfig`에 `prompt_adapter` 필드 추가.

**원칙**: `extra="forbid"`는 오타 방지에 좋지만, **YAML 먼저 수정 → 코드 나중 수정** 패턴과 충돌한다. 새 설정 섹션 추가 시 반드시 스키마 모델부터 업데이트하라.

---

## LESSON-007: Gemini 모델명 Deprecation과 403/404 혼동 [환경] [API]

**상황**: E2E 테스트에서 `gemini-2.5-flash` 모델을 호출했으나 403 PERMISSION_DENIED 발생. 동일 키로 다른 프로젝트에서는 정상 동작.

**증상**: 403 응답이지만 실제 원인은 API 키가 특정 Google Cloud 프로젝트에 바인딩되어 있어서 해당 프로젝트에서 Generative Language API가 비활성화 상태였음. 키를 교체하니 정상 동작.

**추가 발견**: `gemini-2.0-flash` 모델명은 "no longer available to new users" 404 반환. 모델이 deprecated되면 기존 사용자도 접근 불가해질 수 있음.

**해결**: API 키 교체 + 모델 목록(`client.models.list()`)으로 사용 가능한 모델 사전 확인.

**원칙**: Gemini API 403/404 발생 시 (1) API 키의 프로젝트 바인딩 확인, (2) 모델 deprecation 여부를 `models.list()`로 확인하라. 에러 메시지만으로는 원인 구분이 어렵다.

---

## LESSON-008: Windows 콘솔 cp949 인코딩과 유니코드 특수문자 [환경] [Windows]

**상황**: E2E 테스트의 `print()` 출력에 em dash(`\u2014`, `—`) 문자 사용.

**증상**: `UnicodeEncodeError: 'cp949' codec can't encode character '\u2014'`. 테스트 자체는 성공했지만 출력 단계에서 실패.

**원인**: Windows 콘솔의 기본 인코딩이 cp949(EUC-KR). em dash는 cp949에 없는 문자. Python의 `print()`가 콘솔 인코딩으로 변환 시 실패.

**해결**: 테스트 출력에서 `—` → `-` (ASCII 하이픈)으로 교체. 또는 `runner.py`처럼 `sys.stdout` 래퍼로 UTF-8 강제.

**원칙**: Windows 환경에서 `print()` 출력에 유니코드 특수문자(em dash, 화살표 등)를 쓰지 마라. ASCII 대체문자를 쓰거나 `sys.stdout` 인코딩을 UTF-8로 래핑하라.

---

## LESSON-009: Critic 응답 포맷 가이드의 효과 검증 [설계] [LLM]

**상황**: Phase 5-1 E2E에서 Gemini Critic에게 `[STATUS: PASS|CONDITIONAL|REJECTED]` + `[BLOCKERS: N] [MAJORS: N] [MINORS: N]` 포맷 가이드를 프롬프트에 주입.

**결과**: Gemini가 포맷 가이드를 **정확히 준수**. 첫 두 줄에 STATUS + 카운트 라인을 출력하고, 아래에 severity/finding 쌍을 구조화하여 기술.

**의미**: LLM에게 구조화된 응답 포맷을 명시적으로 요구하면, keyword 파싱의 성공률이 극적으로 높아진다. ConsensusEngine의 `status_keyword` 파서가 100% 매칭.

**원칙**: LLM 응답을 파싱해야 할 때는 (1) 프롬프트에 명시적 포맷 가이드를 넣고, (2) 파서에 3단계 fallback(구조화→카운트→휴리스틱)을 두라. 가이드 없이 자유 형식 파싱은 신뢰성이 낮다.

---

## LESSON-010: 같은 LLM이 Writer와 Critic을 맡으면 자기 코드도 REJECT한다 [설계] [LLM]

**상황**: 실전 실행에서 claude-primary가 "Auto mode unavailable"로 모두 실패. fallback으로 gemini-pro가 Writer와 Critic 모두 담당하게 됨. 5라운드 연속 `[STATUS: REJECTED]`.

**원인**: 젬민이(jemini_critic) 페르소나가 "수석 아키텍트가 질책하는" 톤으로 설계되어, 자기가 쓴 코드에도 blocker/major를 찾아낸다. 동일 모델이 양쪽을 맡으면 Writer가 Critic의 기대를 100% 만족시키기 어려움.

**해결 방향**:
1. `balanced_critic`를 기본 페르소나로 변경 (PASS 기준 완화)
2. Writer: gemini-2.5-pro, Critic: gemini-2.5-flash로 모델 분리 (비용 절감 + 관점 차이)
3. 동일 에이전트가 Writer+Critic 모두 맡을 경우 consensus_threshold를 낮춤

**원칙**: Writer와 Critic은 반드시 다른 모델 또는 다른 페르소나를 사용하라. 같은 LLM이 양쪽을 맡으면 자기 확인 편향의 반대 극단(과도한 자기 비판)이 발생한다.

---

## LESSON-011: Starlette 1.0 TemplateResponse 시그니처 변경 [환경] [Python]

**상황**: FastAPI + Starlette 1.0에서 `templates.TemplateResponse("template.html", {"request": request, ...})` 호출.

**증상**: `TypeError: unhashable type: 'dict'`. Jinja2 캐시가 dict를 키로 사용하려다 실패.

**원인**: Starlette 1.0에서 `TemplateResponse`의 시그니처가 변경됨. 이전: `(name, context_dict)`. 이후: `(request, name, context_dict)`. request를 dict 안에 넣지 않고 첫 번째 인자로 분리.

**해결**: `templates.TemplateResponse(request, "template.html", {"key": value, ...})`

**원칙**: FastAPI/Starlette 메이저 버전 업그레이드 시 TemplateResponse, Middleware, Router 시그니처 변경을 반드시 확인하라.

---

## LESSON-012: LLM Critic의 PASS 기준은 3단계로 튜닝해야 한다 [설계] [LLM]

**상황**: Phase 6에서 Critic 페르소나를 3번 반복 조정함.

**1차 (jemini_critic)**: "가차 없이 비판, 강력히 질책" -> 5라운드 연속 REJECTED. 스타일과 설계 철학 차이를 blocker/major로 분류. 사실상 어떤 코드도 통과 불가.

**2차 (balanced_critic v1)**: "동작하면 PASS" -> Critic이 "코드가 훌륭합니다" 1줄로 PASS. 리뷰의 가치가 없음. findings 0건.

**3차 (balanced_critic v3)**: "동작은 PASS의 충분조건이 아니다. 구조/OOP 우수해야 PASS" + "최소 3건 findings" + "SOLID 위반은 major -> CONDITIONAL" -> Critic이 4건 findings와 구조 평가 Summary를 포함하여 CONDITIONAL 판정.

**교훈**: LLM Critic 튜닝은 3가지 축을 동시에 조정해야 한다:
1. **판정 기준**: PASS/CONDITIONAL/REJECTED의 구체적 조건 (어떤 이슈가 어떤 판정에 해당하는지)
2. **출력 강제**: "최소 N건 findings", "Summary N줄 이상" 같은 양적 제약
3. **관점 분리**: 엄격 항목(구조/OOP/안전) vs 관대 항목(스타일/네이밍) 명시

**원칙**: Critic 페르소나는 한 번에 완성되지 않는다. 실측 데이터(실제 Critic 응답)를 보고 반복 교정하되, "엄격/관대" 이분법이 아닌 "어떤 항목이 어떤 판정에 매핑되는지"를 구체적으로 정의하라.

---

## LESSON-013: LLM 출력 하네스의 누적 효과 [설계] [하네스]

**상황**: Writer가 "설명 텍스트 + 코드"를 혼재 출력 -> ASTMerger가 ast.parse 실패 -> 파일 반영 불가. Critic이 "코드가 훌륭합니다" 1줄로 PASS -> 리뷰 품질 제로.

**해결**: 파이프라인의 모든 LLM 입출력에 하네스를 적용.
- H1 Writer Output: [CODE_START]/[CODE_END] 태그 강제 + ASTMerger 3단 추출
- H4 Quality Gate: Writer(빈 함수/짧은 코드 감지) + Critic(칭찬만/verdict 불일치 감지)
- H5 Round Diff: difflib 코드 변경률로 "개선 중인지" 실측 판단
- H6 Post-Merge: 반영 후 ast+ruff+mypy 사후 검증

**결과**:
- 파일 반영 성공률: 0% -> 100% (method=tag로 코드 정확 추출)
- 무의미한 반복: 5라운드 -> 2라운드 이내 확정 (diff 기반)
- Critic 품질: "빈 PASS" 자동 감지 + 재시도 요청

**원칙**: LLM 하네스는 하나만으로는 부족하다. 입력(프롬프트 조합) + 출력(구조 추출) + 품질(검증) + 사후(반영 확인)를 겹겹이 적용해야 실제 동작하는 시스템이 된다. 하네스 없는 LLM 호출은 주사위 굴리기와 같다.

---

## LESSON-014: Writer 프롬프트에 대상 파일 코드를 반드시 주입하라 [설계] [프롬프트]

**상황**: 외부 프로젝트 C++ 프로젝트에 ZTR을 적용. ContextExtractor가 CameraManager.cpp(10K chars)를 수집했지만, PromptAdapter의 `build_writer_prompt`가 `ctx['target_content']`를 프롬프트에 포함하지 않았음.

**증상**: Claude가 "리뷰할 C++ 코드가 제공되지 않았습니다"라고 응답. 코드를 보지 못한 채 질문만 던짐.

**원인**: PromptAdapter가 `diff`와 `decisions`만 프롬프트에 넣고, `target_content`(핵심 파일 내용)를 누락. Python 단일 함수 생성 태스크에서는 문제없었지만, 기존 코드 리뷰/개선 태스크에서 치명적.

**해결**: `build_writer_prompt`에 `ctx['target_content']`를 `## 대상 파일 코드` 섹션으로 추가. 또한 파일 확장자 기반 언어 감지(.cpp=C++, .js=JS) 추가.

**원칙**: LLM에게 "이 코드를 개선하라"고 할 때 코드를 주지 않는 것은 가장 흔한 프롬프트 설계 실수다. 컨텍스트 수집과 프롬프트 조립은 별개 단계이므로, 수집한 데이터가 실제로 프롬프트에 들어가는지 E2E로 반드시 검증하라.

---

## LESSON-015: 전체 파일 덮어쓰기 vs AST 정밀 삽입 [설계] [AST]

**상황**: CameraManager.cpp(478줄) 리뷰에서 Writer가 전체 파일을 15K chars로 재작성. 실제로는 MonitorWorker 함수만 분리하면 되는데 나머지 코드까지 전부 바뀜.

**문제**: 전체 덮어쓰기의 위험:
1. Writer가 원본의 미묘한 로직을 놓쳐서 기능이 깨질 수 있음
2. 무관한 코드까지 변경되어 diff가 비대해짐
3. 리뷰어가 실제 변경을 찾기 어려움

**해결**: `ASTMerger.apply_partial()` — Python `ast` 모듈로 최상위 함수/클래스를 추출하고, 같은 이름이면 교체 + 새로운 이름이면 파일 끝에 추가. 교체 후 `ast.parse`로 문법 검증, 실패 시 전체 덮어쓰기로 fallback.

**한계**: Python only (ast 모듈). C++/JS 등은 여전히 전체 덮어쓰기. Tree-sitter 바인딩으로 확장 가능하지만 현재 scope 밖.

**원칙**: 코드 반영은 "최소 변경 원칙"을 따라야 한다. 전체 파일 덮어쓰기는 편리하지만 위험하다. 가능한 한 함수/클래스 단위로 정밀 교체하고, 실패 시에만 전체 덮어쓰기로 fallback하라.

---

## LESSON-016: Windows stdout 인코딩 보정은 reconfigure를 우선하라 [환경] [Windows] [Python]

**상황**: Windows CLI에서 한국어 출력 깨짐을 막기 위해 import 시점에 `sys.stdout`/`sys.stderr`를 `io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")`로 교체.

**증상**: pytest의 `capfd` 캡처가 CLI 출력 JSON을 잡지 못해 `captured.out`이 비어 있음. 실제 `print()`는 호출되지만 테스트 관점에서는 출력 계약이 깨짐.

**원인**: `sys.stdout` 객체 자체를 새 wrapper로 교체하면 pytest가 설치한 캡처 스트림과 어긋날 수 있다. 특히 import 시점 교체는 테스트 하네스와 충돌하기 쉽다.

**해결**: 객체 교체 대신 가능한 경우 `stream.reconfigure(encoding="utf-8", errors="replace")`를 사용한다.

```python
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
```

**원칙**: Windows 인코딩 보정은 스트림 객체 교체보다 `reconfigure()`를 우선하라. 출력 캡처, 로깅, 테스트 하네스와의 호환성이 훨씬 좋다.

---

## LESSON-017: Windows PowerShell UTF8 파일 입력은 BOM을 허용하라 [환경] [Windows] [Python]

**상황**: `ztr gate` dogfood 중 PowerShell `Set-Content -Encoding UTF8`로 JSON 파일을 만들고 `json.loads(path.read_text(encoding="utf-8"))`로 읽음.

**증상**: JSON 내용은 유효하지만 `Unexpected UTF-8 BOM`으로 `BLOCKED` 처리됨.

**원인**: Windows PowerShell 버전/명령에 따라 `UTF8` 출력이 BOM을 포함할 수 있다. Python의 `json.loads()`는 문자열 첫 글자의 BOM을 JSON 토큰으로 인정하지 않는다.

**해결**: 외부 입력 JSON 파일은 `encoding="utf-8-sig"`로 읽어 BOM이 있으면 제거하고, 없으면 일반 UTF-8처럼 처리한다.

```python
data = json.loads(path.read_text(encoding="utf-8-sig"))
```

**원칙**: Windows 사용자가 직접 만든 JSON/YAML 입력 파일은 BOM 가능성을 고려하라. 내부 소스 파일은 UTF-8을 유지하되, CLI 입력 파일 로더는 `utf-8-sig`를 허용하면 반복 실패를 줄일 수 있다.

---

## LESSON-018: Relay outer envelope의 model 필드는 임의 CLI 모델을 대표하지 않는다 [설계] [통합]

**상황**: Phase 6 `ztr run-phase` dogfood에서 Codex CLI live leg를 실행했다. relay outer envelope는 설정의 `implementer-reviewer.model` 값을 사용해 `model="sonnet"`으로 출력했지만, 실제 step은 `codex exec`였고 캡처 stderr에는 `model: gpt-5.5`가 기록됐다.

**증상**: outer envelope만 읽으면 실제 실행 모델을 잘못 이해할 수 있다. 특히 `run-phase`는 임의 CLI 명령을 받으므로 한 개의 설정 모델명이 전체 relay 실행을 대표하지 못한다.

**원인**: Phase 5 구현이 role binding의 모델 값을 outer envelope metadata에 재사용했다. deterministic relay의 책임은 프로세스 실행과 캡처이지, 외부 CLI 내부 모델 식별을 해석하거나 단정하는 것이 아니다.

**해결**: `run-phase` outer envelope의 `model`은 `external-cli`로 중립화하고, 실제 CLI/모델 증거는 step `command`, stdout/stderr 캡처, 실행 기록 문서에 남긴다.

**원칙**: relay가 제어하지 않는 외부 CLI 내부 모델명은 outer envelope에서 claim하지 마라. 실제 모델 식별은 캡처 증거로 보존하고, 코드가 의미를 해석하지 않도록 중립 metadata를 사용하라. 리뷰 finding을 함께 남길 때는 `severity / finding / evidence_or_repro / impact / recommendation` 5필드를 유지한다.

---

## LESSON-019: 헤드리스 resume용 session id는 stderr가 아니라 JSON stdout으로 캡처하라 [통합] [설계]

**상황**: spike S2(run-phase resume 체인 사전 검증)에서 헤드리스 leg의 session id를 캡처해 다음 페이즈에 재주입하려 함.

**증상**: 과거 dogfood 캡처에서 `session id: [REDACTED]`. 36자 UUID session id가 통째로 소실되어 resume 불가.

**원인**: leg를 JSON 출력 포맷 없이 호출하면 CLI가 session id를 **사람용 텍스트로 stderr에** 출력한다. ztr의 `redact_stderr`(`envelope.py:39`, `_TOKEN_RE = [A-Za-z0-9_-]{32,}` — 하이픈 포함)가 UUID를 한 토큰으로 보고 `[REDACTED]` 치환. redaction은 **stderr 전용**(`envelope.py:115`, `phase_relay.py:274`)이고 stdout은 원문 보존되는데, id가 하필 stderr로 샌 것이 문제였다.

**해결**: leg를 구조화 출력으로 호출해 id를 stdout으로 이동시킨다(코드/ redaction 변경 불필요).
- claude: `claude -p "..." --output-format json` → stdout JSON `session_id`.
- codex: `codex exec --json "..."` → stdout 첫 줄 `{"type":"thread.started","thread_id":"..."}`.
- `phase_relay.py`는 leg stdout 전체를 `NN-<leg>.stdout.txt`로 저장(`:258`)하고 `stdout_preview`(4000자, `:273`)에도 담으므로, 거기서 id를 파싱하면 redaction을 거치지 않는다. (라이브 시연: `run-phase`로 claude leg 실행 → 캡처 파일에서 `session_id` 회수 → `--resume`로 직전 페이즈 토큰 재현 성공.)

**원칙**: 헤드리스 CLI에서 코드가 회수해야 할 **사실**(session id 등)은 사람용 stderr 로그가 아니라 기계용 **구조화 stdout(JSON)**에서 캡처하라. 보안 위생(stderr redaction)이 회수 경로를 먹지 않도록 **캡처 경로와 위생 경로를 분리**한다. R5: id는 문자열 사실일 뿐, 세션 내용을 파싱하지 않는다.

---

## LESSON-020: 사용자 레벨 PATH의 npm 전역 CLI는 비-사용자 셸에서 node를 못 찾는다 [환경] [Windows]

**상황**: git-bash(Bash 도구)에서 `codex`를 실행하려 함. node(v24, `%LOCALAPPDATA%\Programs\nodejs` 사용자 zip)와 codex(npm 전역, `%APPDATA%\npm`)는 **사용자 레벨 PATH에만** 등록됨.

**증상**: `codex: command not found`. 풀패스 `codex.cmd --version`도 `'node'을(를) ... 인식할 수 없습니다`로 실패. (claude는 단일 `.exe`라 풀패스만으로 동작.)

**원인**: git-bash/서비스 셸은 사용자 레벨 PATH를 상속하지 않는다. `codex.cmd` 래퍼는 내부적으로 `node`를 PATH에서 찾는데 node가 없어 실패. 즉 CLI 본체뿐 아니라 그 인터프리터(node)까지 PATH에 있어야 한다.

**해결**: 비-사용자 셸에서는 (1) node 디렉터리를 PATH에 주입 후 (2) `.cmd`가 아니라 확장자 없는 **sh shim** 호출.
```bash
export PATH="/c/Users/<user>/AppData/Local/Programs/nodejs:$PATH"
/c/Users/<user>/AppData/Roaming/npm/codex exec --json "..." < /dev/null   # < /dev/null = stdin EOF (spike S1 교훈)
```

**원칙**: 사용자 레벨 PATH에만 있는 도구를 헤드리스/타 셸에서 부를 때는 **풀패스 + 인터프리터 디렉터리(node)를 명시적으로 PATH에 주입**하라. ROADMAP §0 spike S1의 "PATH 사용자 레벨 — 전체 경로 폴백 고려" 주의와 직결. (relay의 `RelayCommand.resolved_argv`가 `shutil.which`로 풀패스화하므로, ztr를 실행하는 셸 자체에 node PATH가 있어야 codex leg가 산다.)

---

## LESSON-021: Windows 헤드리스 자동화는 argv에 큰 payload를 싣지 말라 [환경] [Windows] [통합]

**상황**: Phase 8 구현 후 설계 Reviewer leg를 실행하려고 긴 diff와 신규 파일 본문을 `claude -p "<prompt>"` 명령줄 인자로 전달했다. 이후 stdin 전달로 우회하려 했지만 해당 CLI 호출 형태의 stdin prompt 동작이 검증되지 않아 timeout이 발생했다.

**증상**:
- Windows `CreateProcess`에서 `[WinError 206] 파일 이름이나 확장명이 너무 깁니다` 발생.
- shell별 quoting/JSON 직렬화 차이로 `--implementer-cmd` JSON 배열이 깨지거나 CLI까지 도달하지 못함.
- 긴 리뷰 payload를 즉석 명령줄/채팅 프롬프트로 조립하면서 문자 포맷, 인코딩, 줄바꿈 문제가 반복됨.

**원인**:
- Windows 명령줄 길이 한도와 PowerShell quoting 규칙이 자동화 payload 크기와 충돌한다.
- 각 외부 CLI가 prompt를 argv/stdin/file 중 어디서 안정적으로 받는지 provider별 계약으로 정리되지 않았다.
- "사람이 한 번 치는 명령"과 "오케스트레이터가 반복 생성하는 명령"의 안전 기준이 분리되어 있지 않았다.

**해결**:
- 큰 payload(diff, 파일 본문, 리뷰 요청, 다중 문서 컨텍스트)는 argv에 직접 싣지 않고 임시 파일/캡처 파일/검증된 stdin 경로로 전달한다.
- provider adapter마다 `max_inline_prompt_chars`, `supports_stdin_prompt`, `supports_prompt_file`, `requires_json_output`, `known_argv_shape`를 명시한다.
- Windows 자동화에서 shell 문자열 조립을 피하고, 가능한 한 Python `subprocess`의 argv 배열 또는 ztr의 JSON argv 배열을 사용한다.
- 외부 CLI의 stdin prompt 지원은 추정하지 말고 live smoke로 검증한 뒤에만 규약화한다. 미검증이면 "파일에 payload 저장 + 짧은 argv로 파일 경로 전달"을 기본값으로 둔다.

**원칙**: Windows 헤드리스 자동화의 기본 단위는 "긴 명령줄"이 아니라 **작은 argv + 파일 기반 payload + 구조화 stdout**이다. 설계 세션은 이 원칙을 ztr만의 교훈이 아니라 phase-cycle-orchestrator/project.config의 공통 실행 규약으로 승격해야 한다.

---

## LESSON-022: 간헐 테스트 실패는 환경성과 코드 결함을 먼저 가르라 [환경] [비동기] [Windows]

**상황**: Phase 8/config 커밋 검토 중 전체 스위트(`pytest -q`, 212개)가 기본 PYTHONHASHSEED에서 간헐적으로 1건 실패. 실패 테스트가 실행마다 달라짐(`test_invariants`/`test_e2e_integration` 등 invariants·CLI·e2e 계열).

**증상**: 동시에 여러 도구(파일 읽기·git·병렬 bash)를 돌려 시스템 부하가 높을 때 일부 실행에서 1건 실패. 그러나 **단독/순차 실행 15/15 green**, **고정 PYTHONHASHSEED(0~6) 전부 green**, 단일 파일 실행도 green.

**원인**: 코드 결함이 아니라 **부하 유발 async subprocess 타이밍 flakiness**. 실측 근거(C2): ① CLI/e2e 테스트가 `git`/`ztr`를 async subprocess로 띄우는데 부하 시 타이밍이 흔들림 ② 출력은 결정적 — `collect_changed_paths`의 `seen` set은 **멤버십 dedup 전용**이고 결과 리스트는 순서 보존, runner stdout은 list/dict 순서, set 직렬화 비결정성 없음 ③ `run_subprocess_tool`은 LESSON-001 3단 방어(communicate+wait_for+finally kill) 정상.

**해결**: 재현되는 결함이 없으므로 **정상 코드를 추측으로 고치지 않는다**(C2 — "관측 없는 경로 = 없는 경로"). 간헐 실패는 격리/순차/고정 시드 재실행으로 환경성 여부부터 판정. 재현되면 그때 traceback 기반 수정.

**원칙**: 간헐 테스트 실패를 보면 (1) 단독·순차·고정 시드로 **재현성부터 판정**, (2) 출력 결정성·subprocess 방어를 코드로 확인, (3) **재현되는 결함에만** 손댄다. 환경성(부하 타이밍)을 코드 버그로 오인해 정상 경로를 고치면 새 결함만 생긴다. CI 결정성이 필요하면 PYTHONHASHSEED 고정(C6)은 별개의 선택지일 뿐 이 flakiness의 원인 수정은 아니다.

---

## LESSON-023: 모노레포 서브트리에서 invariants 경로 — git diff(repo-root)와 ls-files(cwd) base가 다르다 [환경] [통합]

**상황**: ztr가 모노레포 `runtimes/ztr/` 서브트리로 흡수된 뒤 `ztr invariants`를 서브디렉터리에서 실행.

**증상**: `m2_finding_terms` 체크가 BLOCKED — `runtimes/ztr/docs/handoff/DESIGN_FOLLOWUP...`(존재하지 않는 경로) 읽기 실패.

**원인**: `.git`이 모노레포 루트에 있어 서브디렉터리에서 git을 돌리면 — `git diff --name-status`는 **repo-root 상대**(`docs/handoff/...`), `git ls-files --others`는 기본 **cwd 상대**(`src/...`)를 반환한다. base가 달라, 둘을 같은 base로 가정해 `self._root`에 join하면 어긋난다. (a) ls-files의 cwd-상대 경로가 prefix 필터에서 "밖"으로 오판돼 누락되거나, (b) repo-root 경로가 ztr 루트에 잘못 join돼 미존재 파일 읽기 → BLOCKED.

**해결**: `git ls-files`에 `--full-name`을 줘 **둘 다 repo-root 상대로 통일**하고, ztr prefix(`runtimes/ztr/`) 밖 경로는 필터링한다(`invariants.py` `_changed_paths`/`_to_root_changed_path`).

**원칙**: 서브디렉터리에서 git을 돌릴 때 명령마다 **경로 base가 다를 수 있다**(diff=repo-root, ls-files=cwd 기본). 여러 git 출력을 합쳐 경로로 쓰기 전에 base를 `--full-name` 등으로 통일하라. 모노레포/서브트리 도구는 git 루트와 자기 루트의 prefix를 명시 처리한다.

---

## LESSON-024: Orchestrator dogfood는 argv·cwd·provider 승인 정책까지 실측해야 한다 [환경] [Windows] [통합]

**상황**: `phase-cycle-orchestrator` 1페이즈 dogfood에서 `ztr run-phase`가 Codex implementer, Claude reviewer, ztr Mechanical leg를 순차 실행했다.

**증상**:
- PowerShell 변수로 JSON argv 문자열을 넘기자 quote가 손상되어 `RelayCommand.from_text()`의 JSON parse가 실패했다.
- Codex를 `--ignore-user-config`로 실행하자 기본 모델이 계정에서 지원되지 않는 값으로 떨어져 400 오류가 발생했다.
- Codex leg가 exit 0을 반환했지만 patch가 read-only approval에 막혀 실제 파일 변경이 없었다.
- ztr를 `runtimes/ztr` cwd에서 실행하자 Claude reviewer가 repo root의 review artifact 파일을 읽지 못했다.
- `runtimes/ztr/.venv/Scripts/ztr.exe`는 repo root에서 `src` import를 찾지 못했다.

**원인**:
- Windows 자동화에서 shell 문자열과 JSON quoting을 섞으면 argv 배열 계약이 쉽게 깨진다.
- provider capability가 실행 파일/버전만으로는 부족하고, 모델 pin, 승인 정책, working directory, required environment까지 포함해야 한다.
- reviewer artifact는 파일 경로만 넘긴다고 충분하지 않고, process의 readable root/cwd와 맞아야 한다.
- console script packaging이 monorepo cwd를 아직 안정적으로 지원하지 않는다.

**해결**:
- `ztr run-phase` 자체 호출은 Python `subprocess.run([...])`로 argv 배열을 구성했다.
- Codex leg는 `-m gpt-5.5`를 명시하고 dogfood 한정으로 `--dangerously-bypass-approvals-and-sandbox`를 사용했다.
- ztr run-phase는 repo root cwd에서 실행하고 `PYTHONPATH=D:\MultiAgent_Monorepo\runtimes\ztr`와 explicit `--config`를 주입했다.
- Mechanical leg는 별도 argv wrapper로 `runtimes/ztr`에 chdir한 뒤 실행했다.
- 실패/교정 이력과 설계 follow-up을 `docs/handoff/DOGFOOD_ORCHESTRATOR_REPORT.md`와 `docs/handoff/DESIGN_FOLLOWUP_ORCHESTRATOR_DOGFOOD_RULES.md`에 기록했다.

**원칙**: 오케스트레이터 dogfood의 PASS는 단순히 CLI가 존재한다는 뜻이 아니다. 반복 실행 명령은 shell 문자열이 아니라 argv 배열로 구성하고, provider capability에는 모델·승인 정책·cwd·env·artifact root까지 실측값을 기록하라. exit code 0은 "프로세스 성공"이지 "파일 변경 성공"을 자동 증명하지 않으므로, driver report가 changed paths/diff evidence를 별도로 남겨야 한다.

---

## LESSON-025: codex `-s workspace-write`는 Windows에서 셸 leg를 깨뜨린다 — 무인 write는 `--dangerously-bypass` [환경] [Windows] [통합]

**상황**: 외부 프로젝트 trial smoke(2026-06-15)에서 codex implementer leg를 외부 프로젝트 worktree에 `--cd`로 진입시켜 실제 코드를 편집·검증시키려 했다. LESSON-024는 read-only approval에 막혀 patch가 무반영(exit0인데 변경 없음)인 경우를 다뤘으므로, 그 연장으로 `-s workspace-write`를 주어 쓰기를 허용하려 했다.

**증상**: `-s workspace-write`에서 model-generated 셸 명령(예: `pwsh git status`)이 전부 `windows sandbox: CreateProcessAsUserW failed: 5`로 실패했다. 단, `apply_patch` 파일 편집 자체는 성공했다 — 즉 "파일은 써지는데 셸을 도는 스텝은 죽는" 부분 실패라 exit code/표면 로그만 보면 오해하기 쉽다.

**원인**: codex의 Windows sandbox(`workspace-write`) 구현이 자식 프로세스를 제한 토큰으로 띄울 때 `CreateProcessAsUserW`가 권한 오류(5=ACCESS_DENIED)를 낸다. 파일 쓰기는 sandbox 내부 경로라 통과하지만, 셸 명령은 별도 프로세스 생성이 막힌다. 이 PC 한정 실측이며 LESSON-024의 read-only 차단과는 **다른 실패 모드**(024=승인 게이트, 025=sandbox 프로세스 생성).

**해결**: 셸을 실제로 도는 leg(빌드·git·테스트 호출이 필요한 무인 implementer)는 `--dangerously-bypass-approvals-and-sandbox`로 실행한다 — smoke에서 파일 편집 + 셸(`git status` exit0) 둘 다 클린, thread_id 캡처 정상. read-only review leg는 승인/sandbox bypass가 불필요하다(편집·셸 미수행).

**원칙**: Windows codex에서 sandbox 정책은 "쓰기 허용/금지"의 2분법이 아니라 **셸 프로세스 생성 가능 여부**까지 갈린다. `workspace-write`는 apply_patch는 되고 셸은 깨질 수 있으므로, 셸 의존 무인 leg는 dogfood 한정 `--dangerously-bypass`로 두고 capability(`sandbox_policy`/`dangerous_bypass_required_for_dogfood`)에 **실측 모드별로** 기록하라. bypass는 범용 기본값으로 승격하지 않는다. (LESSON-024 상호참조 — 승인정책 차단과 sandbox 프로세스 실패는 별개.)

`[환경][Windows][통합]`

---

## LESSON-026: codex 헤드리스 — 비-git cwd는 `--skip-git-repo-check`, prompt는 stdin(파일), MCP noise는 비차단 [환경] [Windows] [통합]

**상황**: codex implementer를 헤드리스로 구동(외부 프로젝트 worktree 및 비-git 디렉터리 양쪽 smoke, 2026-06-15). prompt를 어떻게 안정 전달할지, 비-git 위치에서 어떻게 행을 푸는지, MCP 인증 noise가 차단인지 확인이 필요했다.

**증상**:
- 비-git 디렉터리에서 codex가 `Not inside a trusted directory`로 거부.
- prompt를 argv로만 줘도 codex가 **stdin을 읽으려 대기**해 헤드리스에서 행(hang)이 발생.
- 다수의 `127.0.0.1:<port>/mcp AuthorizationRequired` 메시지가 stderr에 쏟아짐.

**원인**:
- codex는 git repo 안을 신뢰 기준으로 삼는다. 외부 프로젝트은 worktree(=git)라 무관하지만, 임의 비-git 작업폴더는 trust 체크에 걸린다.
- codex `exec`는 prompt를 stdin으로 받는 경로가 있어, stdin이 열린 채면 EOF를 기다린다(LESSON-020/021 stdin-EOF 계열과 동근).
- 헤드리스 환경엔 MCP 인증 콜백을 받을 브라우저/세션이 없어 인증이 실패하지만, 이는 본작업과 무관한 부가 기능이라 exit code에 영향 없음.

**해결**:
- **비-git cwd만** `--skip-git-repo-check`를 붙인다(외부 프로젝트 worktree는 불필요).
- prompt는 argv에 싣지 말고 **파일에 써서 stdin으로 리다이렉트**한다(payload-as-file + stdin-EOF). 즉 codex의 `supports_stdin_prompt`는 "파일을 stdin으로 먹이면 verified"로 좁혀 규약화한다(임의 inline stdin 일반 주장 아님).
- MCP auth noise는 **비차단(EXIT0)** 으로 간주하되, 헤드리스 leg는 **MCP 비활성**을 권장해 로그 오염을 줄인다.

**원칙**: 외부 CLI를 헤드리스로 자동화할 때 (1) trust/repo 전제(git 여부)는 cwd 종류에 따라 분기 플래그로 명시하고, (2) prompt는 argv가 아니라 **파일+stdin**으로 줘 길이/quoting/hang을 동시에 회피하며, (3) 인증·텔레메트리 noise는 exit code로 차단성을 판정하되 헤드리스엔 해당 부가기능을 꺼라. (LESSON-021 payload-as-file·LESSON-024 argv/cwd 상호참조.)

`[환경][Windows][통합]`

---

## LESSON-027: ztr pytest 전체 스위트는 `runtimes/ztr` cwd에서 — MAM 루트면 review CLI 7건 fail [환경] [Windows] [Python]

**상황**: 외부 프로젝트 trial 환경 점검 중 ztr 회귀를 MAM 모노레포 루트에서 `pytest runtimes/ztr/tests`로 돌렸다(2026-06-15 실측, P1에서 재현).

**증상**: MAM 루트 cwd → **7 failed, 243 passed**. 실패는 전부 review/recording/e2e 계열(`test_review_cli`·`test_recording_cli`·`test_e2e_integration`)로, `cmd_review`가 `SystemExit(70)`(INTERNAL_ERROR)을 냈다. `runtimes/ztr` cwd → **250 passed**.

**원인**: 이 테스트들은 review CLI에 상대 경로(예: `pyproject.toml`)를 넘기고, CLI가 ruff/mypy 같은 정적 도구의 **runtime root를 cwd 기준으로 탐지**한다. MAM 루트에서 돌리면 runtime root 탐지가 모노레포 루트(ztr `pyproject.toml`이 아닌 곳)를 짚어 정적 게이트가 깨지고 exit 70이 된다. LESSON-023(git diff=repo-root vs ls-files=cwd base 차이)과 **동근의 cwd-base 함정**이다 — 거기선 invariants 경로, 여기선 정적도구 root.

**해결**: ztr pytest 전체 스위트는 **`runtimes/ztr`를 cwd로** 실행한다(운전 규칙). 코드 가드(conftest `chdir`/testpaths 절대화)는 **적용하지 않음(YAGNI)** — 실패 테스트가 의도적으로 cwd-상대 경로를 넘기는 hermetic 설계라, 세션 전역 `chdir`로 마스킹하면 진짜 cwd-의존 버그를 가리고 `tmp_path` 격리와 충돌한다. 근본은 "맞는 cwd에서 실행"이라는 운영 규칙이므로 LESSON으로 잠그고 코드는 건드리지 않는다.

**원칙**: 서브트리 런타임의 테스트는 그 런타임 루트를 cwd로 돌려라. cwd-base 의존 실패(diff/ls-files/정적도구 root)는 LESSON-023과 같은 부류이며, 증상을 전역 `chdir`로 덮기보다 **실행 위치를 규약화**하는 편이 hermetic 테스트 설계를 보존한다. (LESSON-023 상호참조.)

`[환경][Windows][Python]`

---

## LESSON-028: run-phase leg는 cwd를 바꾸지 않는다 — 외부 repo Mechanical은 타깃을 명시하라 [통합] [Windows]

**상황**: A10 외부 throwaway trial에서 `ztr run-phase` 4-leg(implementer→mechanical→test→reviewer)를 MAM repo root에서 실행하면서, mechanical leg로 MAM `methodology/nitpicker/run_nit.py`를 사용하려 했다.

**증상**: `phase_relay.py`의 leg 실행은 `asyncio.create_subprocess_exec(...)`에 `cwd`를 넘기지 않으므로 모든 leg가 부모 cwd(MAM)를 상속한다. codex implementer는 `--cd <throwaway>`로 외부 repo에 진입할 수 있지만, 기존 `run_nit.py`는 `REPO = Path.cwd()`라 그대로 호출하면 throwaway가 아니라 MAM diff를 리뷰한다. 이렇게 되면 relay가 PASS해도 외부 repo Mechanical 검증을 했다고 주장할 수 없다.

**원인**: run-phase의 계약은 leg argv 실행과 envelope 라우팅이지 per-leg cwd 관리가 아니다. 외부 repo를 대상으로 하는 provider는 각자 argv나 옵션으로 working directory를 명시해야 한다. 특히 Mechanical 도구가 cwd 기반이면 relay 부모 cwd에 묶여 잘못된 repo를 볼 수 있다.

**해결**: `run_nit.py`에 `--repo <path>` 옵션을 추가해 기본값은 기존 cwd로 유지하되, 외부 relay에서는 `run_nit.py --repo <throwaway> --changed`처럼 타깃 repo를 명시한다. A10 smoke에서 `nit_envelope.py --style runnit -- <python> run_nit.py --repo <throwaway> --provider mock --changed`로 실행해 mechanical stdout이 `===== mod.py =====`를 보고했고, 최종 `ztr run-phase` envelope는 4/4 PASS와 session-map 캡처를 남겼다.

**원칙**: `run-phase` leg는 cwd를 자동으로 바꾸지 않는다. 외부 repo trial에서는 implementer의 `--cd`, mechanical의 `--repo`, test command의 절대 경로처럼 **각 leg argv에 타깃 repo를 명시**하라. cwd 래퍼나 임시 `os.chdir`보다 도구의 공식 옵션이 안전하며, green 판정 전에 mechanical stdout이 실제 타깃 파일을 가리키는지 확인하라.

`[통합][Windows]`

---

## LESSON-029: LLM status 파서는 헤더 한정 + 줄-시작 토큰 + 프롬프트 계약을 함께 잠가라 [Python] [Mechanical] [C3]

**상황**: A12에서 `methodology/nitpicker/run_nit.py`의 Ollama 응답 status 판정이 `first_line.startswith("ALL PASS")`에 의존해, 모델이 `**STATUS: ALL PASS**`처럼 마크다운/접두를 붙이면 실제 승인 의도도 `BLOCKED`로 오판될 수 있었다.

**증상**: 상태 토큰이 의미상 존재해도 첫 줄 문자열이 정확히 `ALL PASS`/`CHANGES_REQUESTED`로 시작하지 않으면 false-BLOCKED가 난다. 반대로 본문 전체를 느슨하게 검색하면 리뷰 본문 중 "BLOCKED라는 단어" 같은 우발 등장으로 false-BLOCKED/false-green 위험이 생긴다.

**원인**: LLM prose 출력은 장식·접두·도입문이 쉽게 붙는다. 코드가 prose 의미를 해석하지 않더라도, 상태 토큰 위치/형식을 명확히 제한하지 않으면 C3 게이트가 출력 스타일에 흔들린다.

**해결**: `_extract_status(output)`로 status 추출을 분리하고 첫 비공백 줄부터 최대 5줄만 헤더로 본다. 각 줄은 앞쪽 마크다운(`*`, `#`, `>`, 백틱)과 `STATUS:`/`RESULT:` 접두만 제거한 뒤, 줄이 토큰으로 시작할 때만 인정한다. 우선순위는 `BLOCKED > CHANGES_REQUESTED > ALL PASS`, 5줄 안에 토큰이 없으면 `BLOCKED`다. 동시에 `build_prompt()`에 "첫 줄은 status 토큰만 단독 출력, 마크다운·접두·잡설 금지" 계약을 명시해 파서 방어와 모델 계약을 함께 잠갔다.

**원칙**: Mechanical 게이트가 LLM 텍스트를 받아야 한다면, 파서는 헤더·토큰·우선순위만 보수적으로 인정하고 모호한 출력은 `BLOCKED`로 닫아라. 파서만 느슨하게 만드는 것은 재발 방지책이 아니며, 프롬프트 출력계약도 함께 강화해야 한다. 더 근본적인 구조화(`format` JSON Schema/enum)는 별도 페이즈로 분리해 검증하라.

`[Python][Mechanical][C3]`

---

## LESSON-030: reviewer verdict가 exit code에 없으면 relay가 false-PASS — stdout 토큰으로 분기 + fail-closed [통합] [C3] [Windows]

**상황**: 2026-06-18 CubiForge 12Z-G(IP/abuse 사전필터)를 `ztr run-phase`로 구현하던 중, reviewer leg(`claude -p`)가 독립 측정 후 **BLOCKED 판정**(implementer가 추가한 trademark 행에 필수 `blocked_id` 누락 → 테이블 로드 실패 회귀)을 냈는데, top envelope는 `status=PASS exit 0`으로 기록됐다.

**증상**: `phase_relay._route_exit_code`는 child `exit 0`을 무조건 `Verdict.PASS`로 매핑한다. `claude -p`는 리뷰 결론과 무관하게 프로세스가 성공하면 exit 0을 반환하므로, reviewer의 BLOCKED가 envelope에서 PASS로 둔갑한다. 교차 reviewer가 회귀를 잡았어도 **게이트가 작동하지 않는다**(false-PASS). mechanical(run_nit)은 tracked JSON만 봐 보강도 못 했다.

**원인**: relay가 "모든 leg는 verdict를 exit code로 신호한다"고 가정했다. mechanical/test/implementer는 맞지만, prose 리뷰어는 exit code에 verdict를 싣지 않는다. exit code 단일 신호 가정이 reviewer 계열에 대해 깨진다.

**해결**: `RelayCommand.verdict_source`("exit_code" 기본 | "stdout_token") 추가. reviewer leg는 stdout의 합의된 토큰 `ZTR_VERDICT: PASS|CHANGES_REQUESTED|BLOCKED`(마지막 토큰 채택)로 분기하고, **토큰이 없으면 fail-closed BLOCKED**. `ztr run-phase --reviewer-verdict-source`(기본 `stdout_token`)로 노출. 정의된 토큰 1개를 읽는 것이라 R5(산문 의미 미해석)와 일치. EXECUTION_ADAPTER_CONTRACT §2.1에 계약화. 회귀: ztr 전체 254 passed(신규 relay 4 + CLI 테스트는 reviewer fake가 토큰 emit하도록 갱신).

**원칙**: exit code는 "프로세스 성공"이지 "리뷰 verdict"가 아니다. verdict를 exit code로 신호하지 않는 leg는 구조화 토큰으로 받고, 형식 위반은 PASS가 아니라 BLOCKED로 닫아라(게이트는 fail-closed). 리뷰 프롬프트에 "마지막 줄에 ZTR_VERDICT 단독 출력" 계약을 함께 잠가야 토큰 파서가 실제로 채워진다. (LESSON-029 status 파서 보수성과 동근 — 파서만이 아니라 출력계약도 함께.)

`[통합][C3][Windows]`

---

## LESSON-031: codex는 self-lint 불가(Windows) + run-phase 단일패스 → codex 페이즈는 ztr leg만으로 clean PASS 불가, autofix+휴먼피드백 배선 필요 [통합] [Windows] [Mechanical]

**상황**: CubiForge 12Z-G 인간-게이트 N페이즈 체이닝 dogfood에서, 게이트 결함 A/B/C/D를 차례로 고친 뒤 mechanical(run_nit --include-untracked)이 codex 신규파일을 실제 리뷰하게 됐다. 그러자 매 codex 페이즈가 mechanical에서 CHANGES_REQUESTED로 멈췄다(예: I001 import 미정렬).

**증상**: `ztr run-phase`를 다시 돌려도 clean PASS가 안 닫힌다. 재실행 = implementer(codex) 재호출 = 비결정적 재생성이라 같은 클래스 lint가 다시 등장한다.

**원인**: 세 가지가 겹친다 — (1) codex가 Windows workspace-write sandbox에서 ruff/mypy 셸을 못 돌려(LESSON-025) 자기 lint를 발견·자가수정 못 함, (2) run-phase는 단일 패스(implementer→mechanical→reviewer)라 구현과 mechanical 사이 수정 단계가 없음, (3) mechanical findings가 implementer로 자동 피드백되지 않음(자동 수정 루프는 오케스트레이터 설계상 범위 밖). 따라서 "맹목 재실행"으로는 루프가 닫히지 않는다. codex는 발견은 못 해도 **지시받으면 apply_patch로 수정은 가능**하다는 점이 핵심(불가가 아니라 배선 부재).

**해결(방향, 별도 페이즈)**: (ㄴ) implementer 직후·mechanical 전에 결정론 autofix 스텝(`ruff --fix`+format, verdict 없음)으로 자동수정 가능한 lint 제거. (ㄱ) mechanical/reviewer findings를 다음 implementer resume 프롬프트에 주입해 재호출 — 단 **사람이 트리거하는 휴먼-게이트**로만(자동 루프 금지, R5/페이즈경계 사람확인). 구현 핸드오프: CubiForge `docs/handoff/ztr_autofix_feedback_phase_prompt.md`.

**원칙**: 실행자가 자기 출력을 검사할 수 없는 환경(여기선 Windows codex)에서는 단일패스 run-phase의 leg 게이트만으로 그 실행자의 페이즈를 clean PASS시킬 수 없다. ztr는 "구현 + advisory 신호"를 내고, 권위 있는 검증·lint수정·커밋은 아웃터 휴먼 루프가 맡는 것이 실측된 운영 모델이다(= "완전 무인"이 범위 밖인 이유와 동근). 게이트를 더 빡세게 만들수록(A/B/C/D) 이 사실이 더 분명히 드러난다 — 게이트 강화와 autofix/피드백 배선은 함께 가야 한다.

`[통합][Windows][Mechanical]`

## LESSON-032: autofix(ㄴ)는 non-gating leg로, 휴먼피드백(ㄱ)은 프롬프트 빌더로 — verdict 모델을 깨지 않고 LESSON-031 배선 [통합] [Mechanical]

**상황**: LESSON-031의 두 배선을 ztr에 구현했다. relay는 모든 leg를 verdict(exit_code/stdout_token)로 게이트하고 첫 non-PASS에서 멈추며, 이전 leg payload를 다음 leg stdin으로 흘린다. autofix는 이 모델에 그대로 끼우면 안 된다 — `ruff check --fix`는 잔여 위반이 있으면 exit 1을 내는데, 그게 게이트되면 "정정했는데 CHANGES로 멈춤"이 된다.

**해결(ㄴ)**: `RelayCommand.gating: bool`(기본 True)을 추가하고, autofix leg를 `gating=False`로 implementer 직후·mechanical 전에 삽입했다. non-gating leg는 (1) `should_stop`을 세우지 않아 다음 leg를 막지 않고, (2) report verdict/exit_code 집계에서 제외되며, (3) `next_input`을 바꾸지 않는다(autofix는 디스크 파일을 고치고 mechanical은 그 파일을 본다 — payload가 아니라 파일이 전달 매체). 실행오류조차 BLOCKED로 게이트하지 않고 봉투에만 기록한다(파일 변형 전용, verdict 없음). CLI는 `--autofix-cmd`(append, opt-in). R5 유지: autofix는 verdict를 내지 않으므로 "코드가 LLM 의미를 판정"하지 않는다.

**해결(ㄱ)**: findings 피드백을 relay 루프에 넣지 않고 **별도 프롬프트 빌더**(`fix_feedback.py`, `ztr fix-prompt`)로 분리했다. 직전 run-phase report에서 **gating + 미skip + non-PASS** leg findings만 추출해(non-gating autofix는 verdict가 없어 제외) 원본 프롬프트에 주입한 fix-resume 프롬프트를 만든다. 이 명령은 텍스트만 만들고 **다음 라운드 실행은 사람이 발행**한다 — relay가 자동으로 되먹이는 루프가 아니다(R5/휴먼게이트). findings 없으면 exit 2로 fail-closed(주입 불필요).

**메타-검증(dogfood)**: 이 구현 중 새 모듈 `fix_feedback.py`가 bare `dict` 타입(repo의 `type-arg` 규칙 위반)을 가졌고, **이 작업이 살리려는 바로 그 mechanical 게이트**(`mypy src`)가 `ztr run-phase`가 아닌 기존 e2e 테스트에서 그 결함을 잡아냈다. 즉 mechanical은 변경 파일뿐 아니라 `src` 전체를 타입체크하므로, 신규 모듈의 결함이 무관해 보이는 review 테스트를 깨뜨릴 수 있다 — "내 변경과 무관한 테스트 실패"를 timing/flaky로 단정하지 말고 실제 envelope를 까서 원인(여기선 내 코드의 mypy 결함)을 확인할 것. 게이트가 제 일을 했다는 증거이기도 하다.

`[통합][Mechanical]`

## LESSON-033: 첫 in-environment relay dogfood(CubiForge 12Z-H2) — (ㄴ)autofix 발화 검증 + mechanical 900s hang(원인=run_nit 총 리뷰시간 > relay leg timeout; jemmin spooler는 이미 WAL+busy_timeout) [통합] [Mechanical] [Windows]

> ⚠️ **정정-2(2026-06-22, 실측)**: 초판("ollama 본질 hang")·정정-1("jemmin spool.db lock 경합") **둘 다 부정확**. 코드 실측 — jemmin spooler는 이미 `WAL + synchronous=NORMAL + busy_timeout=3000`(`src/jemmin/state/sqlite_spooler.py:29-31`)이라 DB lock이면 **3초에 에러**(900s 불가). run_nit는 per-file ollama timeout **120s**(`run_nit.py:45,210` `future.result(timeout=120)` + [TIMEOUT]). **실제 원인 = 변경파일 N개 × 파일당 ≤120s ollama 리뷰의 누적이 relay 900s leg timeout을 초과** + run_nit stdout 버퍼링이 kill 시 유실(→ 빈 출력). vanilla DB lock도, 단일 ollama hang도 아니다. 동시 ollama 경합 시 파일당 더 느려져 악화될 뿐. 아래 본문은 이 **정정-2** 기준으로 읽을 것(jemmin/spool 코드는 손대지 않는다 — 이미 하드닝됨).

**상황**: CubiForge ADR-0015 Item 4(draft-only enforcement)를 처음으로 *오케스트레이터 세션이 직접* `ztr run-phase` relay로 헤드리스 실행(이전 H1/외부 codex 세션은 relay를 밖에서 돌리고 보고만 전달 → relay 내부 미검증). leg: implementer=codex(exec -s workspace-write --json -) / autofix=(ㄴ) `ruff check --fix .` / mechanical=`run_nit.py --changed --include-untracked --keep-going` / reviewer=claude(stdout_token). per-leg timeout 900s.

**관찰(실측 envelope)**:
- implementer(codex): PASS exit0 — apply_patch로 H2 코드 작성 성공(`promote_fallback_draft` 등). **codex가 Windows에서 헤드리스로 파일편집 OK**(LESSON-025와 일관: 파일편집 가능, 셸은 불가).
- **(ㄴ) autofix leg: PASS exit0, gating=false 확인** — 우리가 만든 non-gating autofix가 실제 relay에서 정확히 발화·게이트 안 함. (H1에서 NOT CLAIMED였던 것을 in-environment로 닫음.) 단 stdout="All checks passed!" — codex 출력이 이미 ruff-clean이라 **이번엔 lint "구제"를 입증하진 못함**(LESSON-031 전제인 I001류가 이 페이즈엔 미발생). autofix의 *가치* 입증엔 실제 lint 결함이 나오는 페이즈가 필요.
- **mechanical(run_nit): timed_out=true, duration=900.04s, exit=124 → BLOCKED.** stdout/stderr 빈 채 900초 killed. reviewer leg는 다운스트림 skip(fail-closed 정상 동작).

**원인(정정-2, 실측)**: jemmin spooler는 이미 WAL+busy_timeout=3000, run_nit는 per-file ollama timeout 120s. 따라서 900s hang = **변경파일 N개 × 파일당 ≤120s ollama 리뷰 누적 > relay 900s leg timeout** + 버퍼링된 stdout이 kill 시 유실. 동시 ollama 호출 경합 시 파일당 지연이 커져 악화. jemmin/spool 코드 결함도, vanilla DB lock도 아니다(그건 3초에 에러). 단독 환경에서 파일 수가 적으면 정상 완주.

**교훈/배선**:
1. **mechanical 레그 총시간을 relay leg timeout 안에 bound하라.** jemmin spooler는 이미 WAL+busy_timeout(추가 동시성 보강 불필요 — B3 스코프 무효 확인). 진짜 레버: (a) relay mechanical을 **결정론·고속**(`ruff`/`mypy` 직접 또는 `--provider mock`)으로 — live-ollama nitpick은 비차단 advisory로 분리; (b) 불가피하게 ollama nitpick을 게이트에 두면 `relay --timeout > (변경파일수 × file-timeout 120s)` 또는 run_nit 파일수/`--max-lines` 제한으로 누적시간을 bound; (c) 단독 환경 권장은 유효(동시 ollama 경합 회피). LESSON-034의 "CHANGES 경로는 결정론 leg"와 동근.
2. **relay를 외부에서 돌리고 보고만 받으면 leg 내부(특히 (ㄴ)(ㄱ))를 검증 못 한다.** dogfood는 오케스트레이터가 *직접* 돌려 envelope/`.ztr/run-phase/<run>/NN-*.envelope.json`을 까야 한다. "leg가 돌았다고 보고됨"을 그대로 PASS로 옮기지 말 것(R12; H1에서 (ㄴ) 성공을 미검증 주장한 사례 정정).
3. **(ㄱ) fix-resume는 여전히 미exercise** — 이번엔 mechanical이 findings(CHANGES)가 아니라 timeout(BLOCKED)으로 죽어 fix-resume 입력이 안 생김. fix-resume dogfood엔 mechanical/reviewer가 *깨끗이 CHANGES findings*를 내는 라운드가 필요.
4. **positive**: relay 헤드리스 구동·leg 순차·non-gating autofix·fail-closed(timeout→BLOCKED→reviewer skip)는 설계대로 작동 확인.

`[통합][Mechanical][Windows]`

## LESSON-034: (ㄱ) fix-resume 풀루프 in-env 검증 + CHANGES 경로를 codex/ollama 없이 결정론 dogfood하는 기법 [통합] [Mechanical]

**상황**: LESSON-033에서 (ㄱ) fix-resume가 0회 미검증으로 남았다(mechanical이 CHANGES가 아니라 DB-lock timeout으로 죽어 입력이 안 생김). 또 full codex relay는 느리고 비결정적(CHANGES가 날지 불확실)이라 (ㄱ)를 안정적으로 exercise하기 어려웠다.

**기법(결정론 CHANGES dogfood)**: codex/ollama/jemmin 없이 (ㄱ)를 끝까지 돌리려면 leg를 **결정론 명령으로 치환**한다:
- implementer = 의도적 결함 파일을 쓰는 작은 python 스크립트(예: unused import → ruff I001/F401).
- autofix/reviewer 생략, mechanical = `python -m ruff check <file>`(결정론, 위반 시 exit 1 + findings stdout).
- → relay가 **진짜 CHANGES report**(mechanical gating non-PASS + 실 findings)를 수초 만에 생성.

**검증 결과(풀루프)**:
1. ROUND1: implementer PASS → mechanical CHANGES_REQUESTED(exit1) + 실 findings(I001/F401) → outer CHANGES.
2. `ztr fix-prompt --prompt-file <orig> --report-file <round1 envelope> --out fix-resume.md`: gating+non-PASS leg findings만 추출(1건) → 원본 프롬프트 + "사람 명시 트리거, 자동 루프 아님" 주입 섹션 생성(계약대로). 바깥 Envelope/report 양형식 로드도 OK.
3. ROUND2: fix-resume를 implementer prompt로 재발행 → 수정본 작성 → mechanical PASS → outer PASS. **루프 종결.**

**교훈**:
- (ㄱ) fix-resume의 추출·주입·재실행 전 경로가 설계대로 작동(휴먼게이트 유지 — fix-prompt는 텍스트만 만들고 사람이 round2를 발행). LESSON-033의 미검증 항목 종결.
- **CHANGES 경로 dogfood는 결정론 leg 치환으로 codex/ollama 의존을 제거**해야 안정적이다. live-agent relay는 happy-path 검증엔 좋지만 CHANGES/오류 경로는 결정론 fixture leg로 따로 닫는 게 빠르고 재현 가능.
- 미검증 잔존: (ㄴ) autofix의 "구제"(실 lint 결함을 autofix가 제거해 mechanical을 살리는 시나리오)는 별도. 본 dogfood는 autofix를 일부러 뺐다(F401이 mechanical에 도달하게).

`[통합][Mechanical]`

## LESSON-035: full clean relay cycle in-env + (ㄴ)autofix 구제 + claude reviewer leg 검증 (LESSON-034 잔여 2갭 종결) [통합] [Mechanical]

**상황**: LESSON-034가 남긴 2개 미검증 — (ㄴ) autofix의 "lint 구제"(실 결함을 autofix가 mechanical 전에 제거), claude reviewer leg in-env 실행 — 을 결정론 fixture relay로 닫았다.

**검증(결정론 dogfood)**:
- implementer = I001(import 미정렬, **autofixable**) 결함 파일 writer.
- autofix = `ruff check --fix <mod.py>` → 정렬로 구제(PASS, gating=false).
- mechanical = `ruff check <mod.py>` → **PASS**(구제됨). 대조: autofix 없는 1라운드에선 동일 I001이 mechanical CHANGES로 검출 → 구제 효과 입증.
- reviewer = `claude -p "...ZTR_VERDICT..."` → **in-env 실행 + stdout_token 라우팅 PASS**(stdout에 claude JSON).
- outer = **PASS**: implementer→autofix→mechanical→reviewer 4-leg 전부 PASS = 첫 full clean cycle in-env.

**교훈**:
- (ㄴ) autofix 가치 입증엔 **autofixable 결함(I001 등)** 이 필요(F401·E501 등 일부는 --fix 가능/불가 갈림; E501은 비가역). LESSON-033의 "autofix 발화는 봤으나 구제 미입증"을 종결.
- claude reviewer leg는 ollama/jemmin과 무관(네트워크 호출)이라 spool 경합 없이 in-env 검증 가능.
- **fixture gotcha**: autofix/mechanical 타깃을 **디렉터리가 아니라 특정 파일로** 좁혀라. helper writer 스크립트의 non-autofixable lint(E501 긴 주석 등)가 dir-level `ruff check`를 오염시켜 mechanical을 false-CHANGES시킨다(1라운드 실측). mechanical 입력 범위를 좁혀 결정론·bound로(LESSON-033 정정-2와 동근).

`[통합][Mechanical]`

## LESSON-036: codex 헤드리스 안정화 — MCP noise는 `--ignore-user-config`로, SoT-burn은 scoped preamble로 (LESSON-026 supersede) [환경] [Windows] [통합]

**문제(실측 2026-06-24, codex 0.140.0, CubiForge γ relay)**: 풀 4-leg 무인 relay가 implementer(codex) 단계에서 반복 실패.
- run#1(무-preamble): codex가 프로젝트 `CLAUDE.md` boot 지시로 대형 SoT(PHASES/HANDOFF/DESIGN)를 pwsh로 재독하다 **48.8s에 exit -1**(미완·파일변경 0).
- run#2(`-c mcp_servers.<name>.enabled=false` 적용): **즉시 exit 1** — `Error loading config.toml: invalid transport in mcp_servers.github`. `GITHUB_PAT_TOKEN` 미설정이라 github MCP transport가 invalid인데, `-c mcp_servers.*` override가 전체 [mcp_servers] strict 재파싱을 유발해 깨짐.
- 공통 배경: notion(미로그인)·orca-slicer(127.0.0.1) MCP가 `rmcp::transport::worker: worker quit with fatal`을 반복 emit(LESSON-026은 "비차단"이라 했으나 안정성/로그를 해침).

**해결**:
- **MCP noise = `--ignore-user-config`**: `~/.codex/config.toml`을 통째로 무시 → MCP 서버 전부 미로드(auth는 `CODEX_HOME` 유지). 검증 2/2: MCP fatal 0, **stderr 0줄**, exit 0. → LESSON-026의 `-c mcp_servers.<name>.enabled=false` 권장을 **폐기**(한 서버라도 invalid transport면 깨짐).
- **SoT-burn = scoped preamble**: implementer prompt 첫머리에 `[범위 고정·self-contained] 진행 SoT/대형 문서 재독 금지 — 대상 파일만`. run#3(preamble 적용) implementer PASS(116.9s, 실제 구현).

**교훈**:
- 헤드리스 codex 표준 argv: `exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check --ignore-user-config -C <repo> --json -` + **scoped-prompt preamble**. 계약 `EXECUTION_ADAPTER_CONTRACT.md §3.1` 정정 반영.
- `-c mcp_servers.*` override는 invalid한 다른 서버까지 strict 재검증시켜 self-inflicted exit 1을 만든다 → 통째 무시(`--ignore-user-config`)가 안전.
- codex는 프로젝트 boot 지시(AGENTS/CLAUDE.md)를 충실히 따르다 scoped sub-task에서 턴을 소진한다 → 오케스트레이터 implementer prompt가 명시적으로 범위를 좁혀야 한다.
- 진짜 무인 E2E의 마지막 flakiness는 run_nit/duckdb(이미 견고)가 아니라 codex leg 환경이었다(재진단).

`[환경][Windows][통합]`

---

## LESSON-037: 게이트 leg 수동 구동 시 외부 하네스 timeout ≥ leg 내부 timeout — 아니면 내부 한도가 무의미 [환경] [Mechanical] [통합]

**상황**: CubiForge Item 2 드라이브(2026-07-01)에서 mechanical leg를 relay 없이 수동 구동:
`timeout 500 python run_nit.py --file-timeout 600 ...`. 그런데 실행 툴(Bash) 기본 한도가 120s라
**외부 하네스가 2분에 leg를 kill** — 내부 `--file-timeout 600`은 한 번도 도달 못 했다. 증상은
"mechanical timeout BLOCKED"로 보이지만 실제로는 **바깥 타이머가 먼저 끊은 것**(LESSON-033의
"run_nit 총 리뷰시간 > relay leg timeout"과 동형 — 이번엔 relay가 아니라 세션 툴 타이머).

**원인**: timeout 스택이 3층(세션 툴 기본 한도 < shell `timeout` 인자 < leg 내부 `--file-timeout`)인데
가장 바깥 층이 가장 짧으면 안쪽 설정 전체가 죽은 설정이 된다. 재발방지 규칙(config `1287acc`)은
`--file-timeout 600`만 올렸고 **바깥 층 정합은 명시하지 않았다**.

**원칙**: leg를 수동/스크립트로 구동할 때 **timeout 위계를 바깥이 가장 길게** 정렬하라:
세션 툴 한도(명시 지정) ≥ shell timeout ≥ leg 내부 file-timeout(+파일 수 배수 여유).
Ollama mechanical 600s 파일이면 세션 툴에도 최소 그 이상을 명시 지정해야 한다. timeout-BLOCKED를
보고할 때는 **어느 층이 끊었는지**(툴 exit 143/-1 vs run_nit rc 2)를 구분해 기록 — 층을 오인하면
"모델이 느리다"로 진단이 왜곡된다. `[환경][Mechanical][통합]`

---

## LESSON-038: run-phase 실질 구현 leg는 600s로 부족 — 타임아웃 튜닝/스코프 분할 [환경][Relay]

**상황**: T10-A dogfood — `run-phase` implementer(codex) leg가 600s에서 TIMEOUT(exit 124)→BLOCKED,
mechanical/test/reviewer 전부 skip. codex는 입력 6.6M 토큰·6파일 편집·자기검증까지 하며 600s 초과.
(codex는 실제로 구현을 끝냈으나 프로세스가 시간 내 종료 못 해 kill.)

**해결/원칙**: 신규 서브커맨드+테스트 같은 실질 구현 leg에 600s는 빠듯하다. run-phase는 단일
`--timeout`을 전 leg에 적용하므로 구현 leg가 무거우면 **timeout을 크게(1800s+) 잡거나 스코프를
더 잘게** 쪼갠다. implementer가 자기검증까지 하면 더 길어진다(→ LESSON-039). `[환경][Relay][Timebox]`
**→ 구조 보완(교훈만으로 부족)**: **T11-F1** run-phase 레그별 타임아웃(§10 T11).

> **완료 정정(2026-08-10)**: T11-F1에서 implementer·autofix·mechanical·test·reviewer별 override와
> `leg override > global > 600s` 계약을 run-phase/fix-round에 구현했다. 단 외부 하네스가 모든 실제
> attempt 합계보다 길어야 한다는 LESSON-037의 운영 책임과 자동 outer-bound enforcement는 여전히 별개다.

---

## LESSON-039: 프롬프트는 실행 모드-aware여야 — 릴레이 모드 implementer는 blind [방법론][Relay]

**상황**: 릴레이로 구동했는데 implementer 프롬프트 `[검증·DoD]`가 **수동-모드**("출력 게이트는
너가 소유·집행")였다. codex가 그대로 자기 pytest/ruff/mypy/nitpicker/claude 리뷰를 돌리고
SoT(§10·HANDOFF)까지 "PASS"로 편집 → 릴레이의 mechanical/test/reviewer leg와 **중복·충돌**,
시간 소진→타임아웃.

**해결/원칙**: **릴레이/오케스트레이터 구동이면 출력 게이트 = 릴레이 leg 소유 → implementer는
구현만, 자기 게이트 실행·SoT 편집 금지(implementer blind, ROADMAP_V2 D6)**. 수동 핸드오프면
implementer가 자기 게이트 집행(phased-handoff §8 소유). 프롬프트 저작 시 §8 **모드 구분**을
반드시 반영한다(prompt-skeleton [검증/DoD]에 모드 분기 명시). `[방법론][Relay][C4]`
**→ 구조 보완(문서 분기만으론 강제 안 됨)**: **T11-F2** implementer 범위 가드 — leg 후 diff가 허용경로 밖 건드리면 deterministic BLOCK(§10 T11).

---

## LESSON-040: R5가 가짜 PASS를 막았다 — implementer 자기주장 vs 릴레이 envelope(권위) [정직성][R5]

**상황**: codex가 자기 leg 안에서 "pytest 282·ruff·mypy·nitpicker·Claude 리뷰 ALL PASS"라 주장하고
SoT를 "출력게이트 PASS, 커밋 대기"로 편집했다. 그러나 릴레이 **envelope(권위)는 BLOCKED**,
그 leg들은 **SKIPPED**(실행 안 됨). codex prose를 믿었으면 미검증 코드 + 거짓 PASS 문서를 커밋할 뻔.

**해결/원칙**: **R5가 정확히 이걸 막는다** — 코드(envelope verdict enum/exit)가 권위, LLM prose
자기주장은 비권위. implementer 자기검증·자기 SoT-PASS-편집은 신뢰 대상이 아니다. 살리기 =
거짓 문서 걷어내고 **오케스트레이터가 진짜 게이트를 재실행**(pytest 282·ruff·mypy·독립 Claude 리뷰
PASS)해 정당하게 확인. codex 수치가 우연히 맞았어도 **당시엔 검증 안 된 것**. 보고-실측 불일치는
항상 실측이 이긴다. `[정직성][R5][Relay]`
**→ 구조 보완**: R5는 *신뢰*를 막았으나 *오염*(implementer의 SoT 편집)은 못 막음 → **T11-F2** 범위 가드가 오염 자체를 구조적으로 차단(§10 T11).

---

## LESSON-041: git 경로 수집은 4요소를 다 갖춰야 완전 — 하나 빠지면 silent fail-open [Mechanical][Relay][R5]

**상황**: T11-F2 범위 가드(금지 경로 편집 시 BLOCK) 구현에서 **수집 완전성 구멍이 라운드마다 하나씩** 나왔다.
① `--diff-filter=ACM`만 → **삭제·rename 우회**(입력게이트 적발) → `ACMRD`+`-M --name-status`로 수정.
② `git ls-files --others`가 **cwd 서브트리 스코프** → 서브디렉 cwd면 밖의 새 untracked를 놓침(리뷰어 실증) → 3개 collect를 `cwd=repo_root`에서 실행.
③ git 기본 `core.quotepath=true`가 **비-ASCII 경로를 인용**(`"...\354\204..."`) → 선행 `"`로 prefix/glob 매칭 실패 = **한글 파일명 금지파일이 조용히 통과**(리뷰어가 임시 repo에서 재현). 이 프로젝트 SoT가 정확히 한글 문서라 보호 대상이 그대로 샜다.
**세 구멍 모두 테스트가 ASCII·repo-root 전제라 300+ green이어도 못 잡았다.**

**해결/원칙**: git 경로 수집의 **완전성 4요소**를 한 세트로 본다 —
**(1) 필터 `ACMRD`(삭제·rename 포함) · (2) `cwd=repo_root`(스코프) · (3) `-c core.quotepath=false`(비-ASCII 인용 해제, 필요시 `-z` NUL 파싱) · (4) base 통일(커맨드마다 다름: `diff`=repo-root 상대 / `ls-files`=cwd 기본, `--full-name`은 출력 base만 바꾸고 스코프는 안 넓힘)**.
하나라도 빠지면 **조용히 통과**(fail-open)하지 시끄럽게 실패하지 않는다. 보안/가드 성격의 수집기는 **회귀 테스트에 비-ASCII·삭제·rename·서브디렉 cwd를 반드시 포함**하라 — ASCII·repo-root만 도는 테스트는 이 클래스를 구조적으로 못 잡는다.

**메타**: ②는 **LESSON-023이 이미 기록한 함정**(`invariants.py`가 `_changed_paths`/`_to_root_changed_path`로 해결)이 **다른 수집 경로엔 미적용**이라 재발한 것 = 교훈 문서만으론 재발을 못 막는다는 실증(→ 구조 강제가 T11의 존재 이유). `[Mechanical][Relay][R5][재발]`

---

## LESSON-042: mechanical ruff가 서브디렉 cwd에서 장기 no-op이었다 — "PASS"가 "검사함"을 뜻하지 않았다 [정직성][Mechanical]

**상황**: 릴레이 mechanical leg는 `runtimes/ztr`로 chdir해 `ztr review --changed`를 돈다. 그런데 `git diff --name-only`는 서브디렉에서도 **repo-root 상대**를 내고, 기존 존재필터가 `(cwd/path).exists()`라 `runtimes/ztr/runtimes/ztr/...`가 되어 **전부 조용히 탈락** → **ruff 대상이 사실상 항상 빈 목록**이었다(mypy만 실제 동작). 두 계열(Claude Planner·Codex 게이트)이 독립 실측 확증.

**해결/원칙**: 과거 릴레이의 "mechanical PASS" 중 **ruff 부분은 공허 green**이었다 — 게이트가 "통과"를 냈다고 "검사했다"가 아니다. 정적 게이트는 **검사 대상 수가 0이 아님을 함께 assert**해야 한다(빈 목록 no-op 차단). T11-F2 경로계약(fix5)이 이걸 부수적으로 해소했고, 회귀 테스트에 "의도한 rule code가 실제 검출됨"을 넣어 공허 green을 구조적으로 막았다. `[정직성][Mechanical][공허green]`

---

## LESSON-043: `codex exec resume`은 `exec`와 인자 표면이 다르고 원래 cwd를 기억하지 않는다 [환경][Resume]

**상황**: fix-round 첫 실전 도그푸드에서 codex 재개가 즉시 실패 — `error: unexpected argument '--cd' found`.
`resume_chain._build_codex_argv`가 `exec` 뒤에 `resume <id>`만 끼우고 **나머지 플래그를 그대로 두기** 때문.

**실측(help diff + 라이브 2-repo 실험, 독립 게이트가 재현)**:
- **resume이 거부하는 exec 전용 플래그 = 8종**: `-C/--cd`·`-s/--sandbox`·`--add-dir`·`-p/--profile`·
  `--oss`·`--local-provider`·`--color`·`-V`. 사전 인지는 `--cd`/`--sandbox` 2종뿐이었다 — **추정 목록은
  항상 좁다**. 값 형태도 3축(long `--f v|=v` / short separated / **short 붙임·`=` 결합** `-CX`,`-C=X`)이라
  하나만 처리하면 나머지가 샌다.
- **cwd**: A에서 `-C <A>`로 만든 thread를 **프로세스 cwd=B**에서 resume하니 **파일이 B에 생성**됐다.
  => **resume은 세션의 원래 cwd를 기억하지 않고 프로세스 cwd를 쓴다.**
- `-i/--image` arity도 다르다(exec `<FILE>...` vs resume `<FILE>`) — `-i a b`는 resume에서 깨진다.
- **claude는 무해**: `-r/--resume`이 서브커맨드가 아니라 **최상위 옵션**이라 표면이 동일하다.

**해결/원칙**: resume argv는 **거부 플래그 strip + `--cd`는 "제거"가 아니라 "전치"**(값 -> 그 leg의
subprocess `cwd=`)여야 한다. 그냥 떼면 **런처 cwd에서 조용히 오편집**한다(= LESSON-041 "누락 시 조용히
통과"의 변종). strip은 **allowlist가 아니라 denylist**로 — allowlist는 미래의 정당한 플래그를 조용히
삭제하지만 denylist의 실패 모드는 **CLI 파서의 시끄러운 에러**다. **값 누락(`--cd --json`처럼 다음 토큰이
`-`로 시작)은 값으로 소비하지 말고 spawn 前 BLOCKED**로 잡아라 — 소비하면 `cwd='--json'` 같은 값이
만들어져 진단이 "내부 오류"로 왜곡된다. `[환경][Resume][R5]`

---

## LESSON-044: 범위 가드는 baseline이 없으면 "릴레이 前 dirty"를 implementer 소행으로 오탐한다 [릴레이][운영]

**상황**: T10-B1a 첫 릴레이가 `implementer-scope-guard` BLOCKED로 죽고 나머지 3 leg가 SKIPPED.
차단 사유는 `methodology/**`·`docs/handoff/**` 편집이었는데, **codex는 그 두 파일을 건드리지 않았다**
(허용된 신규 2파일만 생성). 두 파일은 **릴레이 시작 前부터 미커밋**이던 오케스트레이터의 §10 수정과
Planner의 프롬프트였다.

**원인**: 가드는 implementer 직후 **작업트리 전체 변경집합**(`collect_changed_paths`)을 본다. "언제
생긴 변경인지"를 모르므로 **선행 dirty = implementer 소행**으로 귀속된다. T11-F2 잔여 ④(D7 baseline
commit SHA 스냅샷 미구현)의 **첫 실전 발현**이다.

**해결/원칙**: ① **운영 규칙 — 릴레이 구동 前 작업트리를 커밋으로 비운다.** 가드를 켠 릴레이는
clean baseline을 전제한다. ② 구조적 해소는 **implementer 시작 시점의 baseline(HEAD SHA + 그때의
변경집합)을 스냅샷해 델타만 판정**하는 것 — 후속 F-item. ③ **이 오탐은 fail-closed 방향이라
안전하다**(무해한 변경을 막을 뿐, 위반을 놓치지 않는다) — 그래서 가드를 느슨하게 푸는 방향으로
"고치면" 안 된다. 진단 메시지가 정확했기에(`파일 <- 매칭 패턴`) 오탐 판별이 1분이면 끝났다는 것도
관측: **차단 사유에 매칭된 패턴을 함께 찍어라.** `[릴레이][범위가드][운영]`

---

## LESSON-045: 재적용 terminal state와 operational exit는 직교하며 기존 terminal은 덮지 않는다 [R5][재적용][정직성]

**상황**: T10-B1b 구현 프롬프트 §5.5 입력게이트에서 두 P2가 구현 전에 적발됐다. 초안은 timeout/internal
error의 report 부재 경로를 terminal `null`로만 적어 마지막 라운드 소진을 숨길 수 있었고, prior PASS
early path가 이미 `NO_PROGRESS`/`TIMEBOX_EXHAUSTED`로 닫힌 원장을 `CONVERGED`로 덮을 수 있었다.

**원인**: (1) process 결과 축(status/exit 124·70)과 convergence 축(terminal state)을 하나처럼 취급했고,
(2) 새 report의 PASS를 기존 원장의 종결 provenance보다 높은 권위로 놓았다. 둘 다 실패를 green으로
재분류할 수 있는 정직성 결함이다.

**해결/원칙**: spawn 뒤 결과는 verdict와 무관하게 1라운드를 소비하고 append 뒤 terminal을 계산한다.
여유가 남은 124/70은 terminal `null`, 마지막 라운드를 소비한 124/70은
`TIMEBOX_EXHAUSTED`이되 outer status BLOCKED와 **원래 operational exit 124/70을 그대로 유지**한다.
early PASS/BLOCKED는 terminal이 `None`일 때만 최초 terminal을 기록하고, 이미 종결된 원장은 bytes·mtime까지
바꾸지 않는다. phase mismatch도 early exit보다 먼저 차단한다. 회귀는 state와 exit를 독립 축으로 함께
assert하고, 기존 terminal 비덮어쓰기를 파일 불변성으로 고정한다. `[R5][C3][재적용]`

---

## LESSON-046: Reviewer 실행 모드도 verdict transport 계약이다 — plan prose의 PASS를 게이트로 쓰지 않는다 [R5][Review][운영]

**상황**: T10-B2b 독립 Claude 리뷰 첫 시도에서 `--permission-mode plan`을 사용했다. 리뷰 자체는 끝났지만
CLI는 plan 종료 도구가 없다는 이유로 본문 요약 안에 ``ZTR_VERDICT: PASS``를 넣고 외부 plan 파일을 만든 뒤,
요구한 standalone 마지막 토큰을 출력하지 않았다. wrapper는 token count 0으로 정확히 `BLOCKED/2` 처리했다.

**원인**: read-only를 보장하려고 고른 plan mode가 비대화되어, 비대화형 Reviewer의 실제 완료 계약인
"지금 결과를 출력하고 standalone token으로 닫기"와 충돌했다. prose에는 PASS가 있었지만 R5 권위는
structured envelope/exit와 standalone token뿐이므로 이를 의미판정해 승격할 수 없었다.

**해결/원칙**: Reviewer의 permission mode·allowed tools·stdout token은 한 묶음의 transport 계약이다.
읽기 전용은 `Read/Glob/Grep/Bash` allowlist와 명시적 no-edit 지시로 제한하고, plan mode가 결과 출력을
가로막지 않게 한다. wrapper는 child exit 0이어도 standalone token이 없거나 JSON parse가 실패하면
무조건 BLOCKED한다. 재시도는 `acceptEdits` mode+write 도구 미허용으로 수행해 단독 token 1개를 받아
PASS했다. **본문의 "PASS"를 토큰 대신 해석하거나 plan artifact를 승인으로 간주하지 않는다.** `[R5][C3][Review]`

---

## LESSON-047: 에이전트 지침의 환경값은 지원·실행·분석 타깃을 한 줄로 합치지 않는다 [환경][문서][정직성]

**상황**: ztr의 Codex 지침은 `Python 3.13`, Claude 지침은 `.venv Python 3.12.x`로 서로 달랐고,
Codex 지침은 이미 Non-Goal인 Gemini API를 활성 백엔드처럼 안내했다. 실측 결과 `.venv`는 Python 3.13.5,
`requires-python`은 `>=3.12`, mypy `python_version`은 `3.12`였다.

**원인**: 실행 인터프리터, 지원 하한, 정적분석 타깃이라는 서로 다른 사실을 단일 "Python 버전"으로
압축하고 provider별 자동 로드 문서에 중복 기록해 drift를 만들었다. 배포용 adapter 템플릿도 nested 지침처럼
읽힐 수 있는데 그 범위가 표시되지 않았다.

**해결/원칙**: 환경은 **지원 런타임 / 현재 실행 인터프리터(착수 시 실측) / 분석 타깃**의 세 축으로
분리한다. provider별 보장 채널은 자기완결로 유지하되 공통 사실을 함께 정렬하고, 타 repo용 템플릿에는
활성 지침이 아니라는 배너를 둔다. 과거 backend 이름은 코드·결정 SoT로 확인하고 비용·Non-Goal을
되살리는 stale 안내를 남기지 않는다. `[C2][C3][환경][문서]`

---

## LESSON-048: 명시 경로와 변경집합은 서로 다른 capability다 — Git 의존성과 오류 권위를 입력 모드에 묶어라 [CLI][경로][R5]

**상황**: T11-F4 전에는 `review`/`verify`가 명시 경로를 받았어도 먼저 Git root를 찾았다. 그 결과
non-git cwd나 repo 외부의 existing 파일을 검사할 수 없었고, 같은 repo 밖 입력 오류가 review는
internal `70`, verify는 verdict-derived `2`로 갈렸다.

**원인**: `--changed`의 변경집합 수집 capability와 사용자가 직접 지정한 read-only target capability를
하나의 repo-relative resolver에 결합했고, 예상 가능한 입력 오류도 최종 `Exception` 경계로 흘렸다.

**해결/원칙**: `--changed`만 Git precondition과 기존 repo-relative POSIX collector 계약을 갖는다.
명시 경로는 호출 cwd 기준 normalized absolute로 해석하고 Git probe·diff를 요구하지 않는다. runner에
도달한 missing path/non-git changed 같은 입력 오류는 전용 domain exception으로 `BLOCKED/2`, 예상 밖
내부 오류는 `BLOCKED/70`으로 분리한다. 회귀는 mock resolver만으로 끝내지 말고 **실제 non-git cwd의
subprocess CLI**로 relative/absolute review·verify와 단일 Envelope를 고정한다. `[CLI][경로][R5][E2E]`

---

## LESSON-049: corrective prompt가 frozen 계약을 확장하면 안전성 리뷰가 진단 문자열 최적화로 변질된다 [Review][Timebox][R5]

**상황**: T13-P2 writer는 final artifact·ledger 불변, owned temp 삭제 시도, closed error code와
`BLOCKED/70`을 이미 만족했다. 이후 corrective prompt가 cleanup 실패 detail에 exact `path`,
`state`, `retry_safe`까지 요구했고, 희귀한 `fsync` 실패와 `unlink` 실패의 동시 주입에서 이 세 필드가
빠졌다는 이유로 Mechanical이 P2를 반복 제기했다.

**원인**: 각 review finding을 닫는 과정에서 직전 corrective prompt를 새 권위처럼 사용해 최초 frozen
설계보다 강한 합격선을 누적했다. 그 결과 false PASS·데이터 손상·복구 불가 여부보다 진단 문구 형식이
커밋 게이트를 선점했다.

**해결/원칙**: corrective review는 항상 최초 frozen acceptance contract와 사용자 가치에 다시 결속한다.
새 finding이 데이터 무결성·권한·false PASS를 바꾸지 않고 기존 closed error와 파생 가능한 경로의 진단
편의만 확장한다면 `REJECT_OVERENGINEERING` 또는 비차단 후속으로 disposition한다. raw Mechanical verdict는
그대로 보존하되 PASS로 재라벨하지 않고, 별도 독립 adjudicator가 근거와 범위를 기록한다. review budget은
더 많은 문구를 얻기 위한 목표가 아니라 중요한 위험을 제한 시간 안에 찾기 위한 경계다. `[Review][Timebox][R5]`
