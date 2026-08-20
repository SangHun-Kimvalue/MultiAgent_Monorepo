# HANDOFF — ACP 관측 정직성 트랙 (T14)

작성: 2026-07-28
Phase: **트랙 종료 후 S6 추가**(2026-07-28) — 실사용에서 드러난 표시·성능 결함 수정. 잔여는 아래 "열린 항목" 참조
상태: `93e06a1`(S1) · `b0326b4`(S2) · `dbf3e9e`(S3) · `3c7677b`(S4a) · `2c94a9e`(S4b) ·
`009f115`(S4c-1) · `7597509`(S4c-2) · `ea6d608`(S5) · `S6`. **미푸시**.

> 아래 "V1 검증 하니스" 절은 2026-06-11 시점 기록이며 **일부가 stale**이다. T14가 바꾼 계약이
> 우선한다(코드·테스트가 정본). 구체적으로:
> - ~~"행동 필요만 토글은 HOLDING/STALE/ERROR"~~ → S2에서 **action=HOLDING+ERROR /
>   cleanup=STALE** 두 축으로 분리됐다.
> - ~~"서버측 필터/페이지네이션은 범위 밖"~~ → S1에서 **서버측 상태·앱 필터**가 도입됐다.
> - `/api/sessions`는 리스트가 아니라 **봉투**다. S4a에서 `archived` scope·분포 분리가,
>   S4b에서 `collector_health` 합집합·`app_signal_details`가 더해졌다.

---

## T14 진행 (2026-07-27~28)

### 완료·커밋됨

| Slice | 커밋 | 핵심 |
|---|---|---|
| S1 조회·집계 정직성 | `93e06a1` | 정렬키를 `last_activity`로, 창·집계 단일 read 스냅샷, 서버 필터(도달 가능성), 단일 비행 + AbortSignal, 절단 고지 |
| S2 상태 판정 정직성 | `b0326b4` | abort 어휘를 턴 종료 축으로, in-turn/시간폴백 STALE 단계, action/cleanup 등급 분리, 알림 freshness 게이트 |
| S3 수집 정직성 | `dbf3e9e` | 실행 신호 조인·`ProcessSnapshot`(실패≠빈결과)·`collector_cycles` 건강도·`purge` 안전계약 |
| **S4a 보관 사실의 정직한 표현** | `3c7677b` | `archive_observed_at` 기록(해제 복귀·최초 시각 보존) · `archived=include·exclude·only`(기본 include, 미지값 422) · **분포 분리**(`by_state`=현재 관측 대상 / `archived_by_last_known_state`) · 보관은 관제 제외 · 봉투가 범위를 말함 · **정적 자원 내용 해시 버전** · 최소 UI 소비 |
| **S4b 수집 건강도 정직성** | `2c94a9e` | 건강도 키 = `sessions ∪ cycles`(미기록은 `no_cycle_record`+`session_count`) · `BaseCollector.last_cycle` **추상 승급** · codex·cursor **failure-as-empty 교정** · 폴러 합성은 `completed_unknown`까지만 · **축별** `count_scopes`(fail-closed) · `app_signal_details` 병행 필드 |
| **S4c-1 실행 증거 정직성** | `009f115` | capability/observation **두 축 분리**(기본 `unknown`) · 폴러 가용성 `== "ok"` **fail-closed**(화면 `확인 불가`와 상태 `STALE`이 어긋나던 경로) · `evidence.py`가 마커 단일 소유 + AST 쓰기 스캔 · API `execution_evidence_kind` enum · UI 4값 분리(enum만 소비, 마커 원문 비노출) |
| **S4c-2 상태 어휘 단일 출처** | `7597509` | 어휘·순서·후보를 겸하던 클라 배열 제거 · 응답이 **세 축**(`state_vocabulary`·`state_display_order`·`observed_states`)을 나눠 실음 · **수용 어휘=계약 ∪ 관측**(422는 오타에만, scope 무의존) · 판정을 read 트랜잭션 안으로(단일 스냅샷) · **구조화 422**와 선택 보존 · 미인식 상태도 배지·필터로 **도달 가능** |
| **S6 알림 표시 · 틱 비차단** | (아래) | 알림 표시 모양을 **한 곳에서** 정의(초기 로드가 `payload`를 안 펴서 화면이 전부 `UNKNOWN`/`-`였다) · 세션 쓰기 **전용 연결**(`synchronous=NORMAL`, 주 연결은 FULL 유지 — 승인 감사는 재유도 불가) · 틱을 **스레드/루프 두 단계**로 분리. 실측 `_tick` 4737ms→920ms · **루프 차단 4700ms→16~32ms** |
| **S5 "정리 대상" 이름-사실 정합** | `ea6d608` | 축 이름 `cleanup_*`→`inactive_*`(옛 이름은 투영) · 라벨 `정리 대상`→`비활성` · **비활성은 경고 등급을 만들지 않는다**(정상 상태가 영구 경고가 되던 경로 제거) · `purge`가 **재생성 사실** 고지 |
| 로드맵 등재 | `42b6cbc` `1885b17` `52672d4` `50c0cf9` `1070d28` `36fcdf3` | §10 T14 셀 |

