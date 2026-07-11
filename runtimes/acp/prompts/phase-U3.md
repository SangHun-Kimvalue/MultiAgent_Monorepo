작업: Agent Control Plane — Phase U3 (보드 노이즈 정리: 오래된 세션 보존 컷오프 + 프로젝트 경로 단축 표시). 한국어로 응답/주석.

[형상관리] 단일 브랜치 `acp-v1` 유지. 새 브랜치 금지. **태그 미생성**(스킬 §9 — 경계는 커밋/HANDOFF로 기록). 시작 전 `git status`.

[전제] v1 백엔드(P0~P3) + UI(U1·U2) + 검증정비(V1) 위에 시작. 실데이터로 띄웠더니 **오래된 STALE·활동시각 없는 UNKNOWN 세션이 보드를 가득 채워 노이즈**가 됨. 이 페이즈는 그 노이즈를 구조적으로 제거 + 긴 프로젝트 경로 가독성 개선. 백엔드 공개 시그니처 변경 금지(추가만). **V1 테스트 게이트 규약 준수**: 신규 e2e는 `@pytest.mark.e2e`.

[SoT — 반드시 읽을 것]
- `AgentControlPlane/PHASE.md` §0 결정 로그(신규 "세션 보존 컷오프"·"프로젝트 경로 표시" 행) + §4 불변 원칙
- 부착 코드: `acp/poller.py`(per-record 루프), `acp/store.py`(upsert/init), `acp/config.py`+`config/paths.yaml`, `acp/web/static/dashboard.js`, `acp/web/templates/dashboard.html`

[전략 리마인더] minimal-first. 컷오프는 **표시 필터가 아니라 수집 단계 제외 + DB prune**(상훈 확정 2026-06-12). 새 UI 기능 추가 아님 — 노이즈 정리 + 경로 표시만. LLM 0회.

[이번 범위]
할 것:
1. **세션 보존 컷오프 (수집 단계 제외 + DB prune)**:
   - 신규 config: `config/paths.yaml`에 `retention: { max_age_days: 7 }` + `acp/config.py` `AppConfig.max_session_age`(초, 기본 604800=7일; `retention.max_age_days * 86400` 로드). 미설정 시 7일.
   - **수집 단계 제외 (`acp/poller.py` `_tick`의 `for record in records:` 진입부, 현재 :67)**: 레코드가 ① `last_activity is None`(활동시각 없음 → UNKNOWN 주원인) **또는** ② `(now - record.last_activity) > max_session_age` 이면 **upsert·이벤트·알림 전부 skip**(continue). 사유 debug 로깅(C3 — silent 아님, 단 과다로깅 금지 → debug 레벨).
   - **DB prune (`acp/store.py` 신규 `prune_old_sessions(cutoff_iso: str) -> int`)**: `DELETE FROM sessions WHERE last_activity IS NULL OR last_activity < ? OR updated_at < ?` (활동시각 없음/오래됨, 또는 더 이상 수집 안 되는 orphan을 updated_at로 정리). 삭제 건수 반환·로깅. **`_tick`마다 1회**(또는 init+주기) 호출 — poller에서 `now - max_session_age` 컷오프로.
   - **감사로그 보존**: `./.acp/events.jsonl`·events 테이블은 prune 대상 아님(이력 유지). events에 sessions FK 있으면 `ON DELETE` 동작 확인 — sessions만 지우고 events는 남기도록(FK RESTRICT면 prune 실패하므로 FK/인덱스 점검).
2. **프로젝트 경로 단축 표시 (`acp/web/static/dashboard.js` + `dashboard.html`)**:
   - `dashboard.js`에 `shortProject(path)` 헬퍼: `/`·`\` 모두 분리 → **마지막 2개 세그먼트**만(`…\Todo\AgentControlPlane`). 빈 값/`no-project`/원격 uri(`scheme://...`)는 적절히(원격은 host 정도, 또는 원문 축약). 1세그먼트면 그대로.
   - 적용: 테이블 프로젝트 셀(`dashboard.html:161` `x-text="s.project_path || '-'"`) + 그룹 헤더(`:144` `x-text="group.project_path"`). **full path는 `:title` 툴팁**으로 hover 시 노출(정보 손실 0).
   - 필터 메뉴의 프로젝트 라벨도 동일 단축(선택) — 값(필터 키)은 full path 유지, 표시만 단축.

