// 상태 어휘 계약(T14 S4c-2)을 브라우저 없이 node로 실측.
//
// 잠그는 결함:
//   - 클라 리터럴 배열이 어휘·순서·필터 후보를 겸해, 서버와 어긋나면 어휘 밖 상태가
//     **고지 없이 증발**하고 필터로도 고를 수 없었다(도달 불가).
//   - 그 상황에서도 화면이 "정상"이라 단언했다.
//   - 422가 산문뿐이라 소비자가 문장을 파싱해야 했다(R5 위반).
//
// **이 게이트가 증명하지 않는 것**: 실제 DOM 렌더와 CSS 적용(e2e 미설치).
// 여기서 잠그는 것은 표시 문자열·후보 목록·질의 문자열을 만드는 store 함수까지다.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const src = readFileSync(join(root, "acp", "web", "static", "dashboard.js"), "utf8");
const html = readFileSync(
  join(root, "acp", "web", "templates", "dashboard.html"),
  "utf8",
);
const css = readFileSync(join(root, "acp", "web", "static", "style.css"), "utf8");

let currentFetch = async () => {
  throw new Error("fetch stub 미설정");
};
const windowStub = {
  __ACP_POLL_INTERVAL__: 15,
  __ACP_SNAPSHOT_TIMEOUT_MS__: 30,
  setTimeout: (fn) => { fn(); return 0; },
  clearTimeout: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
  location: { pathname: "/", search: "" },
  history: { replaceState() {} },
};
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
  { getElementById: () => null },
  { error() {}, warn() {}, log() {} },
  (...args) => currentFetch(...args),
  function () {},
  URLSearchParams,
);

const VOCAB = ["live", "running", "idle", "holding", "stale", "error", "done", "unknown"];
const ORDER = ["holding", "stale", "error", "live", "running", "idle", "done", "unknown"];

function envelope({ byState = {}, archivedByState = {}, extra = {}, total = null }) {
  const sum = Object.values(byState).reduce((a, b) => a + b, 0);
  const archivedSum = Object.values(archivedByState).reduce((a, b) => a + b, 0);
  return {
    items: [],
    returned: 0,
    total: total === null ? sum + archivedSum : total,
    limit: 300,
    truncated: false,
    summary: {
      total: sum + archivedSum,
      archive_not_observed_total: sum,
      archived_total: archivedSum,
      by_state: byState,
      archived_by_last_known_state: archivedByState,
      by_app: { claude: sum + archivedSum },
      projects: 1,
      action_required: 0,
      action_states: ["holding", "error"],
      cleanup_required: 0,
      cleanup_states: ["stale"],
      state_vocabulary: VOCAB,
      state_display_order: ORDER,
      observed_states: Object.keys({ ...byState, ...archivedByState }).sort(),
      ...extra,
    },
  };
}

function jsonResponse(status, body) {
  return { ok: status < 400, status, json: async () => body };
}