**검증(2026-07-28 실측 · S6 포함)**: pytest **516 passed / 1 skipped** · ruff ·
mypy strict 29파일 · node JS 계약 3종. mutation 실측 누적 89건
(S4a 5 · S4b 16 · S4c-1 25 · S4c-2 28 · S5 3 · S6 12).
S4b의 재현되지 않은 1건은 게이트로 주장하지 않고 characterization test로 표기했다.

### S4c-2 상세 (커밋 `7597509`)

**상태 어휘의 단일 출처**. 설계 = `.ztr/orchestrator/T14-S4c2/planner-design.md`(개정 R5,
독립 설계검토 **5R APPROVED**) · 독립 구현리뷰 **5R APPROVED**
(`.ztr/orchestrator/T14-S4c2/impl-review-r*.txt`).

실측한 결함 — 클라 배열 `ACP_STATES` 하나가 **어휘·표시 순서·필터 후보·URL 화이트리스트**를
겸했다. 서버 응답에 그 배열 밖 상태를 실어 재현하니 **배지 합 1 vs 전체 101**(100건이 고지
없이 증발) · 필터 후보에 없어 **도달 불가** · 그 상태에서 `healthLabel="정상"`.

| 층 | 변경 |
|---|---|
| 어휘 소유권 | `models.state_vocabulary()`가 단일 출처. 응답이 **세 축을 나눠** 싣는다 — `state_vocabulary`(아는 것) · `state_display_order`(보여줄 순서, 제품 정책·순열 불변을 **기동 시** 검사) · `observed_states`(저장소 전량에 실제로 있는 것) |
| 수용 어휘 | `계약 ∪ 관측`. **저장소에 있는 값으로 거르는 질의는 답할 수 있는 질의**다. 422는 계약에도 데이터에도 없는 값에만. `archived` scope에 **의존하지 않는다** |
| 스냅샷(D7) | 수용 판정을 `sessions_view`의 read 트랜잭션 **안으로**. 판정·응답·422 `accepted`가 한 시점. `observed_states`는 기존 두 분포의 키 합집합이라 **추가 스캔 없음** |
| 오류 계약 | 422 `detail`이 구조다(`code`·`invalid_values`·`accepted`). 클라는 그 필드로만 분기하고 `code`가 없거나 모르면 **일반 실패**로 처리 |
| 선택 보존 | 거절된 값은 **지우지 않고** "적용되지 않음"으로 표시, 다음 질의에서만 제외. 서버가 다시 수용하면 표시를 거둔다(어휘 미상 응답에서는 거두지 않는다) |
| 어휘 밖 상태 소비 | 폴러가 원문(`prev_value`)과 enum(`prev_state`)을 함께 들어, 보관 행 보존·전이 이벤트 `from`·알림 게이트가 **사실을 잃지 않는다** |
| UI | 리터럴 사본 2개 제거 · 미인식 상태도 배지·건수·필터 후보로 **보이고 도달 가능** · `badge-unrecognized` · 어휘 미상/미인식/분포 합/거절 고지 · 비교 불능은 `분포 합계 확인 불가` |

**검증**: pytest **496 passed / 1 skipped**(+20) · ruff · mypy strict 28 · node JS 계약 3종 ·
**mutation 28건 실측**. 라이브(실 DB 사본 + 브라우저): 어휘 밖 `quarantined`·`retired`가
배지·건수로 보이고 필터 후보에 오르며, **보관 행에만 있는 `retired`로 실제 2행에 도달**.
오타는 422 + 구조화 detail로 거절되고 선택은 남은 채 고지된다.

구현리뷰가 잡은 것(반복 금지): 보관 행의 어휘 밖 상태를 enum으로 강제해 `unknown`으로
덮어쓴 것(내가 만든 회귀) · 422 재조회 **무한 재귀** · `actionOnly`가 거절값을 다시 전송 ·
거절의 영구화 · 전이 이벤트가 `from: null`로 사실을 잃은 것 · 어휘 미상인데 관측만 보고
거절을 거둔 것 · **표시 순서 배열에서 소속을 유도**한 것.
설계검토가 잡은 것: 도달 가능성 오주장 · 산문 파싱 강요 · 실측 서술과 재현 경로 불일치 ·
`observed_states` 유도 범위 · 스냅샷 경계 · 폴백 우선순위 · 검증 계획 내부 모순.

