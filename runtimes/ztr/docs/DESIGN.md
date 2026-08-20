# ZRT = Mechanical 역할. 검사만 하고 판단·편집 안 함.

> **Version**: 3.0
> **Date**: 2026-06-13
> **Status**: v2 Phase 1 기준 설계 문서
> **Primary SoT**: `docs/ROADMAP_V2.md`, `docs/ZRT_V2_DIAGNOSIS.md`, `docs/discovery/ztr-v2/`

---

## 0. 정체성

Zero-Token Roundtable v2는 멀티 에이전트 회의를 주재하지 않는다.
ZRT v2는 MM(MultiAgent Methodology)의 **Mechanical 역할 전담 런타임**이다.

v1의 Writer -> Critic -> Consensus 라운드 루프는 코드가 판단을 해석하려 했기 때문에
하네스와 파서가 계속 비대해졌다. v2는 판단을 LLM 세션과 사람에게 반납하고,
코드는 반복 가능한 기계 검사와 사실 캡처만 수행한다.

---

## 1. Boundaries

| 경계 | 코드가 한다 | 코드가 안 한다 |
|---|---|---|
| 출력 해석 | verdict enum(`PASS`, `CHANGES_REQUESTED`, `BLOCKED`)과 exit code만 확인 | 리뷰 내용의 의미 해석, 설계 판단, 수락/거부 결정 |
| 진행 | 고정 순서 실행, 게이트 검사, 알림, 타임박스 강제 | 다음 페이즈 결정, 리뷰어 의견 종합, 머지 승인 |
| 파일 | envelope/result 캡처, 인바리언트 사실 확인, 검증 로그 보존 | 소스 코드 편집, 자동 머지, Writer 역할 수행 |

이 경계를 어기면 v1의 `ConsensusEngine` 함정으로 되돌아간다. 특히 LLM 자연어를 코드가
의미 파싱해서 합격/불합격을 만들면 v2 설계 위반이다.

---

## 2. v2 구조

```text
ztr CLI
├─ review      (Phase 2) ruff + mypy + Nitpicker/Ollama 프리필터
├─ gate        (Phase 3) OutputQualityGate 기반 무가치 출력 탐지
├─ verify      (Phase 3) PostMergeVerifier 기반 사후 검증
├─ invariants  (Phase 4) MM 인바리언트 사실 검사
└─ run-phase   (Phase 5) 헤드리스 세션 릴레이, spike S1 이후

공유 계층
├─ envelope.py                  JSON stdout 계약
├─ config/schema.py             역할명 기반 backend/model/call_type 바인딩
├─ agents/base.py, registry.py  IAgent + __init_subclass__ 자동 등록
├─ agents/nitpicker.py          무료 프리필터
├─ agents/ollama.py             로컬 sLLM 보조
├─ engine/circuit_breaker.py    백엔드 장애 폴백
├─ engine/quality_gate.py       H4 보존
├─ engine/post_merge_verifier.py H6 보존
├─ engine/hooks.py              ToastHook/HookManager 보존
├─ engine/session_store.py      판정 이력·정확도 추적
└─ web/                         대시보드 동결 유지
```

삭제된 v1 판단 루프 자산:

| 삭제 자산 | 삭제 이유 |
|---|---|
| `engine/orchestrator.py` | 라운드 진행과 판단 지휘를 세션에 반납 |
| `engine/consensus.py` | v1 자연어 합의 파서 폐기, v2 verdict는 `envelope.py`에 신규 정의 |
| `engine/ast_merger.py` | 코드 편집·머지는 Implementer 세션 책임 |
| `engine/round_diff.py` | 라운드 개념 제거 |
| `engine/prompt_adapter.py` | Writer/Critic 프롬프트 조립 제거 |
| `engine/context_extractor.py` | Writer 컨텍스트 주입 제거 |
| `engine/summarizer.py` | Writer/orchestrator 전용 요약 제거 |
| `agents/claude_code.py`, `agents/gemini.py` | Writer/외부 워커 제거, 후속 바인딩 뒤로 이동 |

---

## 3. Envelope 계약

모든 후속 `ztr` 명령의 stdout은 `src/envelope.py`의 `Envelope` 모델을 따른다.
이번 Phase 1에서는 모델과 단위 테스트만 제공하며 runner 배선은 Phase 2 이후 범위다.

