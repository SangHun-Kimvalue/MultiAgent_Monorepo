// Phase 2 구동 패널 JS store 단위 테스트 (브라우저 없이 node로 실측).
// dashboard.js의 dashboardStore()를 최소 전역 스텁 위에서 로드해 run/approve
// 메서드 존재와 상태 전이(awaiting_gate↔done, 409/422/network error)를 검증한다.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const jsPath = join(here, "..", "..", "acp", "web", "static", "dashboard.js");
const src = readFileSync(jsPath, "utf8");

// run/approve 경로만 실측하므로 브라우저 전역은 최소만 채운다.
let currentFetch = async () => {
  throw new Error("fetch stub 미설정");
};
const fetchImpl = (...args) => currentFetch(...args);
const windowStub = { __ACP_POLL_INTERVAL__: 15 };
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

function jsonResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

async function run() {
  const store = dashboardStore();

  // 메서드/상태 존재
  assert.equal(typeof store.startOrchRun, "function", "startOrchRun 존재");
  assert.equal(typeof store.approveOrchRun, "function", "approveOrchRun 존재");
  assert.ok(store.orchRun, "orchRun 상태 존재");
  assert.equal(store.orchRun.status, "idle", "초기 상태 idle");
  assert.equal(store.canApproveOrchRun, false, "idle에서 승인 비활성");

  // Run → awaiting_gate
  let lastReq = null;
  currentFetch = async (url, opts) => {
    lastReq = { url, opts };
    return jsonResponse(200, {
      run_id: "orch-run-abc123",
      project_id: "T2",
      phase_id: "P1",
      status: "awaiting_gate",
      resume_token: "orch-run-abc123:gate-1",
      message: "awaiting human approval",
    });
  };
  store.orchInput.prompt = "drive phase one";
  store.orchInput.projectId = "T2";
  store.orchInput.phaseId = "P1";
  await store.startOrchRun();
  assert.equal(lastReq.url, "/api/orch/run", "run POST 경로");
  assert.equal(lastReq.opts.method, "POST");
  const sent = JSON.parse(lastReq.opts.body);
  assert.deepEqual(
    sent,
    { prompt: "drive phase one", project_id: "T2", phase_id: "P1" },
    "선택 입력이 채워지면 payload에 포함",
  );
  assert.equal(store.orchRun.runId, "orch-run-abc123");
  assert.equal(store.orchRun.status, "awaiting_gate");
  assert.equal(store.orchRun.resumeToken, "orch-run-abc123:gate-1");
  assert.equal(store.canApproveOrchRun, true, "awaiting_gate에서만 승인 활성");
  assert.equal(store.orchRun.error, "", "성공 시 error 비움");

  // 네트워크 실패 → error 표면화 (awaiting_gate에서 승인 시도, status는 유지)
  currentFetch = async () => {
    throw new Error("network down");
  };
  await store.approveOrchRun();
  assert.match(store.orchRun.error, /네트워크 실패/, "네트워크 실패 표시");
  assert.equal(store.orchRun.status, "awaiting_gate", "네트워크 실패 후에도 status 유지");

  // Approve → done
  currentFetch = async (url, opts) => {
    lastReq = { url, opts };
    return jsonResponse(200, {
      run_id: "orch-run-abc123",
      project_id: "T2",
      phase_id: "P1",
      status: "done",
      resume_token: null,
      message: "approved by human gate",
    });
  };
  await store.approveOrchRun();
  assert.equal(lastReq.url, "/api/orch/runs/orch-run-abc123/approve", "approve POST 경로");
  assert.equal(lastReq.opts.method, "POST");
  assert.equal(store.orchRun.status, "done");
  assert.equal(store.orchRun.resumeToken, "", "done에서 resume_token 비움");
  assert.equal(store.canApproveOrchRun, false, "done에서 승인 비활성");

  // AD-7 방어: done(비-awaiting_gate)에서 approveOrchRun은 fetch를 보내지 않는다.
  let approveCalls = 0;
  currentFetch = async () => {
    approveCalls += 1;
    return jsonResponse(200, { run_id: "x", status: "done" });
  };
  await store.approveOrchRun();
  assert.equal(approveCalls, 0, "done에서 approve writer가 POST를 보내지 않음");
  assert.equal(store.orchRun.status, "done", "done 유지");

  // 빈 선택 입력은 payload에서 생략 (서버 기본값 위임)
  currentFetch = async (url, opts) => {
    lastReq = { url, opts };
    return jsonResponse(200, {
      run_id: "orch-run-def456",
      project_id: "MAM",
      phase_id: "orch-run-def456",
      status: "awaiting_gate",
      resume_token: "orch-run-def456:gate-1",
      message: "awaiting human approval",
    });
  };
  store.orchInput.projectId = "  ";
  store.orchInput.phaseId = "";
  await store.startOrchRun();
  assert.deepEqual(JSON.parse(lastReq.opts.body), { prompt: "drive phase one" }, "빈 선택 입력 생략");

  // 409 active run → error 표면화 (silent ignore 금지)
  currentFetch = async () => jsonResponse(409, { detail: "an orchestrator run is already active" });
  await store.startOrchRun();
  assert.match(store.orchRun.error, /409/, "409가 error로 표시");
  assert.match(store.orchRun.error, /already active/);

  // 422 빈 prompt 등 검증 실패 → error 표면화 (FastAPI detail은 list)
  currentFetch = async () => jsonResponse(422, { detail: [{ msg: "string too short" }] });
  await store.startOrchRun();
  assert.match(store.orchRun.error, /422/, "422가 error로 표시");

  console.log("OK orch_run_store: 모든 단언 통과");
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