**자기 점검으로 잡은 것**: mutation 검증에서 `-k` 패턴이 아무 테스트도 매칭하지 않아
pytest exit 5(수집 0건)를 "게이트 작동"으로 오독했다 — 실제 게이트를 만들고 **수집 건수
확인**을 절차에 넣었다.

### S4c-1 상세 (커밋 `009f115`)

**실행 증거를 있는 그대로 그린다**. 설계 = `.ztr/orchestrator/T14-S4c/planner-design.md`
(개정 R4, 독립 설계검토 **4R APPROVED**) · 독립 구현리뷰 **7R APPROVED**
(`.ztr/orchestrator/T14-S4c/impl-review-r*.txt`).

무엇을 고쳤나 — `실행 명령`/`PID` 열이 **"안 돌고 있음"과 "신호를 얻을 수 없음"을 똑같이
`-`로** 그렸고, 서버조차 그 둘을 구분하지 못했다(`process_signal` 기본값이 `"ok"`라
아무도 선언하지 않아도 "정상 관측"으로 읽혔다).

| 층 | 변경 |
|---|---|
| 수집기 계약 | `CollectCycle`에 `process_signal_capability`(기본 `unknown`) 추가 · `process_signal` 기본값 `"ok"`→`"unknown"` · claude·codex·fake=`supported`, cursor=`unsupported`+`not_applicable` · codex는 실행 신호 소스의 관측 결과를 명시 선언(부재·형식오류·항목 오류 → `unavailable`) |
| 판정(D1c) | `poller`의 가용성 판정을 `!= "unavailable"` → **`== "ok"`**. 옛 식은 넓어진 어휘와 합성 경로의 `None`을 전부 가용으로 읽어, **화면은 `확인 불가`인데 상태는 `STALE`로 확정**됐다 |
| 저장 | `collector_cycles.process_signal_capability`(멱등 마이그레이션) · 쓰기는 두 축 모두 계약 밖 값 거절 · 읽기는 NULL·미지값을 `unknown`으로 닫는다(옛 행 소급 인증 금지) |
| API | `collector_health[app]`에 두 축 · 세션 행에 `execution_evidence_kind ∈ {command, process_recheck, none, unknown}` projection(**미지 값을 명령으로 승격하지 않는다**) |
| 마커 | `acp/evidence.py`가 마커를 **단일 소유**. 예약 네임스페이스 `acp:` + **AST 스캔 게이트**가 `running_cmd`에 쓰는 값이 등록표에서 오는지 강제한다 |
| UI | 4값 분리(실행중 / `-` / `신호 없음` / `확인 불가`) · **enum만 소비** · `process-resume` 원문은 텍스트·`title` 어디에도 넣지 않는다 |

**검증**: pytest **476 passed / 1 skipped**(+36) · ruff · mypy strict 28 · node JS 계약 2종 ·
**mutation 25건 실측**. 라이브(실 수집 1틱 + 브라우저): claude·codex `supported/ok` ·
cursor `unsupported/not_applicable`(화면 11건 `신호 없음`) · 사이클 없는 앱 `unknown/unknown`
(`확인 불가`) · claude 실행 세션은 `실행 확인됨`+PID · DOM에 마커 원문 없음.

구현리뷰가 잡은 것(반복 금지): 쓰기 검증을 한쪽 축에만 걸기 · 마커 강제를 **읽기 쪽**에만
두기(임의 문자열인 명령어와 구분 불가라 강제는 쓰기 쪽이어야 한다) · 그 보완이 만든 스코프
오탐 · 항목 오류에도 앱 신호를 `ok`로 유지 · `NaN`/`Infinity`를 타입만으로 통과 ·
임의 정밀도 int가 **판별자 자신**을 죽여 국소 오류를 전면 실패로 번지게 한 경로.

설계검토가 반려한 원안(반복 금지): `count_scopes.matched == "not_applicable"`을 capability
근거로 쓰기(codex는 그렇게 선언하고도 `osPid`로 PID를 준다) · `fake=unsupported` ·
하류 파급을 질문으로만 남기기.

### S6 — 트랙 종료 뒤에 실사용에서 드러난 것 (2026-07-28)

UI를 띄우자마자 두 결함이 보였다. **범위 확장이 아니라 실측으로 발견된 것**이다.

