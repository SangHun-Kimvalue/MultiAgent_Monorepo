# Discovery Handoff

## Gate Decision

- Decision: **DISCOVERY_PASS**
- Reason: `TBD=0`, PASS 레벨 명시, SSOT/소유권/adapter 경계 확정, 비목표 명시, senior critique 응답 완료,
  핵심 가설(앱 로컬 아티팩트로 binding 가능)이 **실제 PC 증거로 검증됨**. 잔여 항목은 모두 분류되었고
  P0~P1 PoC + 가드 설계로 위험이 통제 가능.

## Profile

- **AI/LLM** (LLM 도구 세션 관측) + **Automation/Script** (read-only 아티팩트 폴링) + **Product/MVP** (관제 대시보드).

## Outputs

| Document | Path | Notes |
|---|---|---|
| Role assignment | `discovery/role_assignment.md` | Discovery≠구현, 역할 분리 |
| Requirements | `discovery/requirements.md` | + ZTR 홀딩 트리거 + Codex + 홀딩 탐지 |
| Design | `discovery/design.md` | + Codex collector(최고소스) + HOLDING 상태전이 |
| Validation plan | `discovery/validation_plan.md` | PASS 레벨 + 검증 매트릭스 |
| **PHASE.md format** | `discovery/phase_md_format.md` | cubi-skills 캐논 정렬 하이브리드 포맷 |
| Open items | `discovery/open_items.md` | TBD=0, OI-001~012 |
| Risk register | `discovery/risk_register.md` | R-001~007 (R-003 홀딩 P1 승격) |

## Planner Input

Planner 세션이 phased 로드맵으로 풀어야 할 핵심 단위(우선순위 갱신):

- **P0 코어 스파이크:** ZTR 복제 → FastAPI+SQLite, 최소 대시보드 + `PhaseFileCollector`(PHASE.md frontmatter 파서).
- **P1 Codex 수집 + 홀딩 탐지(킬러기능):** `CodexCollector`(`sessions/*.jsonl` + `process_manager/chat_processes.json`)
  → cwd/model/command/osPid/updatedAtMs 정규화. LivenessSvc로 **HOLDING vs STALE 분리**. ZTR형 입력대기 탐지 PoC.
- **P2 멀티앱 확장:** `ClaudeSessionCollector` + `CursorWorkspaceCollector`(workspace.json + state.vscdb mtime, DB 미오픈). 세션×PHASE.md 조인.
- **P3 알림:** Nitpicker notifier 재사용 → 좀비/에러/완료/홀딩 토스트·웹훅.
- (v2 후보) 온디맨드 AI 브리핑, 양방향 통제 — v1 비목표.

## Open Items Summary

- Closed: OI-001/002/003/004/008/009/010/012 (수집방식·페이즈SSOT·PHASE.md포맷·범위·Codex·알림·PHASE운용 확정).
- Open(PoC로 닫음): OI-005(Cursor 활동시각), OI-006(스키마 가드), OI-007(다중세션 식별), OI-011(홀딩 임계).

## PASS / NOT CLAIMED

- PASS(v1): 멀티 앱(Codex/Claude/Cursor) 세션 read-only 종합 + 프로젝트/모델/실행명령/페이즈 표시 +
  **홀딩**/좀비/에러/완료 알림. Live smoke.
- NOT CLAIMED(v1): 세션 통제, 대화 내용(Lv3), 클라우드/분산, 알림 SLA, 페이즈 자동생성, ChatGPT 채팅앱.

## Next Recommended Step

1. ✅ **`PHASE.md` 운용 합의 완료(OI-012)** — 옵션A + 갱신 이중방어 확정, 스킬 Exit Checklist 수정 반영.
2. **Planner 세션 개시** — 본 산출물을 입력으로 phased 로드맵 + P0/P1 구현 프롬프트 작성
   (`phased-implementation-handoff` 스킬). **P1(Codex+홀딩탐지)을 첫 가치 증명 페이즈로** 권장.
3. 구현은 **ZTR 복제로 출발**(FastAPI+SQLite+SSE 즉시 확보).
