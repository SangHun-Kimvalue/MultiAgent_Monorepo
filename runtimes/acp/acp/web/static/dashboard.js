const ACP_STATES = ["holding", "stale", "error", "live", "running", "idle", "done", "unknown"];
const ACP_ACTION_STATES = new Set(["holding", "stale", "error"]);
const ACP_SEVERITY_ORDER = {
  error: 0,
  stale: 1,
  holding: 2,
  idle: 3,
  running: 4,
  live: 5,
  done: 6,
  unknown: 7,
};

function readJsonScript(id, fallback) {
  const node = document.getElementById(id);
  if (!node) return fallback;
  try {
    return JSON.parse(node.textContent || "");
  } catch (error) {
    console.error(`[ACP] ${id} JSON parse failed`, error);
    return fallback;
  }
}

function normalizeSession(session) {
  const state = String(session.state || "unknown").toLowerCase();
  return {
    ...session,
    state,
    session_id: session.session_id || `${session.app || "unknown"}:${session.native_session_id || "unknown"}`,
    native_session_id: session.native_session_id || session.session_id || "",
    app: session.app || "unknown",
    project_path: session.project_path || "",
  };
}

function dashboardStore() {
  return {
    sessions: {},
    notifications: [],
    orchEvents: [],
    orchFailures: [],
    streamStatus: "연결 중...",
    streamConnected: false,
    fetchError: "",
    selectedApps: [],
    selectedStates: [],
    selectedProjects: [],
    actionOnly: false,
    sortKey: "last_activity",
    sortDir: "desc",
    pollIntervalMs: Math.max(Number(window.__ACP_POLL_INTERVAL__ || 15), 1) * 1000,
    snapshotTimer: null,
    eventSource: null,
    // Phase 2 구동 패널 — 브라우저 메모리 한정. 새 서버 영속 상태를 만들지 않는다.
    orchInput: { prompt: "", projectId: "", phaseId: "" },
    orchRun: { runId: "", status: "idle", resumeToken: "", message: "", error: "", busy: false },

    init() {
      this.replaceSessions(readJsonScript("initial-sessions", []));
      this.notifications = readJsonScript("initial-notifications", []);
      this.restoreViewState();
      this.loadOrchEvents();
      this.connectStream();
      this.snapshotTimer = window.setInterval(() => this.refreshSessions(), this.pollIntervalMs);
      window.__acpDashboard = this;
      window.__acpDashboardReady = true;
    },

    destroy() {
      if (this.snapshotTimer) window.clearInterval(this.snapshotTimer);
      if (this.eventSource) this.eventSource.close();
    },

    get sessionList() {
      return Object.values(this.sessions).sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
    },

    get availableApps() {
      return Array.from(new Set(this.sessionList.map((session) => session.app || "unknown"))).sort();
    },

    get availableProjects() {
      return Array.from(new Set(this.sessionList.map((session) => session.project_path || "no-project"))).sort();
    },

    get filteredSorted() {
      const sessions = this.sessionList.filter((session) => {
        const app = session.app || "unknown";
        const state = session.state || "unknown";
        const project = session.project_path || "no-project";
        if (this.actionOnly && !this.isActionState(state)) return false;
        if (this.selectedApps.length > 0 && !this.selectedApps.includes(app)) return false;
        if (this.selectedStates.length > 0 && !this.selectedStates.includes(state)) return false;
        if (this.selectedProjects.length > 0 && !this.selectedProjects.includes(project)) return false;
        return true;
      });
      return sessions.sort((a, b) => this.compareSessions(a, b));
    },

    get visibleGroups() {
      const groups = new Map();
      for (const session of this.filteredSorted) {
        const app = session.app || "unknown";
        const project = session.project_path || "no-project";
        const key = `${app}\u0000${project}`;
        if (!groups.has(key)) {
          groups.set(key, { key, app, project_path: project, sessions: [] });
        }
        groups.get(key).sessions.push(session);
      }
      return Array.from(groups.values()).sort((a, b) => {
        if (this.sortKey === "app") return this.applySortDir(a.app.localeCompare(b.app) || a.project_path.localeCompare(b.project_path));
        if (this.sortKey === "project_path") return this.applySortDir(a.project_path.localeCompare(b.project_path) || a.app.localeCompare(b.app));
        return a.app.localeCompare(b.app) || a.project_path.localeCompare(b.project_path);
      });
    },

    get visibleCount() {
      return this.filteredSorted.length;
    },

    get groupedSessions() {
      const groups = new Map();
      for (const session of this.sessionList) {
        const app = session.app || "unknown";
        const project = session.project_path || "no-project";
        const key = `${app}\u0000${project}`;
        if (!groups.has(key)) {
          groups.set(key, { key, app, project_path: project, sessions: [] });
        }
        groups.get(key).sessions.push(session);
      }
      return Array.from(groups.values()).sort((a, b) => {
        const byApp = a.app.localeCompare(b.app);
        return byApp || a.project_path.localeCompare(b.project_path);
      });
    },

    get kpis() {
      const counts = Object.fromEntries(ACP_STATES.map((state) => [state, 0]));
      const apps = new Set();
      const projects = new Set();
      for (const session of this.sessionList) {
        const state = ACP_STATES.includes(session.state) ? session.state : "unknown";
        counts[state] += 1;
        if (session.app) apps.add(session.app);
        projects.add(session.project_path || "no-project");
      }
      const actionRequired = counts.holding + counts.stale + counts.error;
      return {
        total: this.sessionList.length,
        apps: apps.size,
        projects: projects.size,
        actionRequired,
        counts,
        healthLabel: actionRequired > 0 ? "확인 필요" : "정상",
      };
    },

    replaceSessions(rows) {
      const next = {};
      for (const row of rows || []) {
        const session = normalizeSession(row);
        next[session.session_id] = session;
      }
      this.sessions = next;
    },

    compareSessions(a, b) {
      if (this.sortKey === "state") {
        return this.applySortDir((ACP_SEVERITY_ORDER[a.state] ?? 99) - (ACP_SEVERITY_ORDER[b.state] ?? 99));
      }
      if (this.sortKey === "app") {
        return this.applySortDir(String(a.app || "").localeCompare(String(b.app || "")) || this.compareActivity(a, b));
      }
      if (this.sortKey === "project_path") {
        return this.applySortDir(String(a.project_path || "").localeCompare(String(b.project_path || "")) || this.compareActivity(a, b));
      }
      return this.applySortDir(this.compareActivity(a, b));
    },

    compareActivity(a, b) {
      return String(a.last_activity || a.updated_at || "").localeCompare(String(b.last_activity || b.updated_at || ""));
    },

    applySortDir(value) {
      return this.sortDir === "asc" ? value : -value;
    },

    setSort(key) {
      if (this.sortKey === key) {
        this.sortDir = this.sortDir === "asc" ? "desc" : "asc";
      } else {
        this.sortKey = key;
        this.sortDir = ["state", "app", "project_path"].includes(key) ? "asc" : "desc";
      }
      this.persistViewState();
    },

    sortLabel(key) {
      if (this.sortKey !== key) return "";
      return this.sortDir === "asc" ? " ^" : " v";
    },

    toggleFilter(listName, value) {
      const current = this[listName];
      if (current.includes(value)) {
        this[listName] = current.filter((item) => item !== value);
      } else {
        this[listName] = [...current, value];
      }
      this.persistViewState();
    },

    resetFilters() {
      this.selectedApps = [];
      this.selectedStates = [];
      this.selectedProjects = [];
      this.actionOnly = false;
      this.persistViewState();
    },

    updateActionOnly(value) {
      this.actionOnly = Boolean(value);
      this.persistViewState();
    },

    persistViewState() {
      const params = new URLSearchParams();
      if (this.selectedApps.length) params.set("apps", this.selectedApps.join(","));
      if (this.selectedStates.length) params.set("states", this.selectedStates.join(","));
      if (this.selectedProjects.length) params.set("projects", this.selectedProjects.join("|"));
      if (this.actionOnly) params.set("action", "1");
      if (this.sortKey !== "last_activity") params.set("sort", this.sortKey);
      if (this.sortDir !== "desc") params.set("dir", this.sortDir);
      const query = params.toString();
      const next = query ? `${window.location.pathname}?${query}` : window.location.pathname;
      window.history.replaceState(null, "", next);
    },

    restoreViewState() {
      const params = new URLSearchParams(window.location.search);
      this.selectedApps = this.parseParamList(params.get("apps"), ",");
      this.selectedStates = this.parseParamList(params.get("states"), ",").filter((state) => ACP_STATES.includes(state));
      this.selectedProjects = this.parseParamList(params.get("projects"), "|");
      this.actionOnly = params.get("action") === "1";
      const sort = params.get("sort");
      if (sort === "updated_at") this.sortKey = "last_activity";
      if (["state", "last_activity", "app", "project_path"].includes(sort)) this.sortKey = sort;
      const dir = params.get("dir");
      if (["asc", "desc"].includes(dir)) this.sortDir = dir;
    },

    parseParamList(value, separator) {
      if (!value) return [];
      return value.split(separator).map((item) => item.trim()).filter(Boolean);
    },

    async refreshSessions() {
      try {
        const response = await fetch("/api/sessions?limit=300", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        this.replaceSessions(await response.json());
        this.fetchError = "";
      } catch (error) {
        this.fetchError = "세션 스냅샷 갱신 실패";
        console.error("[ACP] session snapshot refresh failed", error);
      }
    },

    connectStream() {
      this.eventSource = new EventSource("/api/live/stream");
      this.eventSource.onopen = () => {
        this.streamStatus = "연결됨";
        this.streamConnected = true;
      };
      this.eventSource.addEventListener("state_change", (event) => {
        this.applyStateChange(JSON.parse(event.data));
      });
      this.eventSource.addEventListener("notification", (event) => {
        this.applyNotification(JSON.parse(event.data));
      });
      this.eventSource.addEventListener("orch_event", (event) => {
        this.applyOrchEvent(JSON.parse(event.data));
      });
      this.eventSource.addEventListener("orch_event_failure", (event) => {
        this.applyOrchFailure(JSON.parse(event.data));
      });
      this.eventSource.onerror = (error) => {
        this.streamStatus = "연결 끊김 (재시도 중)";
        this.streamConnected = false;
        console.error("[ACP] SSE disconnected", error);
      };
    },

    applyStateChange(event) {
      const key = event.session_id;
      const current = this.sessions[key] || {
        session_id: key,
        native_session_id: event.native_session_id || key,
        app: event.app || "unknown",
        project_path: event.project_path || "",
      };
      this.sessions = {
        ...this.sessions,
        [key]: normalizeSession({
          ...current,
          native_session_id: event.native_session_id || current.native_session_id,
          app: event.app || current.app,
          project_path: event.project_path ?? current.project_path,
          state: event.state,
          updated_at: new Date().toISOString(),
        }),
      };
    },

    applyNotification(event) {
      this.notifications = [{ ...event, created_at: new Date().toISOString() }, ...this.notifications].slice(0, 10);
    },

    async loadOrchEvents() {
      try {
        const response = await fetch("/api/orch-events?limit=50", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        this.orchEvents = await response.json();
      } catch (error) {
        console.error("[ACP] orch events load failed", error);
      }
    },

    applyOrchEvent(event) {
      // SSE는 event_type, /api 행은 type — 공통 shape로 정규화
      const normalized = {
        project_id: event.project_id,
        phase_id: event.phase_id,
        type: event.event_type || event.type,
        ts: event.ts,
        payload: event.payload,
      };
      this.orchEvents = [normalized, ...this.orchEvents].slice(0, 50);
    },

    applyOrchFailure(event) {
      this.orchFailures = [{ ...event, seen_at: new Date().toISOString() }, ...this.orchFailures].slice(0, 20);
    },

    // ── Phase 2 구동 패널: run/approve (사람 클릭만 writer, AD-7) ──

    get canApproveOrchRun() {
      // 승인은 awaiting_gate에서만. running/done/blocked/idle에서는 비활성(자동 next 금지).
      return this.orchRun.status === "awaiting_gate" && !this.orchRun.busy;
    },

    async startOrchRun() {
      if (this.orchRun.busy) return;
      this.orchRun.busy = true;
      this.orchRun.error = "";
      try {
        const payload = { prompt: this.orchInput.prompt };
        const project = (this.orchInput.projectId || "").trim();
        const phase = (this.orchInput.phaseId || "").trim();
        if (project) payload.project_id = project;
        if (phase) payload.phase_id = phase;
        const response = await fetch("/api/orch/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!response.ok) {
          // 409(active run)/422(검증 실패)를 silent ignore 하지 않고 화면에 남긴다.
          this.orchRun.error = await this.orchErrorText(response);
          return;
        }
        this.applyOrchRunState(await response.json());
      } catch (error) {
        this.orchRun.error = "네트워크 실패: 구동 요청을 보내지 못했습니다";
        console.error("[ACP] orch run start failed", error);
      } finally {
        this.orchRun.busy = false;
      }
    },

    async approveOrchRun() {
      // AD-7 방어: writer 자신을 버튼과 같은 predicate로 막는다. awaiting_gate가 아니면
      // (done/blocked/running/idle) /approve POST 자체를 보내지 않는다. 버튼 disabled에만
      // 의존하지 않는다.
      if (!this.orchRun.runId || !this.canApproveOrchRun) return;
      this.orchRun.busy = true;
      this.orchRun.error = "";
      try {
        const response = await fetch(`/api/orch/runs/${encodeURIComponent(this.orchRun.runId)}/approve`, {
          method: "POST",
        });
        if (!response.ok) {
          this.orchRun.error = await this.orchErrorText(response);
          return;
        }
        this.applyOrchRunState(await response.json());
      } catch (error) {
        this.orchRun.error = "네트워크 실패: 승인 요청을 보내지 못했습니다";
        console.error("[ACP] orch run approve failed", error);
      } finally {
        this.orchRun.busy = false;
      }
    },

    applyOrchRunState(state) {
      // R5: 서버가 준 status 문자열/필드만 반영한다. prose 의미판정/자동편집 금지.
      this.orchRun.runId = state.run_id || "";
      this.orchRun.status = state.status || "unknown";
      this.orchRun.resumeToken = state.resume_token || "";
      this.orchRun.message = state.message || "";
    },

    async orchErrorText(response) {
      let detail = "";
      try {
        const body = await response.json();
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      } catch (error) {
        detail = "";
      }
      return `HTTP ${response.status}${detail ? " — " + detail : ""}`;
    },

    orchRunStatusBadge(status) {
      const s = String(status || "idle");
      if (s === "done") return "badge badge-done";
      if (s === "awaiting_gate") return "badge badge-holding";
      if (s === "running") return "badge badge-running";
      if (s === "blocked") return "badge badge-error";
      return "badge badge-idle";
    },

    orchTypeBadge(type) {
      const t = String(type || "");
      if (t === "phase.verdict") return "badge badge-phase-ok";
      if (t === "gate.waiting") return "badge badge-holding";
      if (t === "leg.result") return "badge badge-live";
      if (t === "phase.started") return "badge badge-running";
      return "badge badge-phase-unknown";
    },

    orchEventKey(ev) {
      // store dedup과 동일하게 payload까지 포함 — 같은 phase/type/ts 다른 payload 충돌 방지
      let payload = "";
      try {
        payload = JSON.stringify(ev.payload || {});
      } catch (error) {
        payload = "";
      }
      return `${ev.ts || ""}|${ev.phase_id || ""}|${ev.type || ""}|${payload}`;
    },

    orchPayloadText(payload) {
      if (!payload || typeof payload !== "object") return "-";
      try {
        return JSON.stringify(payload);
      } catch (error) {
        return "-";
      }
    },

    badgeClass(state) {
      return `badge badge-${String(state || "unknown").toLowerCase()}`;
    },

    phaseBadgeClass(session) {
      if (session.plan_stale) return "badge badge-phase-stale";
      const flag = String(session.phase_flag || "no-phase-file");
      if (flag === "ok") return "badge badge-phase-ok";
      if (flag === "no-phase-file") return "badge badge-phase-missing";
      return "badge badge-phase-unknown";
    },

    phaseLabel(session) {
      if (session.plan_stale) return "plan-stale";
      if (session.phase_flag && session.phase_flag !== "ok") return session.phase_flag;
      return session.current_phase || "no-phase-file";
    },

    progressText(session) {
      const done = Number(session.phases_done || 0);
      const total = Number(session.phases_total || 0);
      return total > 0 ? `${done}/${total}` : "-";
    },

    shortId(session) {
      const value = session.native_session_id || session.session_id || "";
      return value.length > 24 ? `${value.slice(0, 24)}...` : value;
    },

    shortText(value, length = 40) {
      const text = value || "-";
      return text.length > length ? text.slice(0, length) : text;
    },

    rowId(session) {
      return `row-${String(session.session_id).replace(/[\\/]/g, "-")}`;
    },

    isActionState(state) {
      return ACP_ACTION_STATES.has(String(state || "").toLowerCase());
    },
  };
}