async function run() {
  // ── V3. 리터럴 사본이 소스에 남아 있지 않다 (보조 hygiene 검사) ──
  {
    // 완전한 어휘 배열이 다시 적혀 있으면 그것이 두 번째 사본이다.
    const literalArray = /\[\s*(['"])(holding|live|stale)\1\s*,[\s\S]{0,200}?(['"])unknown\3\s*\]/;
    assert.ok(!literalArray.test(src), "dashboard.js에 상태 리터럴 배열이 남아 있다");
    assert.ok(!literalArray.test(html), "dashboard.html에 상태 리터럴 배열이 남아 있다");
    // 이 검사는 **단일 출처를 증명하지 않는다**(변수·조립 경유는 못 잡는다).
    // 실질 게이트는 아래 V3b다.
  }

  // ── V3b. 서버가 어휘·순서를 바꾸면 화면의 모든 소비 지점이 함께 바뀐다 ──
  {
    const store = dashboardStore();
    store.replaceSessions(envelope({ byState: { live: 1, stale: 2 } }));
    assert.deepEqual(store.displayedStates, ORDER);
    assert.deepEqual(store.availableStates, ORDER);
    assert.deepEqual(Object.keys(store.kpis.counts), ORDER);

    // 서버가 순서를 뒤집고 어휘를 줄이면 — 클라 상수를 보는 소비 지점은 여기서 깨진다.
    const flipped = envelope({ byState: { live: 1 } });
    flipped.summary.state_vocabulary = ["live", "done"];
    flipped.summary.state_display_order = ["done", "live"];
    flipped.summary.observed_states = ["live"];
    const store2 = dashboardStore();
    store2.replaceSessions(flipped);
    assert.deepEqual(store2.displayedStates, ["done", "live"]);
    assert.deepEqual(store2.availableStates, ["done", "live"]);
    assert.deepEqual(Object.keys(store2.kpis.counts), ["done", "live"]);

    // URL 복원도 어휘를 **판정하지 않는다**. 예전 화이트리스트를 되살리면
    // 서버가 아는 상태까지 조용히 버려진다(구현리뷰 R1 P2 — 이 검사가 없었다).
    const restored = dashboardStore();
    windowStub.location.search = "?states=quarantined,live";
    try {
      restored.restoreViewState();
    } finally {
      windowStub.location.search = "";
    }
    assert.deepEqual(restored.selectedStates, ["quarantined", "live"],
      "URL의 어휘 밖 값을 클라가 버렸다");
  }

  // ── V4 · V7 · V9. 미인식 상태는 보이고, 고지되고, "정상"이 아니다 ──
  {
    const store = dashboardStore();
    store.replaceSessions(envelope({ byState: { live: 1, quarantined: 100 } }));

    assert.ok(store.displayedStates.includes("quarantined"), "미인식 상태가 사라졌다");
    assert.equal(store.kpis.counts.quarantined, 100, "미인식 상태가 세어지지 않았다");
    assert.ok(store.availableStates.includes("quarantined"), "필터로 고를 수 없다");
    assert.match(store.unrecognizedStateNote, /quarantined 100건/);
    // 범위가 다른 건수를 합치지 않는다 — 배지는 보관 미관측 분포를 그린다(S4a D7).
    const scoped = dashboardStore();
    scoped.replaceSessions(envelope({ byState: { live: 1 }, archivedByState: { retired: 2 } }));
    assert.match(scoped.unrecognizedStateNote, /retired 보관 2건/);
    const both = dashboardStore();
    both.replaceSessions(envelope({ byState: { retired: 3 }, archivedByState: { retired: 2 } }));
    assert.match(both.unrecognizedStateNote, /retired 3건\(\+보관 2\)/);
    assert.equal(store.kpis.healthLabel, "어휘 확인 필요", "미지 100건을 정상이라 부른다");
    // 배지 클래스: 정의된 하나로 모은다(CSS에 없는 클래스는 투명 배지가 된다).
    assert.equal(store.badgeClass("quarantined"), "badge badge-unrecognized");
    assert.equal(store.badgeClass("live"), "badge badge-live");
    assert.ok(/\.badge-unrecognized\b/.test(css), "badge-unrecognized CSS가 없다");
    // 필터 라벨이 계약 어휘 밖이라는 사실을 말한다.
    assert.match(store.stateFilterLabel("quarantined"), /계약 어휘 밖/);
    assert.equal(store.stateFilterLabel("live"), "live");
  }

  // ── V8 · V8b. 분포 합과 범위 총계 ──
  {
    const ok = dashboardStore();
    ok.replaceSessions(envelope({ byState: { live: 1, quarantined: 100 } }));
    assert.equal(ok.distributionNote, "", "합이 맞는데 경고를 만든다");

    const mismatch = dashboardStore();
    const env = envelope({ byState: { live: 1 } });
    env.summary.archive_not_observed_total = 99;   // 서버 총계와 분포가 어긋난 상황
    mismatch.replaceSessions(env);
    assert.match(mismatch.distributionNote, /1/);
    assert.match(mismatch.distributionNote, /99/);

    // 비교 기준이 없으면 **0으로 간주하지 않는다**(확인 못 함 ≠ 일치함).
    const unknown = dashboardStore();
    const env2 = envelope({ byState: { live: 1 } });
    delete env2.summary.archive_not_observed_total;
    unknown.replaceSessions(env2);
    assert.equal(unknown.distributionNote, "분포 합계 확인 불가");
  }

  // ── V10 · V10b. 어휘 부재 — 폴백 우선순위 ──
  {
    // ① observed_states가 있으면 그것을 쓴다.
    const a = dashboardStore();
    const envA = envelope({ byState: { live: 1 } });
    delete envA.summary.state_vocabulary;
    delete envA.summary.state_display_order;
    envA.summary.observed_states = ["live", "quarantined"];
    a.replaceSessions(envA);
    assert.ok(a.stateVocabularyUnknown, "어휘 미상을 말하지 않는다");
    assert.deepEqual(a.availableStates, ["live", "quarantined"]);
    // 어휘를 모르면 아무 것도 "계약 위반"이라 단정하지 않는다.
    assert.equal(a.unrecognizedStateEntries.length, 0);
    assert.equal(a.badgeClass("quarantined"), "badge badge-quarantined");

    // ①b 순서 배열이 남아 있어도 **소속을 만들지 않는다**(구현리뷰 R4 P1).
    //     표시 순서는 순서일 뿐이다 — 서버가 수용한다고 말한 적 없는 상태를
    //     후보로 올리면 어휘 미상 계약이 거짓이 된다.
    const orderOnly = dashboardStore();
    const envOrder = envelope({ byState: { live: 1 } });
    delete envOrder.summary.state_vocabulary;
    envOrder.summary.observed_states = ["live"];
    orderOnly.replaceSessions(envOrder);
    assert.ok(orderOnly.stateVocabularyUnknown);
    assert.deepEqual(orderOnly.availableStates, ["live"],
      "표시 순서 배열에서 후보를 만들어 냈다");
    assert.deepEqual(Object.keys(orderOnly.kpis.counts), ["live"]);

    // ② observed_states도 없으면 **두 분포의 키 합집합**. `by_state`만 쓰면
    //    보관 행에만 있는 상태가 빠져 도달 불가가 재발한다.
    const b = dashboardStore();
    const envB = envelope({ byState: { live: 1 }, archivedByState: { retired: 5 } });
    delete envB.summary.state_vocabulary;
    delete envB.summary.state_display_order;
    delete envB.summary.observed_states;
    b.replaceSessions(envB);
    assert.ok(b.availableStates.includes("retired"), "보관 행에만 있는 상태가 빠졌다");
    assert.ok(b.availableStates.includes("live"));

    // ③ 둘 다 없으면 빈 목록 — 하드코딩 배열로 되돌아가지 않는다.
    const c = dashboardStore();
    const envC = envelope({});
    delete envC.summary.state_vocabulary;
    delete envC.summary.state_display_order;
    delete envC.summary.observed_states;
    delete envC.summary.by_state;
    delete envC.summary.archived_by_last_known_state;
    c.replaceSessions(envC);
    assert.deepEqual(c.availableStates, []);
    assert.ok(c.stateVocabularyUnknown);
  }

  // ── V11 계열. 422는 구조화 필드로만 소비한다 ──
  {
    const store = dashboardStore();
    store.replaceSessions(envelope({ byState: { live: 1 } }));
    store.selectedStates = ["live", "gone"];

    const requests = [];
    let call = 0;
    currentFetch = async (url) => {
      requests.push(url);
      call += 1;
      if (call === 1) {
        return jsonResponse(422, {
          detail: {
            code: "unknown_state_filter",
            // 산문 문구는 **어디에도 없다** — 있어도 소비하지 않아야 한다.
            invalid_values: ["gone"],
            accepted: VOCAB,
          },
        });
      }
      return jsonResponse(200, envelope({ byState: { live: 1 } }));
    };
    await store.refreshSessions();

    assert.equal(requests.length, 2, "무효 값을 뺀 재조회가 없었다");
    assert.ok(requests[0].includes("states=gone"));
    assert.ok(!requests[1].includes("states=gone"), "거절된 값을 다시 보냈다");
    assert.ok(requests[1].includes("states=live"), "유효 선택까지 잃었다");

    // V11b · V11c: 선택은 남고, 적용만 제외되며, 그 사실을 말한다.
    assert.deepEqual(store.selectedStates, ["live", "gone"], "사용자 선택을 지웠다");
    assert.deepEqual(store.appliedStates, ["live"]);
    assert.ok(store.isRejectedState("gone"));
    assert.match(store.rejectedStateNote, /gone/);
    assert.match(store.stateFilterLabel("gone"), /적용되지 않음/);
    assert.equal(store.fetchError, "", "구조화 거절을 일반 실패로 보고한다");
  }

  // ── V11. `code`가 없거나 모르는 422는 일반 실패다 ──
  {
    // `invalid_values`가 **있어도** `code`가 아니면 상태 어휘 문제가 아니다.
    // 이 케이스가 없으면 `code` 검사를 지워도 테스트가 통과한다(실측으로 확인).
    const notStateFilter = [
      null,
      "알 수 없는 state: ['gone']",                          // 산문 detail
      { code: "other" },
      { code: "unknown_state_filter" },                       // 값이 없다
      { code: "unknown_app_filter", invalid_values: ["gone"] }, // 다른 필터의 거절
      { invalid_values: ["gone"], accepted: VOCAB },           // code 없음
    ];
    for (const detail of notStateFilter) {
      const store = dashboardStore();
      store.replaceSessions(envelope({ byState: { live: 1 } }));
      store.selectedStates = ["gone"];
      let calls = 0;
      currentFetch = async () => {
        calls += 1;
        return jsonResponse(422, { detail });
      };
      await store.refreshSessions();
      assert.equal(calls, 1, `재시도하면 안 된다: ${JSON.stringify(detail)}`);
      assert.equal(store.fetchError, "세션 스냅샷 갱신 실패");
      assert.deepEqual(store.rejectedStates, [], "구조 없는 422로 선택을 건드렸다");
      assert.equal(store.summaryStale, true);
    }
  }

  // ── V11d. 조건이 줄지 않는 422는 **재시도하지 않는다**(무한 재귀·latch 고착 방지) ──
  {
    const store = dashboardStore();
    store.replaceSessions(envelope({ byState: { live: 1 } }));
    store.selectedStates = ["live"];
    let calls = 0;
    currentFetch = async () => {
      calls += 1;
      // 재귀가 풀리지 않으면 **행(hang)** 대신 즉시 실패하게 한다 — 무한 루프는
      // CI에서 타임아웃으로만 드러나 원인을 못 짚는다.
      if (calls > 3) throw new Error("무한 재조회: 조건이 줄지 않는데 계속 재시도한다");
      // 서버/프록시가 **요청에 없던 값**을 계속 거절한다고 주장하는 상황.
      return jsonResponse(422, {
        detail: { code: "unknown_state_filter", invalid_values: ["never-sent"], accepted: VOCAB },
      });
    };
    await store.refreshSessions();
    assert.equal(calls, 1, "조건이 줄지 않는데 재조회했다(무한 재귀 경로)");
    assert.deepEqual(store.rejectedStates, [], "보내지도 않은 값을 거절 목록에 넣었다");
    assert.equal(store.fetchError, "세션 스냅샷 갱신 실패");
    assert.equal(store.snapshotInFlight, false, "단일 비행 latch가 풀리지 않았다");

    // 같은 값이 이미 거절 목록에 있으면 그것도 새 사실이 아니다.
    const repeat = dashboardStore();
    repeat.replaceSessions(envelope({ byState: { live: 1 } }));
    repeat.selectedStates = ["gone"];
    repeat.rejectedStates = ["gone"];
    let repeatCalls = 0;
    currentFetch = async () => {
      repeatCalls += 1;
      if (repeatCalls > 3) throw new Error("무한 재조회: 이미 아는 거절로 계속 재시도한다");
      return jsonResponse(422, {
        detail: { code: "unknown_state_filter", invalid_values: ["gone"], accepted: VOCAB },
      });
    };
    await repeat.refreshSessions();
    assert.equal(repeatCalls, 1, "이미 아는 거절로 재조회했다");
    assert.equal(repeat.snapshotInFlight, false);
  }

  // ── V11e. `actionOnly` 경로도 거절값을 보내지 않는다 ──
  {
    const store = dashboardStore();
    store.replaceSessions(envelope({ byState: { live: 1 } }));
    store.rejectedStates = ["holding"];
    store.actionOnly = true;
    const query = store.serverFilterQuery();
    assert.ok(!query.includes("states=holding"), "적용되지 않음이라 해놓고 다시 보냈다");
    assert.ok(query.includes("states=error"), "유효한 등급 상태까지 빠졌다");
  }

  // ── V11f. 서버가 다시 수용하면 거절 표시를 거둔다 ──
  {
    const store = dashboardStore();
    store.replaceSessions(envelope({ byState: { live: 1 } }));
    store.selectedStates = ["quarantined"];
    store.rejectedStates = ["quarantined"];

    // ① 아직 어휘에도 관측에도 없다 → 거절 유지
    currentFetch = async () => jsonResponse(200, envelope({ byState: { live: 1 } }));
    await store.refreshSessions();
    assert.deepEqual(store.rejectedStates, ["quarantined"]);

    // ② 서버가 그 상태를 관측했다고 말한다 → 표시를 거두고 다시 적용 대상이 된다
    currentFetch = async () =>
      jsonResponse(200, envelope({ byState: { live: 1, quarantined: 2 } }));
    await store.refreshSessions();
    assert.deepEqual(store.rejectedStates, [], "서버가 수용하는데 계속 막고 있다");
    assert.deepEqual(store.appliedStates, ["quarantined"]);

    // ③ 어휘를 **말하지 않는** 응답에서는 거절 사실을 지우지 않는다.
    //    거절값이 `observed_states`에 있어도 마찬가지다 — 관측됐다는 사실은
    //    "그 값으로 거를 수 있다"는 뜻이 아니다(구현리뷰 R3 P1).
    const observedOnly = dashboardStore();
    const envObserved = envelope({ byState: { live: 1, gone: 4 } });
    delete envObserved.summary.state_vocabulary;
    delete envObserved.summary.state_display_order;
    observedOnly.rejectedStates = ["gone"];
    currentFetch = async () => jsonResponse(200, envObserved);
    await observedOnly.refreshSessions();
    assert.ok(observedOnly.observedStates.includes("gone"), "전제: 관측에는 있다");
    assert.deepEqual(observedOnly.rejectedStates, ["gone"],
      "어휘를 모르는데 관측만 보고 거절을 거뒀다");

    //    어휘·관측이 **둘 다 비어도** 마찬가지다.
    const unknown = dashboardStore();
    const env = envelope({ byState: { live: 1 } });
    delete env.summary.state_vocabulary;
    delete env.summary.observed_states;
    delete env.summary.by_state;
    delete env.summary.archived_by_last_known_state;
    unknown.rejectedStates = ["gone"];
    currentFetch = async () => jsonResponse(200, env);
    await unknown.refreshSessions();
    assert.deepEqual(unknown.rejectedStates, ["gone"]);
  }

  // ── V11g. 필터 초기화는 거절 고지도 함께 지운다 ──
  {
    const store = dashboardStore();
    store.replaceSessions(envelope({ byState: { live: 1 } }));
    store.selectedStates = ["gone"];
    store.rejectedStates = ["gone"];
    currentFetch = async () => jsonResponse(200, envelope({ byState: { live: 1 } }));
    store.resetFilters();
    assert.deepEqual(store.rejectedStates, [], "선택을 비웠는데 거절 고지가 남았다");
    assert.equal(store.rejectedStateNote, "");
  }

  // ── V12. 다른 실패는 현행대로 (선택 무변경 · 낡음 유지) ──
  {
    const store = dashboardStore();
    store.replaceSessions(envelope({ byState: { live: 1 } }));
    store.selectedStates = ["live"];
    currentFetch = async () => jsonResponse(500, {});
    await store.refreshSessions();
    assert.equal(store.fetchError, "세션 스냅샷 갱신 실패");
    assert.equal(store.summaryStale, true);
    assert.deepEqual(store.selectedStates, ["live"]);
    assert.deepEqual(store.rejectedStates, []);
  }

  console.log("OK state_vocabulary_store");
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