| | 결함 | 고친 방법 |
|---|---|---|
| 표시 | "최근 알림"이 전부 `UNKNOWN`/`-` — 저장 행은 표시 필드를 `payload` 안에 두는데 화면은 최상위에서 읽었다(SSE 경로만 평평해서 초기 로드만 깨져 있었다) | `acp/notify_view.py`가 모양을 **한 곳에서** 정의하고 두 경로가 같이 쓴다 |
| 성능 | 틱이 15초마다 **3.7~4.7초**, 전부 동기라 **루프를 25~30% 차단** | 세션 쓰기 **전용 연결**(`synchronous=NORMAL`) + 틱을 **스레드/루프 두 단계**로 분리 |

**가설을 측정으로 기각했다**: 원인을 "행마다 커밋"으로 보고 배치를 설계했는데 실측하니
효과가 없었고(640 vs 728ms), 진짜 원인은 `synchronous=FULL`이었다(FULL 682ms vs
NORMAL 17ms / 360행). 측정 없이 갔으면 효과 없는 리팩터링을 "고쳤다"고 보고했을 것이다.

**내구성 경계**: `synchronous`는 연결 단위다. 주 연결에는 **재유도되지 않는** 승인 감사가
흐르므로 FULL로 두고, 매 틱 재수집되는 세션 행만 전용 연결로 뺐다.

**실측**: `_tick` 4737ms → **920~1099ms**, 루프 최대 차단 4700ms → **16~32ms**.
브라우저에서 알림이 제목·상태와 함께 정상 렌더. LESSON-009.
구현리뷰가 잡은 회귀: **반영 실패한 틱을 `success_complete`로 고지**하던 것(전용 연결이
만든 새 실패 경로) — `partial`로 낮추고 게이트를 세웠다.

### 트랙 종료 (2026-07-28 · 사용자 판단)

**관측 정직성의 목적은 달성됐다.** 화면이 모르는 것을 안다고 말하지 않고, 집계에서 뺀 것을
도달 불가로 만들지 않으며, 할 수 없는 일을 시키지 않는다. 여기서 트랙을 닫는다.

**닫으면서 정직하게 남기는 것**

- **공정 무게가 과했다.** 슬라이스당 1300~1700줄 · 리뷰 5~10라운드였고, 후반 라운드는
  슬라이스 주제를 벗어난 일반 강건성으로 흘렀다(실행 증거 표시 슬라이스에서 `NaN`/
  `Infinity` 처리). "P1 0건까지 무한 루프" 규칙이 비용을 폭증시켰다. 마지막 슬라이스(S5)는
  대상 크기에 맞춰 **리뷰 1라운드 · 테스트 4개 · mutation 3건**으로 끝냈다. LESSON-008.
- **리뷰어는 한 번도 테스트를 실행하지 못했다**(read-only 샌드박스). 전 슬라이스의 모든
  라운드가 정적 검토였고, 실행 검증은 전부 구현 세션이 했다.
- 계획이 처음부터 길었던 것이 **아니다**. 등재 시점(`42b6cbc`)에는 "Slice 1 완료 ·
  Slice 2 대상 고정"뿐이었고, 매 슬라이스의 실측이 다음 결함을 드러내며 늘어난 열린
  목록이었다(S4 원안 5결함 → 3분할, S4c → 다시 3분할). **열린 목록이라는 사실 자체를
  더 일찍 드러냈어야 했다.**

### 열린 항목 (필요해지면 재개)

| 항목 | 상태 |
|---|---|
| S4c-3 보관 표시 정책 · 수집 건강도 줄 | 보류 — 대상·선결 조건은 아래 기록에 그대로 |
| S4d 쓰기 원자성 | 보류 — 사용자에게 보이지 않는 내부 정합성 |
| S4e 앱별 풍부한 사이클 사실 | 보류 |
| prune/`missing` | **데이터 0건**으로 무산(원본이 사라진 행이 없다). 메커니즘은 실재 |
| `SessionState` enum rename | S4c-2가 **안전하게 만들어 뒀다**(어휘가 서버 소유, 어휘 밖 상태도 도달 가능) |
| `apps` 파라미터 검증 비대칭 | states는 422, apps는 무검증. S4c-2의 계약을 그대로 적용하면 된다 |

---

### (이전) 범위 조정 (2026-07-28 · 사용자 판단)

관측 정직성의 목적은 **S4c-2까지로 달성**됐다고 보고, 아래 셋을 **보류**한다.
화면이 모르는 것을 안다고 말하지 않고, 집계에서 뺀 것을 도달 불가로 만들지 않는다.

