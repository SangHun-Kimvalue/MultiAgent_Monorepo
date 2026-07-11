작업: Agent Control Plane — Phase P0 (코어 스캐폴드: 수집→저장→상태→대시보드 골격). 한국어로 응답/주석.

[브랜치] 새 브랜치 만들지 말 것. `AgentControlPlane/`에서 git init 후 단일 브랜치 `acp-v1`로 작업/커밋.
완료 시 `git tag acp-phase-0`로 경계 표시. 시작 전 `git status`.

[전제] 신규 그린필드. 선행 페이즈 없음. Discovery는 DISCOVERY_PASS 완료. 이 프롬프트가 첫 구현.

[SoT — 반드시 읽을 것]
- `AgentControlPlane/PHASE.md` (로드맵 + §0 결정 로그 + §2 공개 API + §4 불변 원칙) ← 최우선
- `AgentControlPlane/discovery/design.md` (상태전이/경계/SSOT)
- `AgentControlPlane/discovery/phase_md_format.md` (PHASE.md frontmatter 스키마)
- 복제 참조(읽기만): `ZeroTokenRoundtable/src/web/app.py:192-214`(SSE 패턴),
  `ZeroTokenRoundtable/src/engine/session_store.py:109-119`(SQLite WAL 패턴)

[전략 리마인더] minimal-first. P0은 **실제 앱 수집 금지** — `FakeCollector`로 전체 파이프라인 골격만
세운다. "수집 가능 ≠ 지금 구현." 실 Codex 수집은 P1.

[이번 범위]
할 것:
1. 패키지 스캐폴드: `acp/` 패키지 + `pyproject.toml`([dev]에 pytest, pytest-asyncio).
   deps: fastapi, uvicorn[standard], sse-starlette, jinja2, pydantic>=2, pyyaml, httpx. `pip install -e ".[dev]"` 동작.
2. `acp/models.py`: PHASE.md §2의 `SessionRecord`(Pydantic v2) + `SessionState`(StrEnum) 그대로 정의.
3. `acp/collectors/base.py`: `BaseCollector(ABC)` (`app_name`, `collect()->list[SessionRecord]`).
   `acp/collectors/fake.py`: `FakeCollector` — 고정 픽스처 레코드 N개 반환(LIVE/IDLE/HOLDING 섞어서).
4. `acp/phase.py`: `parse_phase_md(path)->PhaseDoc|None`. frontmatter(YAML)만 파싱. 미지원 `acp_schema`/파싱
   실패 → None 반환 + 경고 로깅(C3, silent 금지). `PhaseDoc`은 Pydantic.
5. `acp/store.py`: SQLite WAL(`./.acp/acp.db`). `upsert_session`, `list_sessions`, `append_event`(events.jsonl도 append).
   ZTR session_store.py의 connect/PRAGMA/executescript 패턴 차용.
6. `acp/liveness.py`: `derive_state(record, now, cfg)->SessionState` **순수함수**. P0은 시간기반 기본 판정만
   (LIVE/IDLE/UNKNOWN). HOLDING/STALE 정밀 로직은 P1에서 확장(자리만 마련, 주석 TODO(P1)).
7. `acp/poller.py`: asyncio 주기 루프. 등록된 collector들 `.collect()` → `store.upsert` → 상태 derive → broadcaster publish.
8. `acp/web/app.py`: FastAPI. `GET /`(Jinja2 대시보드: 세션 테이블), `GET /api/sessions`(JSON),
   `GET /api/live/stream`(SSE, ZTR broadcaster subscribe/unsubscribe 패턴). 최소 템플릿 + static.
9. `acp/config.py` + `config/paths.yaml`: 앱 아티팩트 경로 외부화(P0은 미사용이나 스키마 정의). poll_interval 등.
10. `__main__.py`: `python -m acp web`로 uvicorn 기동, FakeCollector 등록.

★ out-of-scope(다음 페이즈) ★: 실제 Codex/Claude/Cursor 파싱(P1/P2), HOLDING/STALE 정밀판정(P1),
세션×PHASE 조인 표시(P2), 알림(P3). 여기서 손대지 말 것.

[부착점/대상] (신규 파일 위주. 참조는 시그니처로 재확인)
| 파일 | 작업 |
|---|---|
| `acp/web/app.py` | ZTR app.py:192-214 SSE 골격 이식(broadcaster=asyncio.Queue pub/sub) |
| `acp/store.py` | ZTR session_store.py:109-119 connect/PRAGMA(WAL,foreign_keys)/executescript 패턴 |
| `acp/models.py` | PHASE.md §2 시그니처 그대로 |

[아키텍처 결정] (틀리기 쉬운 지점 못박음)
- `derive_state`는 **순수함수**: 입력은 (record, now, cfg)뿐. 내부에서 `datetime.now()` 호출 금지(테스트 위해 주입).
- collector는 **read-only**: 파일을 절대 쓰지 않음. 예외는 삼키지 말고 전파/로깅(C3).
- broadcaster는 ZTR처럼 subscribe()→Queue, finally unsubscribe. SSE는 30s keepalive ping.
- SQLite는 단일 커넥션 직렬화(P0). 폴링 루프와 웹 요청이 공유하면 lock 주의 → 커넥션 접근을 store 내부로 캡슐화.

[불변 원칙]
- silent fallback 금지: 파싱/스키마 실패는 None+경고, UI엔 UNKNOWN. 옛 데이터 현재처럼 표시 금지.
- 앱 아티팩트/PHASE.md 읽기 전용.
- 수집·판정·표시 경로에 LLM 호출 0.
- Timebox: 같은 축 3회 안 풀리면 멈추고 실패 로그+원인+다음 접근 보고.

[검증/DoD]
1) 테스트(`tests/`): `parse_phase_md`(정상/미지원스키마/깨진 YAML), `derive_state`(시간 mock으로 LIVE/IDLE/UNKNOWN),
   `store.upsert→list` 왕복, `FakeCollector.collect` 스키마 검증.
2) 빌드/실행: `pip install -e ".[dev]"` → `pytest -q` 전부 통과(명령+결과 캡처). `python -m acp web` 기동 →
   `GET /`에 Fake 세션 표출 + `/api/live/stream` SSE 1이벤트 수신 확인(Live smoke 골격).
3) 리뷰: Nitpicker local LLM PASS(repo 래퍼 우선). 별도 Reviewer 세션은 L2 설계 변경, P1/P2급 리스크, 사용자 요청, 또는 Nitpicker가 판단하기 어려운 아키텍처 이슈가 있을 때 수행. REJECT→수정→재실행.
4) Sync-Out: `PHASE.md` frontmatter(current_phase=P0, phase_status=done, updated_at) 갱신 +
   `docs/HANDOFF.md` 생성 + `docs/lessons/p0-scaffold.md` append. 커밋은 사용자 확인 후. `git tag acp-phase-0`.

[완료 보고] 변경 파일 / 핵심 결정·부착 위치 / 검증 결과(통과 명령+출력) /
PASS는 어디까지·NOT CLAIMED(실앱 수집 안 함)·가정 / 다음 페이즈(P1) 준비 상태.
