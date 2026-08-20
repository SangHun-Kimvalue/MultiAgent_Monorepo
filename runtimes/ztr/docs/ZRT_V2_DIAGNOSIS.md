# ZRT v2 진단 — 재포지셔닝 + MM·MS 차용 계획

> **전제**: MultiAgent_Methodology(MM)가 현행 정본 방법론이며 실효성이 확인됨
> (스킬 기반 하네스 규약 + HANDOFF/교훈/설계/로드맵 문서 + 세션별 룰 동작).
> multi-agent-starter(MS)는 외부 참조 프로젝트.
> 본 문서는 ZRT v1의 한계를 진단하고, v2의 역할 재정의 + 차용 항목을 확정한다.
>
> 연관 문서: `D:\MultiAgent_Methodology\docs\STARTER_ADOPTION_PLAN.md` (MM 차용 계획 — 특히 A3)
>
> 작성일: 2026-06-13 · 상태: **Discovery 완료 — DISCOVERY_PASS (조건부: Phase 5는 spike S1 후 진입)**
> Discovery 산출물: `docs/discovery/ztr-v2/` (brief·requirements·design·validation_plan·open_items·risk_register·handoff·role_assignment)
> 다음 단계: Planner 세션이 `phased-implementation-handoff`로 로드맵 확정 (페이즈 제안은 discovery handoff.md 참조)

---

## 1. v1 한계의 근본 원인 — 기능이 아니라 "역할"

ZRT v1의 문제는 코드 품질이 아니다 (227 테스트, H1~H6, 서킷브레이커 — 다 잘 만들었다).
문제는 **판단 루프(Writer→Critic→Consensus 라운드)를 코드가 지휘**한 것이다:

```
코드가 LLM을 지휘 → LLM 자연어 출력을 코드가 해석해야 함
  → 출력 태그 강제(H1) → 빈함수 탐지(H4) → 3층 합의 파서 → 사후검증(H6) ...
  → 하네스 군비경쟁 (모델/프롬프트가 바뀔 때마다 파서·하네스 보수)
  → 오케스트레이터 자체가 유지보수 대상 제품이 됨 (FastAPI+SQLite+227테스트)
```

같은 문제를 MM은 반대로 풀었다: **판단은 LLM 세션에게**(역할 규약 + 스킬), 코드는 없음.
MS도 같은 베팅(오케스트레이터=LLM 세션). 그리고 이 방식이 실효성 확인됨.

→ **결론: 판단 루프는 MM 세션에 반납한다. ZRT v2는 회의를 주재하지 않는다 —
회의에 불려가는 검증 기계가 된다.**

## 2. v2의 새 역할 — MM "Mechanical" 역할의 전담 런타임

MM 리뷰 하드게이트는 2-leg이다: ① 설계 리뷰(Reviewer 세션) ② 기계 리뷰(Nitpicker).
현재 MM의 ②는 `run_nit.py` 단일 스크립트 수준이고, MM 스스로 인정하는 갭이
"자동 강제 없음(honor system)"이다. **ZRT의 기존 자산이 정확히 이 자리에 맞는다.**

```
MM 세션 (Discovery/Planner/Implementer/Reviewer = 판단)
   │  호출 (CLI, 동기, 승인 게이트는 MM 규약 따름)
   ▼
ZRT v2 (Mechanical 역할 전담 = 기계)
   ├─ ztr review --changed      # 무료 프리필터: ruff + mypy + nitpicker(ollama)
   ├─ ztr verify --post-merge   # H6: ast.parse + ruff + mypy (머지 후)
   ├─ ztr gate <result-file>    # H4: 빈함수/칭찬-only 등 무가치 출력 탐지
   └─ ztr invariants            # MM 인바리언트 자동 검사 (HANDOFF 갱신됐나,
   │                            #  finding 포맷 준수했나, lessons append 됐나...)
   ▼
JSON envelope 반환 → MM 세션이 읽고 판단·기록
```

부가 효과: **MS의 KI-3(디스패처가 bash-only, Windows 네이티브 미지원)를 ZRT가 해소한다.**
ZRT는 이미 Windows 11 + Python 3.12 네이티브이고 subprocess 3-tier 안전 패턴(LESSON-001)을
보유 — MS 디스패처의 Python 포팅판으로 적임. 내 환경(Windows)에서 즉시 동작.

## 3. ZRT 자산 분류 — 유지 / 폐기 / 전환

### KEEP (v2의 몸체)
| 자산 | v2에서의 역할 |
|---|---|
| Nitpicker 프리필터 (ruff+mypy+ollama, 무료) | `ztr review`의 핵심. **무료 검사를 유료 리뷰 앞에** — zero-token 철학 유지 |
| H4 QualityGate (빈함수/칭찬-only 탐지) | `ztr gate` — LLM 산출물 무가치 탐지 |
| H6 PostMergeVerifier (ast+ruff+mypy) | `ztr verify --post-merge` |
| CircuitBreaker + 폴백 체인 | ollama 다운 등 백엔드 장애 대응 |
| `agents.config.yaml` + Pydantic 스키마 | config-as-code 유지, §5의 바인딩 형식으로 진화 |
| subprocess 3-tier 안전 (LESSON-001) | Windows 네이티브 실행 기반 |
| SQLite SessionStore + 대시보드 | **선택 유지** — 검증 이력 관측용으로 축소 (세션 리플레이) |
| LESSON-NNN 체계 | 이미 MM 표준으로 승격 예정 (STARTER_ADOPTION_PLAN §4) |

