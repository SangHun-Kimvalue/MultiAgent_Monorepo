// 세션 집계(KPI) 정직성 계약을 브라우저 없이 node로 실측.
//
// 잠그는 결함:
//   - KPI가 잘린 창에서 파생돼 "전체 N"·"정상"을 거짓 단언한 것
//   - SSE 전이 후 재조회 대기 구간에 KPI가 낡은 값을 진실처럼 표시한 것
//   - 늦게 도착한 오래된 스냅샷 응답이 최신 응답을 덮어쓴 것
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const jsPath = join(here, "..", "..", "acp", "web", "static", "dashboard.js");
const src = readFileSync(jsPath, "utf8");

let currentFetch = async () => {
  throw new Error("fetch stub 미설정");
};
const fetchImpl = (...args) => currentFetch(...args);
const timers = new Map();
let timerSeq = 0;
const windowStub = {
  __ACP_POLL_INTERVAL__: 15,
  // 실제 중단 경로를 짧은 시간에 검증하기 위한 주입값.
  __ACP_SNAPSHOT_TIMEOUT_MS__: 30,
  setTimeout(fn) {
    const id = ++timerSeq;
    timers.set(id, fn);
    return id;
  },
  clearTimeout(id) {
    timers.delete(id);
  },
  setInterval: () => 0,
  clearInterval: () => {},
};
const documentStub = { getElementById: () => null };
const consoleStub = { error() {}, warn() {}, log() {} };

const factory = new Function(
  "window",
  "document",
  "console",
  "fetch",
  "EventSource",
  "URLSearchParams",
  src + "\nreturn dashboardStore;",
);
const dashboardStore = factory(
  windowStub,
  documentStub,
  consoleStub,
  fetchImpl,
  function () {},
  URLSearchParams,
);

function envelope({
  items = [],
  returned = 0,
  total = 0,
  byState = {},
  byApp = {},
  projects = 0,
  action = 0,
  cleanup = 0,
  actionStates = ["holding", "error"],
  cleanupStates = ["stale"],
}) {
  // 실서버는 등급 목록을 **항상** 함께 내려준다. 헬퍼가 이를 빼면 replaceSessions가
  // 메타를 교체하면서 등급이 사라져, 테스트가 실계약과 다른 상태를 검증하게 된다.
  return {
    items,
    returned,
    total,
    limit: 300,
    truncated: returned < total,
    summary: {
      total,
      by_state: byState,
      by_app: byApp,
      projects,
      action_required: action,
      action_states: actionStates,
      cleanup_required: cleanup,
      cleanup_states: cleanupStates,
    },
  };
}

function jsonResponse(body) {
  return { ok: true, status: 200, json: async () => body };
}

function runPendingTimers() {
  const pending = [...timers.entries()];
  timers.clear();
  for (const [, fn] of pending) fn();
}

