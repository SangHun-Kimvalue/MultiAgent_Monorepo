# PHASE.md 포맷 제안 (cubi-skills 캐논 정렬)

> 목적: 관제소(Agent Control Plane)가 각 프로젝트의 **현재 페이즈/상태를 토큰 0원으로 결정적 파싱**하기
> 위한 표준. cubi-skills 캐논과 **충돌하지 않고 보강**한다.

## 0. 캐논과의 관계 (중복 정의 금지)

`cubi-skills/DOC_TAXONOMY.md`는 이미 다음을 정의한다:
- **로드맵(`*_ROADMAP.md` 또는 PHASES)** = "페이즈 개요·상태 + 결정 로그 + 부착점 + DoD" → **진행/결정 SSoT**.

따라서 `PHASE.md`는 **새 SSoT가 아니다.** 운용 방식은 아래로 **확정(상훈 결정 2026-06-09)**:

- ✅ **옵션 A (확정·기본):** `PHASE.md`가 곧 그 프로젝트의 경량 로드맵. 캐논 로드맵 항목(결정로그/페이즈
  개요/DoD)을 담는다. 신규·중소 프로젝트는 별도 `*_ROADMAP.md`를 만들지 않고 `PHASE.md` 하나로 통합.
- (참고) 옵션 B(기존 거대 로드맵의 요약 포인터)는 AutoUtube처럼 이미 방대한 로드맵이 있는 프로젝트에서만
  예외적으로. 그 경우 본문은 "출처: `XXX_ROADMAP.md`" 포인터, frontmatter만 현재 상태(중복=drift 회피, 캐논 §4).

> 핵심 설계: **사람이 읽는 본문(자유 마크다운) + 기계가 읽는 frontmatter(고정 스키마)** 의 하이브리드.
> 관제소는 **frontmatter만** 파싱한다. 본문 자유도는 보존된다.

## 1. 표준 포맷

```markdown
---
# ── 관제소가 파싱하는 고정 블록 (acp:phase v1) ──
acp_schema: "phase/1.0"          # 스키마 버전 (드리프트 가드, 캐논 C6)
project_id: "AgentControlPlane"  # 프로젝트 식별자(폴더명과 일치 권장)
roadmap_ref: "self"             # 'self'=이 파일이 로드맵 / 또는 "docs/XXX_ROADMAP.md"
current_phase: "P1"             # 지금 진행 중인 페이즈 id
phase_status: "in_progress"     # planned | in_progress | blocked | review | done
pass_level: "deterministic"     # 이 페이즈 PASS 기준: unit|deterministic|live|e2e
updated_at: "2026-06-09T12:30:00+09:00"  # 사람이 갱신한 시각(노후 경고용)
phases:                          # 전체 페이즈 개요(상태 머신)
  - id: "P0"; title: "코어 스파이크"; status: "done"
  - id: "P1"; title: "Codex 수집 + 홀딩 탐지"; status: "in_progress"
  - id: "P2"; title: "페이즈 조인"; status: "planned"
  - id: "P3"; title: "알림"; status: "planned"
owner_session: "implementer"    # (선택) 현재 이 페이즈를 맡은 역할(MULTI_AGENT.md)
blocking: ""                    # (선택) blocked일 때 사유 한 줄
---

# <기능> 전체 페이즈 로드맵   ← 여기부터는 캐논 로드맵 본문(자유 형식)

작성일: 2026-06-09
문서 성격: 다중 세션 작업의 진행 기준(SoT 보조).

## 0. 확정 결정 로그 (변경 시 위로 갱신)   ← 캐논 필수
| 항목 | 확정값 | 근거 |
|---|---|---|
| ... | ... | ... |

## 1. 페이즈 개요 / 상태
| Phase | 내용 | 상태 |
|---|---|---|
| P0 | 코어 스파이크 | ✅ done |
| P1 | Codex 수집 + 홀딩 탐지 | 🔄 진행 |

## 2. 완료 산출물 / 다음 페이즈 상세 / DoD ...
```

## 2. 필드 사양 (frontmatter)