- **보류**: S4c-3(보관 표시 정책·건강도 줄) · S4d(쓰기 원자성) · S4e(앱별 풍부한 사실).
  전부 유효한 대상이지만 사용자에게 보이는 값이 작거나(S4c-3·S4e) 화면에 드러나지
  않는다(S4d). 필요해지면 아래 기록으로 그대로 재개할 수 있다.
- ~~**다음 = prune/`missing`**~~ → **무산**. 실측 결과 원본 파일이 사라진 행이 **0건**이고
  (codex 758 · claude 360 · cursor 11 전부 존재), 수집기가 시간 컷오프 없이 전량을 재스캔해
  지운 행이 재생성된다. 대신 **S5**(이름-사실 정합)를 하고 트랙을 닫았다.

이 트랙이 길어진 이유도 함께 남긴다 — **계획이 처음부터 길었던 것이 아니다.** 등재
시점(`42b6cbc`)에는 "Slice 1 완료 · Slice 2 대상 고정"뿐이었고, 매 슬라이스의 실측이
다음 결함을 드러내고 설계검토가 회귀면 분할을 권고하며 늘어난 **열린 목록**이었다
(S4 원안 5결함 → 3분할, S4c → 다시 3분할).

### 보류된 슬라이스 (재개용 기록)

1. **S4c-3 보관 표시 정책 + 건강도 줄** — 대시보드가 `archived=exclude`를 명시 요청.
   **선결**: 서버 최초 렌더(기본 include)와 후속 조회 scope 불일치, 현재 scope의 URL 상태화.
2. **S4d 쓰기 원자성** — 폴러가 사이클을 세션 루프 **앞에서** 커밋하고 `upsert_session`이
   **행마다** 커밋한다. "완결된 한 수집 결과"는 여전히 보장되지 않는다.
3. **S4e 앱별 풍부한 사이클 사실** — codex 조인 카운트·cursor 신호 품질 등.
4. prune/`missing` 판정 · `apps` 파라미터 검증 비대칭(S4c-2가 states에 세운 계약을 apps에
   적용) · `SessionState` enum rename(**S4c-2가 이것을 안전하게 만들었다** — 어휘가 서버
   소유이고 어휘 밖 상태도 도달 가능하므로 rename이 화면에서 데이터를 지우지 않는다).

### S3 상세 (커밋 `dbf3e9e`)

**T14 Slice 3 — 수집 정직성**. 변경 11파일(코드 8 · 테스트 2 신규 · 문서 1).

- `proc.py`: `ProcessSnapshot(status/observed_at/processes/error/malformed_uuid)` ·
  argv 분해 + exact token 파서 · `ProcessSnapshotCache`(TTL 30s·단일비행·timeout 5s).
  **argv 원문은 모듈 밖으로 나가지 않는다.**
- `collectors/claude.py`: `CollectCycle` · 스냅샷 주입 · `--resume`↔`cliSessionId` 조인
  (중복 시 최소 pid) · ~~archived 기본 제외~~(**S4a에서 철회** — 플래그로 표시해 넘긴다)
  + 스캔이 본 건수 · freshness 검사 · 정직성 카운트
  (matched/unmatched/ambiguous/malformed).
- `liveness.py`: `process_signal_available`(신호 없으면 STALE→UNKNOWN) ·
  6.5 분기(재확인 마커 `process-resume` + 살아있는 PID → RUNNING).
- `store.py`: `collector_cycles` 테이블 + 멱등 마이그레이션 · `preview_app_rows` /
  `purge_app_rows`(삭제와 감사 **같은 트랜잭션**).
- `poller.py`: 사이클 기록(수집기 예외도 `failed` 사이클로) · 신호 가용성 전달.
- `web/app.py`: 사이클 고지 · `collector_health` · `app_signals`.
  (~~`summary.excluded_archived`~~ → **S4a에서 폐기**. 응답 이름은
  `summary.archived_seen_in_latest_cycle` / `collector_health.*.archived_seen`이며,
  DB 컬럼 `collector_cycles.excluded_archived`는 감사 이력으로 보존된다.)
- `__main__.py`: `--fake` → `.acp/acp-fake.db` 격리 · `purge --app <name> [--yes]`.

**검증(이 세션 실측)**: pytest **370 passed, 1 skipped** · ruff · mypy strict 27파일 ·
node JS 계약 17 케이스. 라이브: claude 358→112(archived 246 제외·고지),
**실행 중 세션 7개 전부 RUNNING**(이 세션 포함), 기존 DB 마이그레이션 후 INSERT 성공,
`purge --app fake` 미리보기가 6행 표시 후 **삭제하지 않음**.

**게이트**: 독립 설계 검토(교차 계열 Codex) 4R → PASS · 독립 구현 리뷰 3R(P2 11건 반영).