### DROP / 동결 (판단 루프와 함께 은퇴)
| 자산 | 이유 |
|---|---|
| AsyncOrchestrator 라운드 루프 | 판단은 MM 세션으로 반납. v1 최대 유지비 원천 |
| ConsensusEngine 3층 파서 | 읽는 주체가 LLM(MM 세션)이면 파싱 자체가 불필요 |
| ASTMerger 자동 머지 | 코드 편집은 Implementer 세션의 일 (MM 역할 순수성) |
| Writer 에이전트 군 (claude_code/gemini writer) | 코드 생성은 세션의 일. Writer가 없으면 H1(출력 태그)·H5(라운드diff)도 불필요 |
| UI_DESIGN/Stitch 산출물 | 동결 (대시보드 축소 유지 시 기존 Obsidian 테마 재사용만) |

### 전환
| 자산 | 전환 방향 |
|---|---|
| H2(라운드 이스컬레이션)·H3(평가 피드백) | 라운드 개념 소멸 → **판정 이력·정확도 추적**으로 축소 (Critic 판정이 사람 판단과 얼마나 일치했나) |
| GeminiAgent/E2E 테스트 | 외부 모델 호출 경로는 §5 바인딩 레지스트리 뒤로 이동 |

## 4. MM에서 차용 (규약 — ZRT가 "따라야 할" 것)

| # | 차용 항목 | 적용 |
|---|---|---|
| M1 | **역할 계약**: ZRT = Mechanical 역할. 검사만 하고 판단·편집 안 함 | v2 정체성. README/DESIGN.md 첫 줄에 명시 |
| M2 | **finding 포맷**: `severity / finding / evidence_or_repro / impact / recommendation` | 모든 검사 결과 출력 형식. 최종 판정은 `PASS / CHANGES_REQUESTED / BLOCKED` |
| M3 | **PASS vs NOT CLAIMED 분리**: 실행 못 한 검사는 "통과"가 아니라 "미주장"으로 보고 | envelope에 `not_claimed: [...]` 필드 |
| M4 | **C1~C7 중 기계 검사 가능 항목의 자동화** — C3(silent fallback: 폴백 사용 시 envelope에 명시), C6(재현성: 실행 커맨드+버전 기록) | `ztr invariants`의 검사 목록. **STARTER_ADOPTION_PLAN A3(인바리언트 체커)의 구현 홈 = ZRT v2** |
| M5 | **Sync-Out 연동**: 검사 결과 요약 1줄을 HANDOFF.md 증거 라인으로 쓸 수 있는 형식 제공 | `--handoff-line` 출력 옵션 |
| M6 | **프로젝트 config 호환**: `.claude/phased-handoff.config.md`의 빌드/RC/컨벤션 PARAM을 읽음 | config 로더 확장 |

## 5. MS에서 차용 (메커니즘 — ZRT가 "구현할" 것)

| # | 차용 항목 | 적용 |
|---|---|---|
| S1 | **JSON envelope 반환**: `{status, exit_code, backend, model, duration_s, stdout, stderr_sanitized, fallback_used, not_claimed}` | 모든 `ztr` 명령의 stdout. MM 세션이 워커 부르듯 ZRT를 호출하고 상태를 결정론적으로 확인 |
| S2 | **바인딩 레지스트리**: role→backend→call_type 간접화. `agents.config.yaml`을 backends 형식으로 진화 (nitpicker→ollama, critic→gemini 등을 선언으로) | MM A1(역할 바인딩)과 같은 형식 공유 — MM 키(역할명) 사용 |
| S3 | **redaction**: stderr에서 32자+ 토큰 자동 `[REDACTED]` | envelope 생성부 |
| S4 | **타임아웃 러너 패턴** (`_run.py`: TERM→KILL, exit 124) | ZRT subprocess 레이어에 이식 — Python이라 그대로 호환 |
| S5 | **isolated_tmp cwd 정책**: 외부 모델 호출 시 임시 디렉토리 실행 | 워크스페이스 오염 방지 |
| S6 | **progressive disclosure**: 기본 출력은 요약 1줄 + 상세는 파일 경로 | 토큰 경제. MM 세션 컨텍스트 보호 |
| S7 | (선택) **fail-fast 슬롯 패턴**: 미구현 백엔드는 명확히 즉시 실패 (gemini_api.sh 방식) | 폴백 체인의 미구현 항목 처리 |

### 차용하지 않는 것
- MS 승인 게이트(`workers_approved`) — ZRT v2는 read-only 검사 도구라 쓰기 권한 통제 불필요.
  승인은 호출자(MM 세션)의 규약.
