# P0 스캐폴드 교훈

날짜: 2026-06-09  
모듈: 전체 P0 스캐폴드

---

## L1 — YAML 세미콜론 분리 포맷 함정

**문제:** PHASE.md의 phases 항목을 `- id: "P0"; title: "..."; status: "planned"` 로 작성 →
PyYAML이 블록 매핑 내 세미콜론을 파싱 오류로 처리.

**해결:** 각 필드를 별도 YAML 줄로 분리:
```yaml
phases:
  - id: "P0"
    title: "..."
    status: "planned"
```

**재현 방지:** `phase.py` 파서의 세미콜론 분리 브랜치는 유지하되, PHASE.md 표준을 YAML 블록 매핑으로 명시.

---

## L2 — Starlette TemplateResponse API 변경

**문제:** 구 Starlette API `TemplateResponse("name.html", {"request": req, ...})` 가
신 Starlette(≥0.36)에서 첫 인자가 `Request`로 바뀌어 `unhashable type: 'dict'` 오류 발생.

**해결:** `TemplateResponse(request, "name.html", {"key": val, ...})` 로 호출 순서 변경.

**재현 방지:** Starlette 버전 고정(pyproject.toml) 또는 신 API 패턴 사용 확인.

---

## L3 — setuptools.backends.legacy 미지원

**문제:** `pyproject.toml`의 `build-backend = "setuptools.backends.legacy:build"` 가
설치된 setuptools 82.0.1 에서 `BackendUnavailable` 오류.

**해결:** `build-backend = "setuptools.build_meta"` 로 교체.

**재현 방지:** 새 프로젝트는 `setuptools.build_meta` 사용.

---

## L4 — SSE 신규 구독자는 과거 이벤트 미수신

**관찰:** 폴러 첫 틱이 SSE 클라이언트 연결보다 먼저 실행되어 초기 state_change 이벤트를 수신 못함.

**현재 대응:** 페이지 로드 시 `/api/sessions` JSON으로 현재 스냅샷을 동기 표시.

**P1+ 대응:** SSE 연결 시점에 현재 세션 스냅샷을 초기 이벤트로 push.