★ out-of-scope ★: 사용자별 컷오프 토글 UI(설정파일로 충분), 세션 상세 드릴다운, 서버측 페이지네이션, "보관함"/소프트삭제(하드 prune으로 충분 — 감사로그가 이력 보존).

[부착점/대상] (시그니처로 재확인 후 부착)
| 파일 | 작업 |
|---|---|
| `config/paths.yaml` | `retention: { max_age_days: 7 }` 신설 |
| `acp/config.py` | `AppConfig.max_session_age`(초) + `load()`에서 `retention.max_age_days` 파싱 |
| `acp/poller.py` | `_tick` per-record 루프 진입부 컷오프 skip(:67) + 매 tick `prune_old_sessions` 호출 |
| `acp/store.py` | `prune_old_sessions(cutoff_iso)` 신규(sessions만 삭제, events 보존, FK 점검) |
| `acp/web/static/dashboard.js` | `shortProject(path)` 헬퍼 |
| `acp/web/templates/dashboard.html` | 프로젝트 셀·그룹헤더에 shortProject + full path title 툴팁 |
| `tests/` + `tests/e2e/` | 컷오프(단위: None/7일초과 skip + prune 삭제건수) + shortProject(e2e 또는 단위) |

[아키텍처 결정]
- **컷오프 기준 = last_activity 우선**(실제 세션 활동), orphan 정리는 updated_at 보조. 수집 skip은 last_activity로, prune은 둘 다로.
- **활동시각 없음(None) = 제외**: 보드는 "최근 행동 가능한" 세션만 — C3의 UNKNOWN 표시 원칙을 보드 한정으로 의도적 우회(사유 로깅 유지, 감사로그 보존). 결정 로그에 명시.
- 경로 단축은 **표시 전용** — 필터/그룹 키·정렬은 full path 유지(데이터 무손실, 툴팁으로 전체 노출).

[불변 원칙]
- silent fallback 금지: skip/prune은 사유 로깅(과다 방지 debug). 감사로그(events)는 삭제하지 않음.
- 앱 아티팩트·PHASE.md read-only. LLM 0회.
- 결정적: 컷오프는 순수 시간계산(now 주입 가능, 테스트 mock).
- Timebox: 컷오프/prune이 3회 안 풀리면 멈추고 보고.

[검증·DoD]
1) 단위: 컷오프 skip(last_activity None·7일초과 → upsert 안 됨, 7일 이내 → 됨, now 주입) + `prune_old_sessions` 삭제건수·events 보존. `pytest -q` (단위, `-m "not e2e"`) 통과·수치.
2) e2e(`@pytest.mark.e2e`): `--fake`에 오래된/None 픽스처 추가 → 보드에 안 뜸 + 경로가 마지막 2세그먼트로 표시 + title에 full path. `pytest -m e2e tests/e2e -q` 통과.
3) 수동 스모크: `run.bat`(실데이터)로 **확인필요/UNKNOWN 노이즈가 줄고 경로가 짧게** 보이는지 육안 + 캡처. (무거운 실행 — 사용자 확인 후)
4) 리뷰(둘 다 **Implementer 세션** 집행, §8): 별도 Reviewer 서브에이전트 + Nitpicker(local LLM/ollama). 미준비/락이면 `blocked` 정직 보고, PASS/waive 주장 금지.
5) Sync-Out: PHASE.md(U3 done·결정로그 2행) + HANDOFF + `docs/lessons/u3-retention-cutoff.md`(WHY: 보드 노이즈 vs C3 우회 트레이드오프). 사용자 "커밋해" 후 커밋(태그 없음).

[완료 보고] 변경 파일 / 컷오프·prune 구현 위치 / 경로 단축 위치 / 검증(명령+출력+artifact) / before·after 세션 수(노이즈 감소량) / PASS 어디까지·NOT CLAIMED·가정.