```json
{
  "status": "PASS",
  "exit_code": 0,
  "backend": "nitpicker",
  "model": "local",
  "duration_s": 0.12,
  "stdout": "...",
  "stderr_sanitized": "...",
  "fallback_used": false,
  "not_claimed": []
}
```

Exit code 규약:

| 상태 | 코드 |
|---|---:|
| `PASS` | 0 |
| `CHANGES_REQUESTED` | 1 |
| `BLOCKED` | 2 |
| timeout | 124 |
| internal error | 70 |

stderr의 32자 이상 토큰은 `[REDACTED]`로 치환한다.

---

## 4. 역할 바인딩

`src/config/agents.config.yaml`은 모델명을 키로 쓰지 않고 MM 역할명을 키로 쓴다.
모델은 값이며, backend와 call_type은 교체 가능한 실행 세부사항이다.

```yaml
roles:
  implementer-reviewer:
    backend: "claude_cli"
    model: "sonnet"
    call_type: "headless"
    l2_model: "opus"

  mechanical:
    backend: "ollama"
    model: "qwen2.5-coder:7b"
    call_type: "local"
```

`agents:` 목록은 현재 Phase 1에서 KEEP 에이전트만 남긴다.

| Agent ID | type | v2 역할 |
|---|---|---|
| `nitpicker-prefilter` | `nitpicker` | `ztr review` 프리필터 핵심 |
| `ollama-local` | `ollama` | Mechanical 로컬 모델 보조 |

---

## 5. 보존 자산

| 자산 | 위치 | v2 용도 |
|---|---|---|
| NitpickerAgent | `src/agents/nitpicker.py` | Phase 2 `ztr review` |
| OllamaAgent | `src/agents/ollama.py` | 무료 로컬 검토 보조 |
| OutputQualityGate | `src/engine/quality_gate.py` | Phase 3 `ztr gate` |
| PostMergeVerifier | `src/engine/post_merge_verifier.py` | Phase 3 `ztr verify --post-merge` |
| CircuitBreaker | `src/engine/circuit_breaker.py` | 백엔드 장애 폴백 |
| HookManager/ToastHook | `src/engine/hooks.py` | Phase 5 알림 재사용 |
| SessionStore | `src/engine/session_store.py` | 판정 이력, 피드백, 대시보드 |
| config loader/schema | `src/config/` | 역할 바인딩 config-as-code |

KEEP 자산의 공개 시그니처는 후속 페이즈 전까지 유지한다.

---

## 6. Discovery 포인터

상세 근거는 discovery 산출물을 참조한다.

| 문서 | 역할 |
|---|---|
| `docs/discovery/ztr-v2/design.md` | Boundaries, D1~D7 결정 |
| `docs/discovery/ztr-v2/requirements.md` | 목표/비목표 |
| `docs/discovery/ztr-v2/validation_plan.md` | PASS vs NOT CLAIMED 기준 |
| `docs/discovery/ztr-v2/risk_register.md` | 주요 리스크 |
| `docs/discovery/ztr-v2/handoff.md` | 세션 간 인계 |
| `docs/ZRT_V2_DIAGNOSIS.md` | KEEP/DROP/전환, MM/MS 차용 |
| `docs/ROADMAP_V2.md` | 페이즈 경계와 불변 원칙 |

---

## 7. 교훈 유지

v1 절은 삭제하지만 다음 교훈은 v2에서도 그대로 유효하다.

| ID | v2 적용 |
|---|---|
| LESSON-001 | Windows subprocess는 resolved 경로 + communicate + timeout + kill 원칙 |
| LESSON-002 | `asyncio.Lock`은 lazy init |
| LESSON-003 | `__init_subclass__` 자동 등록은 deferred buffer로 순환 import 방지 |
| LESSON-005 | 외부 프로젝트 import는 최소 모듈만 |
| LESSON-006 | Pydantic `extra="forbid"` 사용 시 YAML과 스키마를 함께 갱신 |
| LESSON-008 | Windows 콘솔 출력은 UTF-8 래핑 또는 ASCII 대체문자 사용 |
| LESSON-013 | 하네스는 보존하되, 의미 판단을 코드가 가져오지 않도록 경계 유지 |

새 교훈은 `docs/LESSONS_LEARNED.md` 맨 아래에 `LESSON-NNN` 형식으로 append한다.
