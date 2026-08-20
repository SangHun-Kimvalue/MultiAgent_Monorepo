// 실행 증거 표시 계약(T14 S4c-1)을 브라우저 없이 node로 실측.
//
// 잠그는 결함:
//   - "안 돌고 있음"과 "신호를 얻을 수 없음"을 똑같이 `-`로 그린 것
//   - 내부 마커 `process-resume`를 명령어인 양(그리고 `title` 원문으로) 노출한 것
//   - UI가 sentinel을 직접 해석해, 마커가 늘 때마다 같은 결함이 반복되던 것
//
// **이 게이트가 증명하지 않는 것**: Alpine 템플릿이 실제로 그리는 DOM 텍스트와
// `title` 속성. 여기서 잠그는 것은 표시 문자열을 만드는 store 함수까지다(e2e 미설치).
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const jsPath = join(here, "..", "..", "acp", "web", "static", "dashboard.js");
const src = readFileSync(jsPath, "utf8");

const windowStub = {
  __ACP_POLL_INTERVAL__: 15,
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
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
  async () => {
    throw new Error("fetch stub 미설정");
  },
  function () {},
  URLSearchParams,
);

// 서버 응답 모양 그대로. health를 빼면 실계약과 다른 상태를 검증하게 된다.
function storeWith(health, items = []) {
  const store = dashboardStore();
  store.replaceSessions({
    items,
    returned: items.length,
    total: items.length,
    limit: 300,
    truncated: false,
    summary: {
      total: items.length,
      by_state: {},
      by_app: {},
      projects: 0,
      action_required: 0,
      action_states: ["holding", "error"],
      cleanup_required: 0,
      cleanup_states: ["stale"],
      collector_health: health,
    },
  });
  return store;
}

const SUPPORTED_OK = { process_signal_capability: "supported", process_signal: "ok" };
const SUPPORTED_LOST = {
  process_signal_capability: "supported",
  process_signal: "unavailable",
};
const UNSUPPORTED = {
  process_signal_capability: "unsupported",
  process_signal: "not_applicable",
};
const UNDECLARED = { process_signal_capability: "unknown", process_signal: "unknown" };