### NOT CLAIMED (T14 전체)

- e2e(playwright 미설치 + `addopts -m 'not e2e'`) — 수정은 했으나 통과 증거 없음.
- 브라우저 e2e(playwright 미설치). S4a는 DOM 실측으로 확인했으나 e2e 게이트는 아니다.
- 리뷰어 측 기계 검증(샌드박스 정책으로 재실행 불가; 적대적 에이전트 1회만 직접 실행).
- prune/`missing` 판정(계속 유보 — S3에서 수집 건강도만, S4b에서 관측 부재 표기만 도입).
- Windows 네이티브 프로세스 열거(PowerShell/CIM fallback만) · Windows 외 플랫폼.
- UI 반영: **부분 해소**. archived 범위 고지·상태 분포 라벨·보관 분포 도달은 S4a(`3c7677b`),
  실행 증거 4값 구분은 S4c-1(`009f115`), 상태 어휘는 S4c-2(`7597509`)에서 화면까지 갔다.
  남은 것은 건강도 줄(S4c-3) — **보류**(아래 범위 조정).
- 마커 쓰기 스캔(S4c-1)은 **호출·f-string·문자열 결합 경유를 잡지 못한다**. 모듈 상수와
  `acp.evidence` import까지만 따라간다(테스트 docstring에 한계로 명시).
- 상태 리터럴 스캔(S4c-2)도 같은 한계다. 실질 게이트는 **계약 연동 검사**(서버 어휘를
  바꾸면 후보·KPI·순서·URL 복원이 함께 바뀐다)이며, 스캔은 보조 hygiene이다.
- `apps` 파라미터 검증 비대칭(states는 422, apps는 무검증)은 **그대로 남았다**(S4c-2 범위 밖).
- 저장소 밖 API 소비자의 422 `detail` 타입 변경(문자열→객체) 호환.
- S3 검증 계획 잔여 케이스: 단일비행 동시성 · TTL 신규 탐지 지연 · 캐시 PID 종료 ·
  실패→회복 · fake DB 격리 e2e.

### S4a 상세 (커밋 `3c7677b`)

**T14 Slice 4a — 보관(archived) 사실의 정직한 표현**. 설계=`.ztr/orchestrator/T14-S4/planner-design.md`(R7).

고친 결함(실측): `summary.excluded_archived=246`이 `total=1073` 옆에 실려 **일어나지 않은
제외를 일어났다고 단언**했다. `sessions`에 archived 컬럼이 없어 수집기의 제외는 새 upsert만
막았고, 246행은 총계·상태 분포·"정리 대상"에 그대로 남아 있었다.

- `models.py`: `SessionRecord.archived`.
- `store.py`: `sessions.archive_observed_at`(멱등 마이그레이션) · 최초 관측 시각 보존·해제 복귀 ·
  `archived=include|exclude|only` 범위 필터(전량 적용) · **분포 분리**(`by_state`=보관 미관측 /
  `archived_by_last_known_state`=보관) · 사이클을 세션과 **같은 read 트랜잭션**에서 읽기.
- `collectors/claude.py`: `_ARCHIVED` sentinel 제거 — 보관도 레코드로 넘긴다.
- `poller.py`: 보관은 **관제 대상 제외**(재판정·`state_change`·SSE·알림·action/cleanup).
- `web/app.py`: 봉투에 `archived_scope`·`archive_not_observed_total`·`archived_total`·
  `state_distribution_scope`·`action_scope`, 옛 이름 폐기(중첩 포함), 미지 값 **422**,
  정적 자원 **내용 해시 버전**.
- `config.py`: `include_archived` deprecation 경고(키가 있으면 **값과 무관하게**).
- UI(D8 최소 소비): KPI 범위 고지 · 상태 분포 범위 라벨 · 보관 분포 도달 경로.
- 테스트: `tests/test_archive_scope.py`(신규 27) · JS 계약 +4 · 옛 계약 2파일 갱신.

**검증(이 세션 실측)**: pytest **397 passed, 1 skipped** · ruff · mypy strict 27 · node JS 21.
mutation 5건(되돌리면 각각 실패). 라이브(실 DB 사본 + 실 수집 1사이클): `total 1073` 불변,
`827 + 246` 등식, `cleanup_required 1043→799`. 브라우저: 범위 고지·라벨·보관 분포 표시.

**게이트**: 독립 설계검토 **7R APPROVED**(원안 `total` 재정의·기본 exclude·0세션 사이클 제외
전부 반려) · 독립 구현리뷰 **2R APPROVED**. 완료 게이트 G(API+최소 UI 한 커밋) 충족.

