# Open Items

| ID | Type | Item | Basis / impact | Close condition | Status |
|---|---|---|---|---|---|
| OI-001 | Decision | 상용 앱 수집 = 로컬 아티팩트 주기 폴링 | 코드 주입 불가가 근거. ZTR 홀딩 보완 처방 | 확정됨 | Closed |
| OI-002 | Decision | 페이즈 SSOT = 프로젝트 `PHASE.md` (cubi-skills 캐논 정렬) | 사용자 채택 + DOC_TAXONOMY 로드맵=SSoT | `phase_md_format.md` 작성 완료 | Closed |
| OI-003 | Decision | v1 범위 = 읽기 + 알림(통제 제외) | 사용자 선택. 상용앱 통제 사실상 불가 | 확정됨 | Closed |
| OI-004 | Decision | `PHASE.md` 포맷 = 하이브리드(frontmatter+본문), 캐논 정렬 | 결정적 파싱 + 캐논 비중복 | `phase_md_format.md` §1~4 | Closed |
| OI-005 | Evidence Required | Codex/Claude/Cursor 활동시각·생존 신뢰 소스 | **Codex 실측 완료(2026-06-09)**: osPid=툴콜 서브프로세스(RUNNING 확인용), last_activity=jsonl 마지막 이벤트 ts | Codex=Closed. **잔여: Cursor state.vscdb 활동시각 PoC(P2)** | Partially Closed |
| OI-006 | Assumption | 앱 로컬 파일 스키마 단기 안정 | 앱 업데이트 시 silent break. Codex jsonl/Claude json/Cursor vscdb | 스키마 버전 가드+파싱 실패 시 UNKNOWN | Open |
| OI-007 | Assumption | 동일 cwd 다중 세션 식별 가능 | Codex conversationId / Claude sessionId로 구분 | 유일성 PoC 확인 | Open |
| OI-008 | Decision | 알림 채널 = Nitpicker notifier(토스트+웹훅) 재사용 | 기존 자산 | 확정됨 | Closed |
| OI-009 | Decision | GPT 계열 = **Codex Desktop**으로 커버(ChatGPT 채팅앱 미설치) | 정찰: `~/.codex/` 존재·실행중, ChatGPT 부재 | 확정됨 | Closed |
| OI-010 | Decision | **Codex Collector 1순위 구현** | 가장 풍부(jsonl+osPid). 홀딩 PoC 최단 | 확정됨 | Closed |
| OI-011 | Evidence Required | 홀딩(HOLDING) 판정 임계·신호 조합 검증 | ZTR형 입력대기 정확 탐지 | **Closed(2026-06-09)**: last_evt(jsonl)+age 결정테이블 확정. 임계 idle=5분/hold=15분/stale=60분. osPid는 RUNNING 전용. 잔여 검증=P1 Live smoke(턴중단 승인대기 케이스) | Closed |
| OI-012 | Decision | PHASE.md=옵션A(경량 로드맵), 갱신=이중방어 | 상훈 확정. 노후경고+스킬 Exit Checklist | `phase_md_format.md` §5~6 + 스킬 수정 완료 | Closed |

## Gate Rule

`DISCOVERY_PASS` requires zero `TBD` items. `Assumption`, `Decision`, `Evidence Required` may remain only
when explicitly documented.

**현재 상태:** `TBD = 0`. 잔여 항목은 모두 `Decision`/`Assumption`/`Evidence Required`로 분류 완료.
OI-005(Codex)/OI-011은 **2026-06-09 osPid·jsonl 실측으로 Close** — 신호모델=last_evt+age 결정테이블,
임계 5/15/60분 확정. 잔여 Open은 OI-006(스키마 안정·가드로 완화), OI-007(cwd 다중세션 유일성·P1/P2 PoC),
OI-005 잔여(Cursor state.vscdb 활동시각·P2 PoC). OI-012는 PHASE.md=옵션A + 갱신 이중방어로 종결.