| 필드 | 필수 | 타입 | 관제소 사용처 |
|---|---|---|---|
| `acp_schema` | ✅ | str | 파서 버전 분기. 미지원 버전 → `UNKNOWN` 격리(silent 금지) |
| `project_id` | ✅ | str | 세션 cwd ↔ 프로젝트 조인 키 |
| `roadmap_ref` | ✅ | str | `self` 또는 실제 로드맵 경로(포인터) |
| `current_phase` | ✅ | str | 상황판 "현재 페이즈" 표시 |
| `phase_status` | ✅ | enum | 페이즈 진행 배지 + blocked 경고 |
| `pass_level` | ⬜ | enum | 검증 기대 레벨 표시 |
| `updated_at` | ✅ | ISO8601 | **노후 경고**: 세션 활동은 있는데 PHASE.md가 오래되면 "plan stale" 경고 |
| `phases[]` | ✅ | list | 페이즈 상태 머신 시각화(트리/바) |
| `owner_session` | ⬜ | enum | 역할 매핑(MULTI_AGENT.md): planner/implementer/reviewer... |
| `blocking` | ⬜ | str | blocked 사유 |

## 3. 조인 규칙 (세션 ↔ PHASE.md)

1. 세션 레코드의 `cwd`(Codex/Claude) 또는 워크스페이스 폴더(Cursor)를 얻는다.
2. 그 경로(또는 상위 경로)에서 `PHASE.md`를 찾는다 → frontmatter 파싱.
3. `project_id` + `current_phase`로 상황판 행을 구성: **[앱][세션][프로젝트][모델][현재페이즈][상태][생존]**.
4. PHASE.md 없으면 → 프로젝트는 표시하되 페이즈 칼럼은 `no-phase-file`(미설정 명시, 추정 금지).

## 4. 캐논 정합성 체크

- ✅ DOC_TAXONOMY: 로드맵=진행/결정 SSoT를 **대체하지 않고** 기계 판독 레이어만 추가.
- ✅ C3 Fail-fast: 미지원 스키마/파싱 실패는 `UNKNOWN` 명시(옛 상태 표시 금지).
- ✅ C6 재현성: `acp_schema` 버전 고정.
- ✅ §4 SSoT 충돌: 옵션 B에서 본문은 로드맵 **포인터**(중복=drift 회피).
- ✅ C5 YAGNI: frontmatter는 관제 표시에 실제 필요한 필드만. 그 외는 본문 자유.

## 5. 갱신 의무 (실측 기반 — 이중 방어)

**실측 결과(2026-06-09, `phased-implementation-handoff/references/doc-management.md`):**
스킬 Sync-Out 의무는 `HANDOFF 갱신 + lessons append + (설계 바뀌면) 로드맵/ADR 갱신`이다.
→ 즉 **HANDOFF/lessons는 매번 의무지만, 로드맵(=PHASE.md) 갱신은 "설계 바뀌면" 조건부**다.
페이즈만 진행되고 설계가 그대로면 frontmatter 갱신이 누락될 수 있다(관제소가 의존하는 값).

**이 구멍을 두 겹으로 닫는다(상훈 결정):**

1. **관제소 방어(런타임):** `updated_at` **노후 경고**. 세션 활동은 갱신되는데 PHASE.md `updated_at`이
   오래되면 상황판에 `plan-stale` 경고를 띄운다. → 갱신을 깜빡해도 **silent stale 금지**(캐논 C3).
2. **스킬 방어(프로세스):** `phased-implementation-handoff`의 **Phase Exit Checklist에
   "PHASE.md frontmatter(current_phase/phase_status/updated_at) 갱신" 한 줄을 추가**한다.
   → 페이즈를 닫을 때 갱신이 체크리스트 의무가 된다. (실제 스킬 파일 수정 완료)

## 6. 확정 사항 (해결된 열린 질문)

- Q1 (옵션 A/B 기본값): **옵션 A 확정** — PHASE.md가 곧 경량 로드맵. 신규 기본.
- Q2 (phases[] 상태 enum): 캐논 배지와 1:1 (`planned/in_progress/blocked/review/done` ↔ ⏳/🔄/⏸/👀/✅).
- Q3 (갱신 주체): **이중 방어** — 관제소 노후경고 + 스킬 Exit Checklist 의무화.