### NOT CLAIMED (S4a)

- **수집 배치의 쓰기 원자성** — 사이클 선커밋·행별 커밋 그대로(→ **S4d**).
- 파일이 사라진 보관 세션의 분류(prune/`missing` 트랙) — `archive_not_observed_total`에 잔류.
- 브라우저 e2e(playwright 미설치) · `<details>` 펼침 동작 · 스크린샷(패널 미표시).
- 리뷰어 측 기계 검증(읽기 전용 정책으로 재실행 불가 — 리뷰어가 명시).

### 다음 작업 (순서) — **문서 상단 "후속 슬라이스"가 정본**

이 절의 옛 목록은 S4a·S4b가 닫은 항목을 "잔여"로 담고 있어 **삭제**했다(닫힌 항목이 다른
곳에 잔여로 남는 stale 차단). 현재 순서는 이 문서 상단 **"후속 슬라이스 (대상 고정됨)"**를
보라: 위 **트랙 종료** 절이 정본이다.

옛 §10 정렬 보류 사유도 해소됐다 — 다른 세션이 `78cd410`으로 커밋해 `50c0cf9`·`1070d28`에서
T14 셀을 정렬했다.

---

## (이전) V1 검증 하니스 정비 — 2026-06-11 기록

작성: 2026-06-11
Phase: **V1 review** — 단위/e2e 게이트 분리 + 실제 SSE 통합 e2e + 리뷰 게이트 정직화
상태: V1 코드/테스트/실앱 API smoke/Reviewer/V1 diff Nitpicker 완료. P3/U1/U2 과거 페이즈 Nitpicker 전체 클로즈는 local LLM timeout/stale analyzing으로 blocked.

---

## V1 결과

해소:
- U1 Reviewer MAJOR: `pytest -q`에 e2e가 섞이던 문제를 `@pytest.mark.e2e` + `addopts = "-p no:pytest_playwright -m 'not e2e'"`로 분리.
- U1 Reviewer MINOR: KPI 프로젝트 수가 `no-project`를 그룹/필터와 같은 기준으로 세도록 통일.
- 통합 e2e: 직접 `app.apply*()` 호출을 제거하고, fake collector 전이 옵션으로 poller → broadcaster → 실제 `state_change`/`notification` SSE → dashboard handler → KPI/필터/정렬 흐름 검증.
- 실앱 초기 알림 폭주: fresh DB baseline 수집은 알림 전이가 아니므로 `prev_state is not None`일 때만 notification 발행.

검증:

```text
python -m pytest -q
107 passed, 7 deselected in 1.20s

python -m pytest -m e2e tests/e2e -q
7 passed in 13.10s

python -m compileall -q acp
PASS

git diff --check
PASS (CRLF normalization warnings only)
```

실앱 API smoke:

```text
port: 8913
mode: real collectors (Codex/Claude/Cursor), fresh db/log
/api/sessions total: 100
artifact: .acp/v1-live-smoke-afterfix-sessions.json
initial webhook/toast notification logs: 0
server process stopped
```

Reviewer:

```text
별도 Reviewer 1차:
- P2: 통합 e2e가 실제 SSE가 아니라 app.apply* 직접 호출이라 PASS 문구 과대 주장 위험.

수정 후 재확인:
- Blocking finding 없음.
- 실제 EventSource 연결 후 poller가 만든 state_change/notification SSE 결과를 기다리는 구조 확인.
```

Nitpicker:

```text
V1 current diff:
- acp/__main__.py: F401 지적 후 수정, 재확인 PASS
- acp/collectors/fake.py: PASS
- tests/e2e/conftest.py: PASS(리팩토링 권장 수준)
- tests/e2e/test_integration_full.py: PASS
- 기존 동일 diff 파일은 DUPLICATE_REQUEST_IGNORED(이전 delivered PASS/ignored 상태)

P3/U1/U2 historical phase closeout:
- DuckDB lock(PID 30300)은 현재 재현되지 않음.
- P3 phase-level rerun은 일부 REVIEW_PASSED 기록 후 acp/store.py가 stale analyzing/timeout으로 반복 blocked.
- U1/U2 과거 페이즈 전체 Nitpicker PASS는 주장하지 않음.
```

NOT CLAIMED:
- P3/U1/U2 과거 페이즈 Nitpicker 전체 PASS.
- 실앱 브라우저 UI smoke. 직전 real browser 접근은 page crash/timeout 이력이 있어 V1에서는 API smoke와 fake Playwright e2e로 분리 검증.
- `run.bat`는 untracked 의도 밖 파일로 제외.

---

## U2 Historical Summary

---

## U2 구현 요약