async function run() {
  // ── V7. 네 값이 서로 다른 문자열이다 ──
  {
    const store = storeWith({
      a: SUPPORTED_OK,
      b: SUPPORTED_OK,
      c: UNSUPPORTED,
      d: SUPPORTED_LOST,
      e: UNDECLARED,
    });
    const running = store.executionPidText({ app: "a", running_pid: 4242 });
    const notRunning = store.executionPidText({ app: "b", running_pid: null });
    const cannot = store.executionPidText({ app: "c", running_pid: null });
    const lost = store.executionPidText({ app: "d", running_pid: null });
    const undeclared = store.executionPidText({ app: "e", running_pid: null });

    assert.equal(running, "4242");
    assert.equal(notRunning, "-", "관측했는데 값이 없다 = 안 돌고 있음");
    assert.equal(cannot, "신호 없음", "구조적으로 줄 수 없다");
    assert.equal(lost, "확인 불가", "이번 사이클에 못 얻었다");
    assert.equal(undeclared, "확인 불가", "선언이 없다");
    // 넷이 서로 달라야 화면이 사실을 구분한다. 둘을 합치면 여기서 실패한다.
    assert.equal(new Set([running, notRunning, cannot, lost]).size, 4);
  }

  // ── V7b. 앱 이름이 아니라 필드로 분기한다(R5) ──
  {
    // 두 이름만 비교하면 "그 두 이름을 하드코딩하지 않았다"만 증명된다(구현리뷰 R1 P2).
    // 실서버에 등장하는 이름 전부 + 처음 보는 이름까지 넣어, **필드가 같으면 결과도 같다**를
    // 이름 집합 전체에 대해 확인한다.
    const names = ["claude", "codex", "cursor", "fake", "probe", "브랜뉴앱"];
    for (const health of [SUPPORTED_OK, UNSUPPORTED, SUPPORTED_LOST, UNDECLARED]) {
      const store = storeWith(Object.fromEntries(names.map((n) => [n, health])));
      const results = new Set(
        names.map((n) => store.executionPidText({ app: n, running_pid: null })),
      );
      assert.equal(
        results.size,
        1,
        `필드가 같은데 이름에 따라 결과가 갈렸다: ${[...results].join(" / ")}`,
      );
      const cmdResults = new Set(
        names.map((n) => store.executionCmdText({ app: n, execution_evidence_kind: "none" })),
      );
      assert.equal(cmdResults.size, 1);
    }

    // 필드만 다르면 결과가 다르다(반대 방향 — 아무 값이나 같게 만드는 구현을 배제).
    const flipped = storeWith({ cursor: UNSUPPORTED, claude: SUPPORTED_OK });
    assert.notEqual(
      flipped.executionPidText({ app: "cursor", running_pid: null }),
      flipped.executionPidText({ app: "claude", running_pid: null }),
    );
  }

  // ── V5b · V6. UI는 enum만 소비하고, 마커 원문은 어디에도 넣지 않는다 ──
  {
    const store = storeWith({ claude: SUPPORTED_OK });
    // `running_cmd`를 **주지 않아도** 올바른 문자열이 나온다 = sentinel 직접 비교가 아니다.
    const enumOnly = { app: "claude", execution_evidence_kind: "process_recheck" };
    assert.equal(store.executionCmdText(enumOnly), "실행 확인됨");
    assert.equal(store.executionCmdTitle(enumOnly), "", "title에도 원문 없음");

    // 원문이 함께 와도 화면 문자열·title 어디에도 새지 않는다.
    const withRaw = {
      app: "claude",
      execution_evidence_kind: "process_recheck",
      running_cmd: "process-resume",
    };
    assert.ok(!/process-resume/.test(store.executionCmdText(withRaw)));
    assert.ok(!/process-resume/.test(store.executionCmdTitle(withRaw)));

    // 실제 명령어는 그대로 보여주고 title에 전문을 준다(기존 동작).
    const cmd = {
      app: "claude",
      execution_evidence_kind: "command",
      running_cmd: "python -m acp web",
    };
    assert.equal(store.executionCmdText(cmd), "python -m acp web");
    assert.equal(store.executionCmdTitle(cmd), "python -m acp web");
  }

  // ── V8. 계약 밖 값을 "정상"으로 축약하지 않는다 ──
  {
    const store = storeWith({
      x: { process_signal_capability: "probably", process_signal: "fine" },
    });
    assert.equal(store.executionPidText({ app: "x", running_pid: null }), "확인 불가");
    assert.equal(
      store.executionCmdText({ app: "x", execution_evidence_kind: "brand_new_kind" }),
      "확인 불가",
      "미지 enum을 명령으로 승격하지 않는다",
    );
  }

  // ── V8b. 옛 응답(필드 부재)에서도 닫힌다 ──
  {
    // health 자체가 없는 응답(옛 서버)
    const store = storeWith(undefined);
    assert.equal(store.executionPidText({ app: "claude", running_pid: null }), "확인 불가");
    // 필드만 빠진 health
    const partial = storeWith({ claude: { status: "success_complete" } });
    assert.equal(partial.executionPidText({ app: "claude", running_pid: null }), "확인 불가");
    // evidence kind 필드가 아예 없는 세션 행
    assert.equal(
      partial.executionCmdText({ app: "claude", running_cmd: "python -m acp web" }),
      "확인 불가",
      "kind가 없으면 running_cmd를 명령으로 읽지 않는다",
    );
  }

  // ── 방어: command라 했는데 값이 없으면 지어내지 않는다 ──
  {
    const store = storeWith({ claude: SUPPORTED_OK });
    assert.equal(
      store.executionCmdText({ app: "claude", execution_evidence_kind: "command" }),
      "확인 불가",
    );
  }

  console.log("OK execution_evidence_store");
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