- MS 재진입 프로토콜 — ZRT 호출은 무상태 1회성이라 재진입 개념 없음. (MM 세션 쪽 규약)
- bash 디스패처 자체 — 개념만 차용, 구현은 Python.

## 6. 파이프라인 릴레이 (`ztr run-phase`) — v2 범위에 추가

### 배경: 실사용 수동 프로세스
사용자가 실제로 반복 중인 페이즈 파이프라인:
```
(1) Opus 설계 → (2) Codex 설계리뷰 → (1) 반영+구현프롬프트 생성
→ (3) Codex 구현 → (4) 구현 리뷰 → 닛피커 → (2) 수락확인+커밋 → (1) 다음 페이즈
```
이 흐름에서 사람의 실제 고통은 판단이 아니라 **전달(세션 간 복붙)·순서 관리·완료 확인**
— 즉 사람이 메시지 버스 노릇을 하는 것. 판단이 아닌 진행(sequencing)은 기계의 일이다.

### v1 함정을 피하는 자동화 경계
- **자동화한다**: 고정 순서 진행, 헤드리스 세션 구동(`claude -p/--resume`,
  `codex exec`), 산출물 파일 캡처(envelope), 게이트 검사, 알림(기존 hooks Toast 재사용),
  타임박스 강제(MM L1=동일축 3회 → STOP)
- **자동화하지 않는다**: 리뷰 내용의 해석(읽는 주체는 다음 세션의 LLM),
  설계 판단, 수락/거부 결정. 코드가 파싱하는 것은 **verdict enum 1개**
  (`PASS|CHANGES_REQUESTED|BLOCKED`)뿐 — v1 ConsensusEngine(의미 파싱)과의 결정적 차이.
- **휴먼 게이트 3곳 고정**: ① 설계 PASS 확정(구현 진입 전) ② 커밋 ③ 다음 페이즈 진행

### 2루프 분리
| 루프 | 단계 | 성격 | 처리 |
|---|---|---|---|
| 바깥(페이즈 간) | 설계, 리뷰 반영, 프롬프트 생성, 다음 페이즈 결정 | 반복·판단 | **세션 유지** (자동화 안 함) |
| 안쪽(페이즈 내) | 구현 → 구현리뷰 → 닛피커 → 수정 루프 | 트랜잭션(프롬프트 완결 시) | **`ztr run-phase`** (결정론적 릴레이) |

### 현 수동 프로세스의 보완 2건 (자동화 전 수정)
1. **구현측 리뷰가 전부 Codex 계열** — 설계는 Opus→Codex 교차인데, 구현 리뷰는
   (4) Codex 파생 → 닛피커 → (2) Codex로 동일 모델 맹점 상관(PROCESS_CONCLUSION §3.2-⑤).
   → 구현 리뷰 leg를 **Opus로 바인딩** (Implementer=Codex ↔ Reviewer=Opus 교차 복원)
2. **Implementer가 자기 리뷰어를 생성(세션 4)** — 리뷰어의 프레이밍·브리핑을 구현자가
   정하면 C4(자기검토 금지)의 변형 위반. → 리뷰 세션 생성 주체를 **릴레이(또는 Planner)**로
   이동. 구현 세션은 리뷰어의 존재를 모른 채 산출물만 낸다.

### 한정판 라운드테이블 (보류 유지)
UU 하드닝 라운드 패턴의 `ztr roundtable --once`(1회성·비반복)는 C5(YAGNI)에 따라
실수요 발생 시 추가. 릴레이와 별개 항목.

## 7. 진행 방식 — MM 방법론으로 dogfooding (일석이조)

이 업데이트는 MM 기준 **L2**(아키텍처 변경)다. 따라서 MM 자신의 절차를 따른다:

```
Phase 0 Discovery (phase0-discovery-interview 스킬)
  → 7문서 + DISCOVERY_PASS 게이트
  → Planner: 로드맵 + 페이즈 프롬프트 (phased-implementation-handoff)
  → Implementer 구현 → 2-leg 리뷰 → Sync-Out
```

**보너스**: MM HANDOFF.md의 미결 항목 "E1 — Discovery 게이트 실전 1사이클 미검증"을
이 작업으로 검증할 수 있다. ZRT v2가 MM Discovery 게이트의 첫 실전 사이클이 된다.

## 8. 요약 — 세 프로젝트의 최종 자리

```
MM   = 판단 규약 (세션, 역할, 게이트, 문서)          ← 정본, 실효 확인
ZRT  = 기계 런타임 (무료 프리필터, 하네스, 인바리언트) ← v2로 재포지셔닝
MS   = 참조 (envelope/바인딩/타임아웃 메커니즘 기증)   ← 차용 후 역할 종료
```

판단은 LLM이(MM), 기계는 코드가(ZRT), 인터페이스는 envelope로(MS) — 지난 분석에서
도출한 하이브리드 최적점의 구현 계획이 이 문서다.
