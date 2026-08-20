작업: Agent Control Plane — Phase P3 (알림: 좀비/에러/완료/홀딩 → 토스트·웹훅). 한국어로 응답/주석.

[브랜치] 단일 브랜치 `acp-v1` 유지. 새 브랜치 금지. **태그 미생성**(스킬 §9 — 경계는 커밋/HANDOFF로 기록). 시작 전 `git status`.

[전제] P2 완료(tag `acp-phase-2`, commit 6d493bb) 후 시작. 필요한 별도 Reviewer 리스크는 PASS /
Nitpicker는 local LLM(`jemmin_cli.py --provider ollama --no-daemon`) 기준으로 PASS해야 함. 로컬 LLM 서비스/모델 미준비 시
`blocked: local LLM unavailable`로 처리하고 PASS를 주장하지 말 것. Codex/Claude/Cursor 3종 수집 + 세션×PHASE 조인 + 상황판 표출 동작. pytest 93 passed.
**시작 전 P2 산출물을 재구현하지 말 것 — 검증 후 재사용**:

  ★ 전이 감지·이벤트 발행 골격이 P2에 이미 있다 (재작성 금지, Notifier만 이 지점에 끼운다) ★
  - `acp/poller.py` per-record 루프: `prev = store.get_session_for_record(record)` → `prev_state` vs 현재 `state`
    비교 → 변경 시 `store.append_event(...)` + `await self._broadcaster.publish({type:"state_change", ...})`.
    이 블록이 **유일한 전이 감지 지점**. P3 알림은 바로 여기서 분기한다(새 루프/새 비교 만들지 말 것).
  - 이벤트/저장 키는 **`app:session_id`** 네임스페이스(`acp/store.py` `session_key()`). publish payload는
    `session_id`(=app:native), `native_session_id`, `app`, `state`, `project_path` 보유.
  - `acp/web/app.py` `EventBroadcaster`(publish/subscribe) + `/api/live/stream` SSE 이미 가동 — 대시보드 알림 패널은 이 스트림 재사용.
  - 전이 대상 상태 enum은 `acp/models.py`(HOLDING/STALE/ERROR 존재, **DONE은 Codex 세션 미적용**).

[SoT — 반드시 읽을 것]
- `AgentControlPlane/PHASE.md` §0 결정 로그(알림 채널) + §4 불변 원칙
- `AgentControlPlane/discovery/design.md` State Transitions(어떤 전이가 알림 트리거인지)
- 재사용 참조(읽기만): `Nitpicker Daemon/src/jemmin/services/notifier.py:88`
  (`NotificationService(toast_enabled, webhook_url, webhook_format)` — slack/discord/generic + win32 toast)

[전략 리마인더] minimal-first 마무리. 알림은 **상태전이 이벤트**에만 발생. 폴링마다 스팸 금지(dedupe 필수).
LLM 호출 0 유지(알림 문구는 템플릿 문자열).

[이번 범위]
할 것:
1. `acp/notify.py` — `Notifier`: Nitpicker `NotificationService` 패턴 차용(코드 이식 또는 최소 포팅).
   - 채널: Windows 토스트(win32 한정) + 웹훅(httpx.post, format=slack|discord|generic). `config/paths.yaml`에 webhook_url/format.
   - `notify(event: StateTransitionEvent)` — title/body/detail/status를 상태별 템플릿으로 구성.
2. 트리거 배선: poller/liveness 결과에서 **상태 전이 감지**(이전 상태 vs 현재) → 알림 대상 전이만 발사.
   **채널×상태 매트릭스(config 조정):**
   | → 전이 | 토스트 | 웹훅 | 우선순위 |
   |---|---|---|---|
   | → HOLDING | ✅ | ✅ | 높음(세션 멈춰 입력 대기/망각) |
   | → STALE | ✅ | ✅ | 높음(좀비 승급) |
   | → ERROR | ✅ | ✅ | 높음 |
   | (선택) PHASE.md phase_status `→ done` | ⬜ | ✅ | 낮음(프로젝트 페이즈 완료 알림) |
   - LIVE/RUNNING/IDLE 진입은 알림 안 함. **Codex 세션 DONE 전이는 없음**(미적용) → 세션 완료 알림은 PHASE phase_status로만.
   - HOLDING→STALE 승급은 **별도 전이로 1회 더** 발사(우선순위 상승).
3. `acp/dedupe.py` — **전이당 1회 발사**(상훈 확정: 홀딩 알림 1회, 반복 리마인드 없음, 무한대기 허용).
   동일 `(session_id, to_state)`는 쿨다운(`notify_cooldown` 기본 3600s) 내 재발사 금지. 복귀(HOLDING→LIVE 등)
   후 다시 같은 상태로 전이하면 **새 이벤트로 인정**(쿨다운 리셋). last_state/last_notified_at은 store에 기록.
