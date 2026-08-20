# Risk Register

| ID | Severity | Risk | Evidence / trigger | Mitigation | Owner | Status |
|---|---|---|---|---|---|---|
| R-001 | P1 | **아티팩트 스키마 드리프트** — 앱 업데이트로 로컬 파일 포맷 변경 시 수집 silent break | Claude `2.1.165` 등 잦은 버전업 확인됨 | `schema_version`/필드 존재 가드, 파싱 실패 시 `UNKNOWN` 명시(옛 데이터 현재처럼 표시 금지), 회귀 픽스처 보관 | Implementer | Open |
| R-002 | P1 | **state.vscdb 잠금/손상** — 앱 실행 중 SQLite 잠금으로 읽기 실패 또는 오손 | Cursor/VSCode 실행 중 DB 점유 | **P2 회피(2026-06-10 실측):** DB 내부에 깔끔한 활동시각 필드 부재(ItemTable만, history.entries뿐) → DB 미오픈, **state.vscdb 파일 mtime을 last_activity로 채택**. temp-copy read-only(immutable)는 실익 없어 보류(YAGNI). 향후 DB 필드 필요 시 재개. | Implementer | **Mitigated(회피)** |
| R-003 | P1 | **홀딩/좀비 오탐·미탐** — 활동시각만으로 판정 시 입력대기(holding)를 죽음으로 오인하거나, 멈춤을 놓침 | ZTR가 빠진 바로 그 실패 모드 | 3신호 조합(last_activity+osPid+완료표식)으로 HOLDING/STALE 분리, 임계 분리, 결정 테스트 | Implementer | Open |
| R-004 | P2 | **PHASE.md 규율 붕괴** — 사용자가 갱신 안 하면 페이즈가 거짓 표시 | 수기 유지보수 의존 | PHASE.md mtime 노후 시 "stale plan" 경고 표시, 최소 포맷+예시 제공 | 상훈/Planner | Open |
| R-005 | P2 | **바인딩 모호성** — 동일 폴더에 여러 세션/창 | 멀티 세션 동시 진행 | session_id 기준 분리, 동일 cwd 그룹핑 표시 | Implementer | Open |
| R-006 | P3 | **프라이버시** — 세션 파일에 대화/민감정보 포함 가능 | 로컬 JSON에 프롬프트 흔적 | 메타데이터 필드만 추출(본문 미파싱), 로컬 전용·외부 전송 금지 | Implementer | Open |
| R-007 | P3 | **단일 PC SPOF / 수집기 다운** | 코어·수집기 프로세스 종료 | 자동 재시작(Run_*.bat/스케줄러), append-only 로그로 복구 | Implementer | Open |