async function run() {
  // ── 1. KPI는 창 길이가 아니라 서버 총계를 쓴다 ──
  {
    const store = dashboardStore();
    store.replaceSessions(
      envelope({
        items: [{ session_id: "a", app: "claude", state: "live", project_path: "/p" }],
        returned: 1,
        total: 1009,
        byState: { live: 4, stale: 604, unknown: 362, holding: 38, error: 1 },
        byApp: { claude: 356, codex: 636, cursor: 11, fake: 6 },
        projects: 73,
        action: 643,
      }),
    );
    assert.equal(store.sessionList.length, 1, "창은 1건");
    assert.equal(store.kpis.total, 1009, "총계는 서버 값(창 길이 아님)");
    assert.equal(store.kpis.apps, 4);
    assert.equal(store.kpis.projects, 73);
    assert.equal(store.kpis.actionRequired, 643);
    assert.equal(store.kpis.counts.stale, 604);
    assert.equal(store.kpis.healthLabel, "확인 필요");
    assert.equal(store.sessionMeta.truncated, true);
    assert.match(store.truncationNote, /1009/);
  }

  // ── 2. 행동필요 0이고 갱신도 정상이면 "정상" ──
  {
    const store = dashboardStore();
    store.replaceSessions(envelope({ returned: 0, total: 0, byState: {}, byApp: {}, action: 0 }));
    assert.equal(store.kpis.healthLabel, "정상");
  }

  // ── 3. SSE 전이 직후 즉시 stale — 재조회 대기 구간에 "정상"이라 하지 않는다 ──
  {
    const store = dashboardStore();
    store.replaceSessions(
      envelope({
        items: [{ session_id: "a", app: "claude", state: "live", project_path: "/p" }],
        returned: 1,
        total: 1,
        byState: { live: 1 },
        byApp: { claude: 1 },
        projects: 1,
        action: 0,
      }),
    );
    assert.equal(store.kpis.healthLabel, "정상");

    store.applyStateChange({ session_id: "a", app: "claude", project_path: "/p", state: "error" });
    assert.equal(store.sessions["a"].state, "error", "행은 즉시 바뀐다");
    assert.equal(store.summaryStale, true, "집계는 그 즉시 낡았다고 표시한다");
    assert.notEqual(store.kpis.healthLabel, "정상", "대기 구간에 '정상' 단언 금지");
    assert.equal(store.kpis.healthLabel, "집계 갱신 중");

    // debounce 만료 → 권위 스냅샷 재조회 성공 → stale 해제
    currentFetch = async () => jsonResponse(
      envelope({ returned: 1, total: 1, byState: { error: 1 }, byApp: { claude: 1 }, projects: 1, action: 1 }),
    );
    runPendingTimers();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(store.summaryStale, false, "성공 응답에서 해제");
    assert.equal(store.kpis.actionRequired, 1);
    assert.equal(store.kpis.healthLabel, "확인 필요");
  }

  // ── 4. 재조회 실패는 "정상"으로 위장하지 않는다 ──
  {
    const store = dashboardStore();
    store.replaceSessions(envelope({ returned: 0, total: 0, byState: {}, byApp: {}, action: 0 }));
    currentFetch = async () => {
      throw new Error("network down");
    };
    await store.refreshSessions();
    assert.equal(store.summaryStale, true);
    assert.equal(store.kpis.healthLabel, "집계 갱신 실패");
    assert.equal(store.fetchError, "세션 스냅샷 갱신 실패");
  }

  // ── 5. 늦게 도착한 오래된 응답이 최신 응답을 덮어쓰지 않는다 ──
  {
    const store = dashboardStore();
    let release;
    const slow = new Promise((resolve) => {
      release = resolve;
    });
    // 1차: 느린 응답(총계 100)
    currentFetch = async () => {
      await slow;
      return jsonResponse(envelope({ returned: 0, total: 100, byState: {}, byApp: {}, action: 0 }));
    };
    const first = store.refreshSessions();
    // 2차 요청은 단일 비행 계약상 **큐잉**된다(병렬로 쏘지 않는다).
    currentFetch = async () => jsonResponse(
      envelope({ returned: 0, total: 999, byState: {}, byApp: {}, action: 0 }),
    );
    await store.refreshSessions();
    assert.equal(store.snapshotQueued, true, "진행 중이면 새로 쏘지 않고 예약한다");

    release();
    await first;
    await new Promise((resolve) => setTimeout(resolve, 20));
    // 큐에 있던 최신 요청이 뒤이어 실행돼 최종 상태는 최신 값이어야 한다.
    assert.equal(store.kpis.total, 999, "낡은 응답이 최종 상태로 남으면 안 된다");
  }

  // ── 6. 전이 이전에 출발한 응답은 stale을 풀지 못한다 ──
  //    순서: 요청 시작 → SSE 전이 → (그 이전 데이터인) 응답 도착.
  //    이 응답은 전이를 반영할 수 없으므로 "정상"으로 되돌리면 거짓 안심이 된다.
  {
    const store = dashboardStore();
    store.replaceSessions(
      envelope({
        items: [{ session_id: "a", app: "claude", state: "live", project_path: "/p" }],
        returned: 1,
        total: 1,
        byState: { live: 1 },
        byApp: { claude: 1 },
        projects: 1,
        action: 0,
      }),
    );
    let release;
    const inflight = new Promise((resolve) => {
      release = resolve;
    });
    currentFetch = async () => {
      await inflight;
      // 전이 이전 시점의 데이터 — action 0, 모두 live
      return jsonResponse(
        envelope({ returned: 1, total: 1, byState: { live: 1 }, byApp: { claude: 1 }, projects: 1, action: 0 }),
      );
    };
    const pending = store.refreshSessions();

    store.applyStateChange({ session_id: "a", app: "claude", project_path: "/p", state: "error" });
    assert.equal(store.summaryStale, true);

    release();
    await pending;
    assert.equal(store.summaryStale, true, "전이 이전 응답은 stale을 풀 수 없다");
    assert.notEqual(store.kpis.healthLabel, "정상");
  }

  // ── 7. 상태/앱 필터는 서버 쿼리로 나간다 (창 안에서만 거르지 않는다) ──
  {
    const store = dashboardStore();
    store.replaceSessions(
      envelope({
        returned: 0,
        total: 100,
        byState: { live: 1, stale: 99 },
        byApp: { claude: 100 },
        action: 99,
      }),
    );
    store.sessionMeta.summary.action_states = ["holding", "stale", "error"];

    assert.equal(store.serverFilterQuery(), "", "필터 없으면 쿼리도 없다");

    store.selectedStates = ["error"];
    assert.equal(store.serverFilterQuery(), "?states=error");

    store.selectedStates = [];
    store.selectedApps = ["cursor"];
    assert.equal(store.serverFilterQuery(), "?apps=cursor");

    store.selectedApps = [];
    store.actionOnly = true;
    const q = store.serverFilterQuery();
    assert.ok(q.includes("states=stale") && q.includes("states=holding") && q.includes("states=error"),
      "행동 필요만 = 서버 action_states 전체를 조건으로 보낸다");

    // 필터 옵션은 창이 아니라 전량 집계에서 나온다.
    // 상태 순서는 **서버가 말할 때만** 그 순서를 따른다(T14 S4c-2 D1). 이 봉투에는
    // `state_display_order`가 없으므로 클라가 순서를 지어내지 않고 관측 집합을 쓴다 —
    // 예전에는 하드코딩 배열이 순서를 정해 `["stale","live"]`였다.
    assert.deepEqual(store.availableApps, ["claude"]);
    assert.deepEqual(store.availableStates, ["live", "stale"]);
  }

  // ── 8. SSE로 들어온 창 밖 세션이 다음 스냅샷에서 사라지지 않는다 ──
  {
    const store = dashboardStore();
    store.replaceSessions(
      envelope({
        items: [{ session_id: "in-window", app: "claude", state: "live", project_path: "/p" }],
        returned: 1,
        total: 2,
        byState: { live: 1, stale: 1 },
        byApp: { claude: 2 },
        projects: 1,
        action: 1,
      }),
    );
    // 창 밖 세션이 stale로 전이 — 정의상 last_activity가 오래돼 창에 다시 안 잡힌다
    store.applyStateChange({ session_id: "far-away", app: "codex", project_path: "/q", state: "stale" });
    assert.equal(store.sessionList.length, 2);

    // 같은(창 밖을 포함하지 않는) 스냅샷이 다시 와도 유지돼야 한다
    store.replaceSessions(
      envelope({
        items: [{ session_id: "in-window", app: "claude", state: "live", project_path: "/p" }],
        returned: 1,
        total: 2,
        byState: { live: 1, stale: 1 },
        byApp: { claude: 2 },
        projects: 1,
        action: 1,
      }),
    );
    assert.ok(store.sessions["far-away"], "조치가 필요한 전이가 조용히 사라지면 안 된다");
    assert.equal(store.sessions["far-away"].state, "stale");
  }

  // ── 9. 고지가 출처를 뭉뚱그리지 않는다 (서버 창 vs 창 밖 보존) ──
  {
    const store = dashboardStore();
    store.replaceSessions(
      envelope({
        items: [{ session_id: "a", app: "claude", state: "live", project_path: "/p" }],
        returned: 1,
        total: 50,
        byState: { live: 50 },
        byApp: { claude: 50 },
        projects: 1,
        action: 0,
      }),
    );
    store.sessionMeta.matched = 50;
    store.sessionMeta.summary.action_states = ["holding", "stale", "error"];
    assert.match(store.truncationNote, /최근 활동순 상위 1건 표시 · 전체 50건/);

    // 창 밖 전이가 표에 남으면 표시 행 수는 늘지만, 그걸 "상위 2건"이라 부르면 거짓이다.
    // 서버 창(1건)과 보존분(1건)을 **분리해** 고지해야 한다.
    store.applyStateChange({ session_id: "b", app: "codex", project_path: "/q", state: "holding" });
    store.replaceSessions(
      envelope({
        items: [{ session_id: "a", app: "claude", state: "live", project_path: "/p" }],
        returned: 1,
        total: 50,
        byState: { live: 49, holding: 1 },
        byApp: { claude: 50 },
        projects: 1,
        action: 1,
      }),
    );
    store.sessionMeta.matched = 50;
    assert.equal(store.sessionList.length, 2, "보존분이 표에 남는다");
    assert.match(store.truncationNote, /최근 활동순 상위 1건 표시/, "서버 창은 1건 그대로");
    assert.match(store.truncationNote, /창 밖 전이 1건 유지/, "보존분은 따로 고지");
    assert.ok(!/상위 2건/.test(store.truncationNote), "출처를 뭉뚱그리지 않는다");
  }

  // ── 10. 요청이 완료보다 빨리 발생해도 마지막 결과가 반드시 반영된다 ──
  //    세대 가드만 있으면 모든 응답이 직후 요청에 폐기돼 화면이 굳는다(실측 결함).
  {
    const store = dashboardStore();
    let inFlight = 0;
    let maxInFlight = 0;
    let calls = 0;
    currentFetch = async () => {
      calls += 1;
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await new Promise((resolve) => setTimeout(resolve, 5));
      inFlight -= 1;
      return jsonResponse(
        envelope({ returned: 0, total: 42, byState: {}, byApp: {}, action: 0 }),
      );
    };

    // 응답이 오기 전에 연달아 5번 요청
    const bursts = [
      store.refreshSessions(),
      store.refreshSessions(),
      store.refreshSessions(),
      store.refreshSessions(),
      store.refreshSessions(),
    ];
    await Promise.all(bursts);
    // 큐에 남은 후속 요청까지 끝나도록 잠깐 대기
    await new Promise((resolve) => setTimeout(resolve, 30));

    assert.equal(maxInFlight, 1, "동시 요청은 1건으로 묶인다");
    assert.ok(calls <= 2, `버스트가 요청 폭주로 번지지 않는다 (calls=${calls})`);
    assert.equal(store.kpis.total, 42, "마지막 결과가 반드시 반영된다");
    assert.equal(store.summaryStale, false);
  }

  // ── 11. 창 밖 보존에는 종료 계약이 있다 (무한 누적 금지) ──
  {
    const snapshot = () =>
      envelope({
        items: [{ session_id: "in-window", app: "claude", state: "live", project_path: "/p" }],
        returned: 1,
        total: 2,
        byState: { live: 1, stale: 1 },
        byApp: { claude: 2 },
        projects: 1,
        action: 1,
      });

    // (a) 조치가 불필요한 상태로 전이하면 더는 붙잡지 않는다
    {
      const store = dashboardStore();
      store.replaceSessions(snapshot());
      store.sessionMeta.summary.action_states = ["holding", "stale", "error"];
      store.applyStateChange({ session_id: "far", app: "codex", project_path: "/q", state: "stale" });
      store.replaceSessions(snapshot());
      assert.ok(store.sessions["far"], "조치 대상이면 보존");

      store.applyStateChange({ session_id: "far", app: "codex", project_path: "/q", state: "live" });
      store.replaceSessions(snapshot());
      assert.equal(store.sessions["far"], undefined, "조치 불필요로 바뀌면 보존 종료");
    }

    // (b) 서로 다른 세션이 계속 쌓여도 상한을 넘지 않는다
    {
      const store = dashboardStore();
      store.replaceSessions(snapshot());
      store.sessionMeta.summary.action_states = ["holding", "stale", "error"];
      for (let i = 0; i < 120; i += 1) {
        store.applyStateChange({ session_id: `far-${i}`, app: "codex", project_path: "/q", state: "stale" });
      }
      store.replaceSessions(snapshot());
      const retained = Object.values(store.sessions).filter((s) => s.outside_window);
      assert.equal(retained.length, 50, "보존은 상한까지만");
      assert.ok(store.sessions["far-119"], "최신 전이가 남는다");
      assert.equal(store.sessions["far-0"], undefined, "오래된 전이는 밀려난다");
      // 상한에 밀려 뺀 건도 조용히 사라지면 안 된다 — 고지해야 한다.
      assert.equal(store.retainedShown, 50);
      assert.equal(store.retainedDropped, 70);
      assert.match(store.truncationNote, /비활성 70건 미표시/);  // stale = cleanup 등급
      assert.match(store.truncationNote, /창 밖 전이 50건 유지/);
    }

    // (c) 스냅샷이 다시 그 세션을 주면 서버 값이 이기고 보존 플래그도 사라진다
    {
      const store = dashboardStore();
      store.replaceSessions(snapshot());
      store.sessionMeta.summary.action_states = ["holding", "stale", "error"];
      store.applyStateChange({ session_id: "far", app: "codex", project_path: "/q", state: "stale" });

      const withFar = snapshot();
      withFar.items.push({ session_id: "far", app: "codex", state: "holding", project_path: "/q" });
      withFar.returned = 2;
      store.replaceSessions(withFar);

      assert.equal(store.sessions["far"].state, "holding", "서버 값이 이긴다");
      assert.ok(!store.sessions["far"].outside_window, "창 안이면 보존 대상이 아니다");
    }
  }

  // ── 12. 스냅샷 재조회가 실패하는 동안에도 보존 예산이 강제된다 ──
  {
    const store = dashboardStore();
    store.replaceSessions(
      envelope({ returned: 0, total: 0, byState: {}, byApp: {}, action: 0 }),
    );
    store.sessionMeta.summary.action_states = ["holding", "stale", "error"];
    currentFetch = async () => {
      throw new Error("network down");
    };
    for (let i = 0; i < 80; i += 1) {
      store.applyStateChange({ session_id: `x-${i}`, app: "codex", project_path: "/q", state: "stale" });
    }
    await store.refreshSessions(); // 실패

    const held = Object.values(store.sessions).filter((s) => s.outside_window);
    assert.equal(held.length, 50, "재조회 실패 중에도 상한이 걸린다");
    assert.equal(store.retainedDropped, 30, "탈락 건수도 그 즉시 갱신된다");
    assert.match(store.truncationNote, /비활성 30건 미표시/);  // stale = cleanup 등급
    assert.equal(store.summaryStale, true);
  }

  // ── 13. 표 분모는 "이 질의로 보유한 행" — 조건 밖 보존분을 세지 않는다 ──
  {
    const store = dashboardStore();
    store.replaceSessions(
      envelope({
        items: [{ session_id: "e1", app: "claude", state: "error", project_path: "/p" }],
        returned: 1,
        total: 100,
        byState: { error: 1, stale: 99 },
        byApp: { claude: 100 },
        projects: 1,
        action: 100,
      }),
    );
    store.sessionMeta.summary.action_states = ["holding", "stale", "error"];
    store.selectedStates = ["error"];
    for (let i = 0; i < 3; i += 1) {
      store.applyStateChange({ session_id: `s-${i}`, app: "codex", project_path: "/q", state: "stale" });
    }

    assert.equal(store.sessionList.length, 4, "보존분까지 4행을 들고 있다");
    assert.equal(store.visibleCount, 1, "조건에 맞는 렌더 행은 1건");
    assert.equal(store.heldForQuery, 1, "분모도 1 — '1 / 4' 같은 무의미한 수를 만들지 않는다");
  }

  // ── 14. settle 하지 않는 요청도 **실제로 중단**돼 latch가 풀린다 ──
  //    (스텁이 TimeoutError를 직접 던지면 동어반복이므로, 응답을 영원히 안 주는
  //     fetch를 두고 AbortSignal이 실제로 끊는지를 본다.)
  {
    const store = dashboardStore();
    let sawSignal = false;
    currentFetch = (_url, options) =>
      new Promise((_resolve, reject) => {
        sawSignal = Boolean(options && options.signal);
        // 응답을 영원히 주지 않는다 — 오직 signal만이 이 요청을 끝낼 수 있다.
        options.signal.addEventListener("abort", () => reject(options.signal.reason));
      });
    // `AbortSignal.timeout()`의 타이머는 이벤트 루프를 붙잡지 않는다(unref).
    // 대기 중 프로세스가 먼저 종료되지 않도록 keep-alive를 둔다.
    const keepAlive = setTimeout(() => {}, 5000);
    await store.refreshSessions();
    clearTimeout(keepAlive);

    assert.ok(sawSignal, "요청에 중단 신호가 붙어 있어야 한다");
    assert.equal(store.snapshotInFlight, false, "실패해도 latch가 풀린다");
    assert.equal(store.fetchError, "세션 스냅샷 갱신 시간 초과");

    // latch가 풀렸으므로 다음 요청이 정상 진행된다
    currentFetch = async () => jsonResponse(
      envelope({ returned: 0, total: 7, byState: {}, byApp: {}, action: 0 }),
    );
    await store.refreshSessions();
    assert.equal(store.kpis.total, 7, "타임아웃 뒤에도 갱신이 재개된다");
    assert.equal(store.summaryStale, false);
  }

  // ── 15. 보존 예산은 action 우선 (T14 S2 D6) ──
  //    cleanup(stale)이 칸을 다 채워도, 즉시 확인할 action은 반드시 남아야 한다.
  {
    const snapshot = () =>
      envelope({ returned: 0, total: 1, byState: {}, byApp: {}, action: 0 });
    const store = dashboardStore();
    store.replaceSessions(snapshot());
    store.sessionMeta.summary.action_states = ["holding", "error"];
    store.sessionMeta.summary.cleanup_states = ["stale"];

    // 비활성으로 예산(50)을 가득 채운다
    for (let i = 0; i < 60; i += 1) {
      store.applyStateChange({ session_id: `st-${i}`, app: "codex", project_path: "/q", state: "stale" });
    }
    store.replaceSessions(snapshot());
    assert.equal(store.retainedShown, 50, "예산이 가득 찼다");

    // 그 뒤에 도착한 action 전이가 밀려나면 안 된다
    store.applyStateChange({ session_id: "hold-1", app: "codex", project_path: "/q", state: "holding" });
    store.replaceSessions(snapshot());

    assert.ok(store.sessions["hold-1"], "action은 cleanup보다 먼저 보존된다");
    assert.equal(store.sessions["hold-1"].state, "holding");
    assert.equal(store.retainedShown, 50, "상한은 유지된다");
    // 고지는 등급을 뭉개지 않는다
    assert.equal(store.droppedAction, 0);
    assert.ok(store.droppedCleanup > 0);
    assert.match(store.truncationNote, /비활성 \d+건 미표시/);
    assert.ok(!/확인 필요 \d+건 미표시/.test(store.truncationNote));
  }

  // ── 16. 탈락 원장은 등급 간 상호 배타 (T14 S2 D6) ──
  {
    const snapshot = () => envelope({ returned: 0, total: 1, byState: {}, byApp: {}, action: 0 });
    const store = dashboardStore();
    store.replaceSessions(snapshot());
    store.sessionMeta.summary.action_states = ["holding", "error"];
    store.sessionMeta.summary.cleanup_states = ["stale"];

    for (let i = 0; i < 55; i += 1) {
      store.applyStateChange({ session_id: `h-${i}`, app: "codex", project_path: "/q", state: "holding" });
    }
    store.replaceSessions(snapshot());
    assert.equal(store.droppedAction, 5, "action 5건이 상한에 밀렸다");

    // 밀려난 세션이 cleanup 등급으로 전이하면 원장도 옮겨간다(양쪽 잔류 금지)
    store.applyStateChange({ session_id: "h-0", app: "codex", project_path: "/q", state: "stale" });
    store.replaceSessions(snapshot());
    assert.equal(
      store.droppedAction + store.droppedCleanup,
      store.retainedDropped,
      "합계 == 고유 탈락 수",
    );
  }

  // ── 17. 탈락한 세션이 등급 밖으로 전이하면 고지에서 즉시 빠진다 ──
  //    (탈락 HOLDING → LIVE인데 "확인 필요 N건 미표시"가 남으면 새 거짓이다.)
  {
    const snapshot = () => envelope({ returned: 0, total: 1, byState: {}, byApp: {}, action: 0 });
    const store = dashboardStore();
    store.replaceSessions(snapshot());

    for (let i = 0; i < 55; i += 1) {
      store.applyStateChange({ session_id: `h-${i}`, app: "codex", project_path: "/q", state: "holding" });
    }
    store.replaceSessions(snapshot());
    assert.equal(store.droppedAction, 5, "5건이 상한에 밀렸다");

    // 밀려난 세션이 회복(LIVE)되면 더는 가시성 손실이 아니다.
    for (const id of ["h-0", "h-1", "h-2"]) {
      store.applyStateChange({ session_id: id, app: "codex", project_path: "/q", state: "live" });
    }
    store.replaceSessions(snapshot());

    assert.equal(store.droppedAction, 2, "회복된 건은 고지에서 빠진다");
    assert.equal(store.droppedCleanup, 0);
    assert.equal(
      store.droppedAction + store.droppedCleanup,
      store.retainedDropped,
      "합계 == 고유 탈락 수",
    );
  }

  // ── 보관 범위 고지 (T14 S4a D8) ──
  // 의미가 바뀌는 수(상태 분포)와 그 설명은 같은 화면에 있어야 한다. API 봉투가
  // 정직해도 소비자가 안 읽으면 사용자에게는 설명 없는 숫자 변화다.
  {
    const store = dashboardStore();
    const meta = envelope({ returned: 3, total: 6, byState: { live: 1, error: 1, stale: 1 } });
    meta.summary.archived_total = 3;
    meta.summary.state_distribution_scope = "archive_not_observed";
    meta.summary.archived_by_last_known_state = { stale: 2, running: 1 };
    store.replaceSessions(meta);

    assert.equal(store.kpis.total, 6, "전체는 저장 전량");
    assert.equal(store.kpis.archivedTotal, 3);
    assert.ok(store.hasArchivedScopeNote, "두 수가 다른 이유를 화면이 말해야 한다");
    assert.match(store.archivedScopeNote, /3/, "제외된 건수를 고지");
    assert.equal(store.stateScopeLabel, "상태 분포: 보관 제외");
    assert.deepEqual(
      store.archivedLastKnownEntries,
      [["stale", 2], ["running", 1]],
      "보관 분포는 도달 가능해야 한다(큰 순)",
    );
  }

  {
    // 보관이 0이면 고지하지 않는다 — 무소식=정상 관례를 깨지 않는다.
    const store = dashboardStore();
    const meta = envelope({ returned: 1, total: 1, byState: { live: 1 } });
    meta.summary.archived_total = 0;
    meta.summary.state_distribution_scope = "archive_not_observed";
    store.replaceSessions(meta);
    assert.equal(store.hasArchivedScopeNote, false);
    assert.deepEqual(store.archivedLastKnownEntries, []);
  }

  {
    // 모르는 범위를 "현재"·"정상"으로 조용히 축약하지 않는다(미확인을 아는 척 금지).
    const store = dashboardStore();
    const meta = envelope({ returned: 1, total: 1, byState: { live: 1 } });
    meta.summary.state_distribution_scope = "some_future_scope";
    store.replaceSessions(meta);
    assert.match(store.stateScopeLabel, /범위 미상/);
    assert.match(store.stateScopeLabel, /some_future_scope/, "원문을 감추지 않는다");

    // 서버가 범위를 말하지 않으면 라벨도 만들지 않는다(추측 금지).
    const silent = dashboardStore();
    silent.replaceSessions(envelope({ returned: 1, total: 1, byState: { live: 1 } }));
    assert.equal(silent.stateScopeLabel, "");
  }

  // ── T14 S5. 비활성은 경고 등급을 만들지 않는다 ──
  {
    // 활동이 멈춘 세션이 쌓이는 것은 정상이다. 그것으로 상단 라벨을 물들이면
    // "정상"이라 말할 수 있는 상태가 사실상 사라진다(실측: action 11 / 비활성 853).
    const store = dashboardStore();
    const meta = envelope({ returned: 1, total: 900, byState: { stale: 899, live: 1 }, action: 0 });
    meta.summary.inactive_total = 899;
    meta.summary.inactive_states = ["stale"];
    meta.summary.state_vocabulary = ["live", "stale"];
    store.replaceSessions(meta);
    assert.equal(store.kpis.inactiveTotal, 899, "비활성 수는 계속 보인다");
    assert.equal(store.kpis.healthLabel, "정상", "비활성이 경고 등급을 만들었다");

    // 조치가 있으면 여전히 확인 필요가 이긴다(S2 등급 분리는 유지).
    const acting = dashboardStore();
    const meta2 = envelope({ returned: 1, total: 900, byState: { stale: 899, holding: 1 }, action: 1 });
    meta2.summary.inactive_total = 899;
    meta2.summary.inactive_states = ["stale"];
    acting.replaceSessions(meta2);
    assert.equal(acting.kpis.healthLabel, "확인 필요");

    // 옛 이름만 주는 응답도 읽는다(옛 서버·캐시 호환).
    const legacy = dashboardStore();
    const meta3 = envelope({ returned: 1, total: 10, byState: { stale: 9 }, cleanup: 9 });
    legacy.replaceSessions(meta3);
    assert.equal(legacy.kpis.inactiveTotal, 9);
  }

  console.log("OK session_summary_store");
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