4. 알림 발행 이력 append-only: `./.acp/events.jsonl`(P0 append_event 재사용) + 대시보드에 최근 알림 패널.
5. `config/paths.yaml`: toast_enabled, webhook_url, webhook_format, notify_cooldown 노출. 미설정 시 토스트만(웹훅 skip을 명시 로깅 — silent 아님).

★ out-of-scope ★: 양방향 통제(일시정지/강제종료/프롬프트 주입 — v2), 온디맨드 AI 브리핑(v2),
알림 전달 보장/재시도 큐(SLA 미주장).

[부착점/대상] (시그니처로 재확인 후 부착)
| 파일 | 작업 |
|---|---|
| `acp/notify.py` | 신규. Notifier(토스트+웹훅) |
| `acp/dedupe.py` | 신규. 전이별 쿨다운(`(app:session_id, to_state)` 키) |
| `acp/poller.py` | **기존 전이 감지 블록(prev_state≠state)** 안에서 Notifier 호출 분기 추가. 새 비교 만들지 말 것 |
| `acp/store.py` | last_notified_at/last_state 칼럼 — P2 `_ensure_phase_columns` 패턴(멱등 ALTER) 그대로 따라 추가. 공개 메서드 호환 유지 |
| `acp/config.py` | `NotifyConfig`(toast_enabled/webhook_url/webhook_format/notify_cooldown) — P2 `LivenessConfig` 로더 패턴 복제 |
| `config/paths.yaml` | `notify:` 블록 신설(미설정 시 토스트만, 웹훅 skip 명시 로깅) |
| `acp/web/templates/` | 최근 알림 패널 — 기존 `/api/live/stream` SSE 재사용 |

[아키텍처 결정]
- 알림은 **전이(transition)** 기반: 이전 상태 vs 현재 상태가 바뀌고, 현재가 알림대상일 때만. 폴링 스냅샷마다 발사 금지.
- dedupe 없으면 폴링 주기마다 같은 HOLDING이 반복 발사됨 → **반드시 쿨다운**.
- 웹훅은 httpx.post 동기/비동기 일관성 유지. 실패는 예외 전파+로깅(C3), 토스트는 best-effort지만 실패 로깅.
- 토스트는 win32 한정 — 비win32는 skip을 명시 로깅(silent fallback 금지).

[불변 원칙]
- silent fallback 금지: 채널 미설정/실패는 로그로 표면화. 알림 누락을 조용히 넘기지 않음.
- 수집·판정·알림 경로에 LLM 0회.
- 앱 아티팩트 read-only.
- Timebox: 전이 감지/dedupe가 3회 안 풀리면 멈추고 보고.

[검증/DoD]
1) 결정 테스트: 전이 매트릭스(→HOLDING/STALE/ERROR만 발사, LIVE/RUNNING/IDLE 무발사; Codex DONE 없음),
   HOLDING→STALE 승급 별도 발사, dedupe 쿨다운(중복 억제 + 복귀 후 재발사 허용), 웹훅 payload 빌더(slack/discord/generic).
2) Live smoke: 실제(또는 주입된) HOLDING 전이 1건 → 토스트 또는 웹훅 1회 발행 캡처(artifact 경로).
   ★ZTR형 홀딩이 실제로 알림으로 떨어지는 것★을 end-to-end로 확인.
3) `pytest -q` 통과(명령+결과).
4) 리뷰(둘 다 Implementer 세션이 집행 — 스킬 §8): Nitpicker local LLM PASS(repo 래퍼 우선, 없으면
   `jemmin_cli.py --provider ollama --no-daemon`). 별도 Reviewer는 **Implementer가 독립 컨텍스트 서브에이전트로 띄워** 수행
   (L2 설계 변경, 저장/동시성/마이그레이션 리스크, 사용자 요청, Nitpicker가 판단하기 어려운 아키텍처 이슈 시).
   로컬 LLM 서비스/모델 미준비 시 `blocked: local LLM unavailable`로 보고하고 Nitpicker PASS를 주장하지 않는다.
5) Sync-Out: PHASE.md frontmatter(P3/done/updated_at, 전체 phases done) + HANDOFF +
   `docs/lessons/p3-notify.md`. 커밋 확인 후 v1 완성. **태그는 생성하지 않음**(스킬 §9 — 경계는 커밋/HANDOFF로 기록).

[완료 보고] 변경 파일 / 전이·dedupe 결정 위치 / 검증(명령+출력+Live smoke artifact) /
PASS는 어디까지·NOT CLAIMED(통제·브리핑·SLA 미주장)·가정 / v1 종합 상태 + v2 후보(브리핑/양방향 통제).