| 항목 | 내용 |
|---|---|
| 필터 상태 | `dashboard.js`: `selectedApps`, `selectedStates`, `selectedProjects`, `actionOnly` |
| 정렬 상태 | `dashboard.js`: `sortKey`, `sortDir`, `setSort()` |
| 파생뷰 | `filteredSorted`, `visibleGroups`, `visibleCount` 추가. 원본 `sessions`, `sessionList`, `kpis`는 전체 기준 유지 |
| 필터 UI | `dashboard.html`: 앱/상태/프로젝트 다중 선택 + "행동 필요만" 토글 + 초기화 |
| 정렬 UI | 테이블 헤더 버튼: 상태 심각도순, 최근활동, 앱, 프로젝트 |
| 상태 지속 | URL querystring에 필터/정렬 반영 및 새로고침 복원 |
| 빈 결과 | 필터로 0건이면 "조건에 맞는 세션 없음" 명시 |
| e2e | `tests/e2e/test_dashboard_u2.py`: 행동 필요 토글, 상태/프로젝트 필터, 실제 DOM 행 정렬 순서 검증 |

## 결정

- 필터/정렬은 서버 엔드포인트 없이 U1의 단일 `sessions` 모델에서 파생한다.
- KPI는 필터 비종속 전체 기준이다. 필터 적용 시에는 `visibleCount / kpis.total`만 별도 표기한다.
- 그룹 구조는 유지한다. 정렬은 그룹 보존 + 그룹 내 정렬이며, app/project 정렬은 그룹 순서에도 반영한다.
- "최근 활동" 정렬은 화면에 표시되는 `last_activity` 기준이다. 저장 갱신 시각 `updated_at`이 아니다.
- U2는 U1/P3 게이트 종료와 독립적으로 구현·검증·Reviewer 재확인을 완료했다. Nitpicker PASS는 주장하지 않는다.

## 검증 증거

```text
python -m pytest tests/e2e/test_dashboard_u2.py -q
3 passed in 4.38s

python -m pytest -m e2e tests/e2e -q
7 passed in 13.10s

python -m pytest -q
107 passed, 7 deselected in 1.20s

python -m compileall -q acp
PASS

git diff --check
PASS (CRLF normalization warnings only)
```

Reviewer:

```text
별도 Reviewer 서브에이전트 1차 findings:
- P1: 최근 활동 정렬이 updated_at 기준으로 동작
- P2: e2e가 실제 DOM 정렬 순서를 잡지 못함
- P3: app/project 첫 정렬 방향이 desc라 직관과 다름

수정 후 재확인:
- Blocking finding 없음
- 기존 3건 해소 확인
```

Nitpicker:

```text
Ollama reachable, qwen2.5-coder:7b available.
jemmin_cli local LLM direct run attempted.
blocked: Nitpicker Daemon .jemmin/analytics.duckdb lock by python.exe PID 30300.
Nitpicker PASS not claimed.
```

Browser smoke (U2 historical):

```text
url: http://127.0.0.1:8911/
separate db/log: .acp/u2-smoke.db, .acp/u2-smoke-events.jsonl
DOM rendered: KPI, filter band, action-only control, state/app/project menus, table rows
server stopped after smoke
```

## PASS / NOT CLAIMED

PASS:
- 필터/정렬 UI 렌더 및 파생 getter 동작.
- "행동 필요만" 토글은 `HOLDING/STALE/ERROR`만 표시한다.
- 상태/프로젝트 필터 적용 시 표시 행 수가 바뀌고 KPI 전체 기준은 유지된다.
- 상태 정렬은 그룹 내 심각도순을 만족한다.
- 최근 활동 정렬은 DOM row의 `last_activity` 기준 desc를 만족한다.
- 앱/프로젝트 첫 정렬 방향은 asc다.
- U1 e2e와 전체 pytest 회귀 통과.
- 별도 Reviewer 재확인에서 blocking finding 없음.

NOT CLAIMED:
- U1 Reviewer PASS는 아직 주장하지 않는다.
- U1/U2 Nitpicker local LLM PASS는 아직 주장하지 않는다.
- P3 Nitpicker 게이트 종료도 여전히 별도 남은 항목이다.
- 세션 상세 드릴다운, 서버측 필터/페이지네이션, 저장된 뷰 프리셋, 인증은 범위 밖이다.

## 후순위 후보

- 세션 상세 드릴다운.
- 저장된 뷰 프리셋.
- 대량 세션 대비 가상 스크롤 또는 서버측 페이지네이션.
- 브라우저 smoke 자동화 시 시스템 부하 완화를 위한 명시 opt-in 절차.
