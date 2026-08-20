# P2 Lesson — multi-app collector + PHASE join

작성: 2026-06-11

## WHY

P2의 가치는 앱별 세션을 많이 긁어오는 데 있지 않고, 각 세션을 프로젝트의 `PHASE.md`와 결정적으로 결합해 "누가 어떤 프로젝트의 어느 페이즈를 만지고 있는지"를 한눈에 보여주는 데 있다.

## LESSON

1. Cursor `state.vscdb`는 열지 않는다.

   실측상 `workspaceStorage/<hash>/workspace.json`은 `{"folder": "<uri>"}` 구조이고, `state.vscdb` 내부에는 단일 마지막 활동시각으로 삼기 좋은 필드가 없다. DB를 열면 잠금/손상 리스크만 생기므로 P2에서는 파일 mtime을 `last_activity`로 채택했다.

2. 원격 URI는 로컬 파일 탐색 대상이 아니다.

   Cursor는 `vscode-remote://ssh-remote%2B<host>/...` workspace를 실제로 만든다. 이 경로는 현재 PC의 `PHASE.md` 탐색 대상이 아니므로 `project_path`에는 원문을 보존하고 `join_phase`에서 `no-phase-file`로 명시 강등한다.

3. `no-phase-file`과 `unknown`은 다른 실패다.

   `PHASE.md`가 없으면 프로젝트 규약이 없는 상태이고, 파일은 있는데 파싱이 실패하면 규약 위반 또는 스키마 드리프트다. 두 상태를 섞으면 사용자가 잘못된 조치를 하게 된다.

4. SQLite `CREATE TABLE IF NOT EXISTS`만으로는 마이그레이션이 되지 않는다.

   기존 `.acp/acp.db`에는 새 phase 컬럼이 자동으로 생기지 않는다. `PRAGMA table_info(sessions)`로 컬럼을 확인한 뒤 누락분만 `ALTER TABLE ADD COLUMN`하는 멱등 보강이 필요하다.

5. `plan-stale`은 phase 존재/파싱 flag와 별도 축이어야 한다.

   `phase_flag=ok`인 프로젝트도 세션 활동이 계속되는데 `PHASE.md`가 오래되었거나 현재 phase가 이미 `done`이면 노후 경고가 필요하다. 따라서 `flag`와 `plan_stale`을 분리했다.

## 검증

```text
python -m pytest -q
93 passed in 0.87s

Live smoke:
- rows: 150
- apps: claude,codex,cursor
- canonical session_id rows: 150
- phase_ok: 2
- artifact: .acp/p2-live-smoke-sessions.json
```

## 남은 게이트

별도 Reviewer 재검토는 blocking finding 없음. Nitpicker는 로컬 LLM 기준으로 진행해야 하므로 `jemmin_cli.py --provider ollama --no-daemon` 경로를 사용한다. 현재 Ollama 서비스/모델이 준비되지 않아 실행 blocked라 PASS를 주장하지 않는다. P2는 구현과 live smoke까지 완료됐고, 상태는 `review`로 둔다.
