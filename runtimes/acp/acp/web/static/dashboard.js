// 상태 어휘는 **서버가 소유한다**(T14 S4c-2 D1). 클라 리터럴 배열은 제거했다 —
// 그 배열이 어휘·표시 순서·필터 후보를 겸하는 바람에, 서버가 아는 것과 어긋나는 순간
// 어휘 밖 상태가 KPI에서 증발하고(고지 없이) 필터로도 고를 수 없었다(실측).
// 서버가 어휘를 말하지 않으면 **지어내지 않고** `어휘 미상`으로 표시한다.
const ACP_UNRECOGNIZED_BADGE = "badge-unrecognized";
const ACP_ACTION_STATES = new Set(["holding", "stale", "error"]);
// SSE 전이 폭주 시 권위 스냅샷 재조회를 합치는 창.
const SUMMARY_REFRESH_DEBOUNCE_MS = 750;
// 서버 창 밖인데 SSE로 알게 된 세션을 몇 건까지 남길지(무한 누적 방지).
const RETAINED_OUTSIDE_WINDOW_MAX = 50;
// 스냅샷 요청 상한 — 이걸 넘기면 요청을 끊어 단일 비행 latch를 반드시 해제한다.
// 테스트가 실제 중단 경로를 짧은 시간에 검증할 수 있도록 주입 가능하게 둔다.
const SNAPSHOT_TIMEOUT_MS = Number(window.__ACP_SNAPSHOT_TIMEOUT_MS__) || 10000;
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
    // 서버가 내려준 창·총계 메타. KPI의 권위 소스이며 클라가 세지 않는다.
    sessionMeta: { returned: 0, total: 0, limit: 0, truncated: false, summary: {} },
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
    summaryRefreshTimer: null,
    // 스냅샷 응답 세대 — 늦게 도착한 오래된 응답을 버리는 데 쓴다.
    snapshotGeneration: 0,
    // 단일 비행 제어 — 동시 요청이 서로를 폐기해 아무것도 반영 안 되는 것을 막는다.
    snapshotInFlight: false,
    snapshotQueued: false,
    // SSE 전이 순번 — 창 밖 보존 상한을 넘을 때 최신 것을 남기는 기준.
    sseSequence: 0,
    // 창 밖 전이 중 표에 남긴 수 / 상한에 밀려 뺀 수(고지용).
    retainedShown: 0,
    retainedDropped: 0,
    droppedAction: 0,
    droppedCleanup: 0,
    // 상한에 밀려 표에 못 올린 세션 id(고지용 누적). 등급별로 분리하고 상호 배타를
    // 유지한다. 스냅샷에 다시 등장하거나 보존에 재진입하면 양쪽에서 해제된다.
    _droppedActionIds: new Set(),
    _droppedCleanupIds: new Set(),
    // 집계 갱신이 실패해 KPI가 현재 사실이 아닐 수 있는 상태.
    summaryStale: false,
    // 서버가 **구조화 오류로** 거절한 상태 필터 값(T14 S4c-2 D5). 선택에서 지우지
    // 않는다 — 사용자가 건 조건을 지우는 것은 의도를 지우는 것이다. 다음 질의에서만
    // 빼고, 화면은 그 값이 왜 빠졌는지 계속 말한다.
    rejectedStates: [],
    eventSource: null,
    // Phase 2 구동 패널 — 브라우저 메모리 한정. 새 서버 영속 상태를 만들지 않는다.
    orchInput: { prompt: "", projectId: "", phaseId: "" },
    orchRun: { runId: "", status: "idle", resumeToken: "", message: "", error: "", busy: false },

    init() {
      this.replaceSessions(readJsonScript("initial-sessions", []));
      this.sessionMeta = readJsonScript("initial-session-meta", this.sessionMeta);
      this.notifications = readJsonScript("initial-notifications", []);
      this.restoreViewState();
      // 서버가 내려준 초기 창은 **무필터**다. URL로 필터가 복원된 진입(새로고침·북마크·
      // 링크 공유)에서 첫 폴링(기본 15초)까지 기다리면, 그 구간 동안 화면은 무필터 창을
      // 클라로만 걸러 "KPI는 N인데 표는 없음"이 그대로 재현된다(리뷰 P2).
      // 진입 즉시 권위 조회를 한 번 돌려 그 창을 없앤다.
      this.refreshSessions();
      this.loadOrchEvents();
      this.connectStream();
      this.snapshotTimer = window.setInterval(() => this.refreshSessions(), this.pollIntervalMs);
      window.__acpDashboard = this;
      window.__acpDashboardReady = true;
    },

    destroy() {
      if (this.snapshotTimer) window.clearInterval(this.snapshotTimer);
      if (this.summaryRefreshTimer) window.clearTimeout(this.summaryRefreshTimer);
      if (this.eventSource) this.eventSource.close();
    },

    get sessionList() {
      // 정렬키는 활동 시각이다. `updated_at`(폴러 write 시각)으로 정렬하면
      // SSE로만 들어온 세션처럼 그 값이 없는 행이 항상 끝으로 밀린다.
      return Object.values(this.sessions).sort((a, b) =>
        String(b.last_activity || b.updated_at || "").localeCompare(
          String(a.last_activity || a.updated_at || ""),
        ),
      );
    },

    get actionStates() {
      // 서버 응답이 권위. 클라가 따로 정의하면 드리프트 시 KPI와 필터가
      // 다른 집합을 가리킨다(리뷰 P2).
      const fromServer = this.sessionMeta.summary?.action_states;
      return new Set(fromServer && fromServer.length ? fromServer : ACP_ACTION_STATES);
    },

    get inactiveStates() {
      // 비활성 축(T14 S2 D4 · S5에서 개명). 알림 축은 아니지만 **조회·보존 대상에는
      // 포함**한다 —
      // 빼면 그 세션들이 서버 필터로도 닿지 않고 화면에서 사라진다.
      // 새 이름을 우선 쓰고 옛 이름으로 폴백한다(옛 서버·캐시된 응답 호환).
      const summary = this.sessionMeta.summary || {};
      return new Set(summary.inactive_states || summary.cleanup_states || []);
    },

    get retainableStates() {
      return new Set([...this.actionStates, ...this.inactiveStates]);
    },

    get availableApps() {
      // 옵션은 창이 아니라 **전량 집계**에서 뽑는다. 창에만 있는 앱을 나열하면
      // KPI가 세는 앱을 필터로 고를 수조차 없다(리뷰 P1).
      const fromSummary = Object.keys(this.sessionMeta.summary?.by_app || {});
      if (fromSummary.length) return fromSummary.sort();
      return Array.from(new Set(this.sessionList.map((session) => session.app || "unknown"))).sort();
    },

    // ── 상태 어휘 (T14 S4c-2) ──
    // 세 축을 분리한다: **아는 것**(계약 어휘) / **보여줄 순서**(제품 정책) /
    // **실제로 있는 것**(데이터). 예전에는 클라 배열 하나가 셋을 겸했다.

    get stateVocabulary() {
      const value = (this.sessionMeta.summary || {}).state_vocabulary;
      return Array.isArray(value) ? value.map(String) : [];
    },

    get stateDisplayOrder() {
      const value = (this.sessionMeta.summary || {}).state_display_order;
      return Array.isArray(value) ? value.map(String) : [];
    },

    // 저장소에 실제로 있는 상태. 서버가 말해주면 그것을 쓰고, 없으면 **두 분포의 키
    // 합집합**으로 내려간다 — `by_state`만 쓰면 보관 행에만 있는 상태가 빠져서
    // "조회하면 나오는데 고를 수 없는" 상태가 다시 생긴다(설계 D4·D2a).
    get observedStates() {
      const summary = this.sessionMeta.summary || {};
      const declared = summary.observed_states;
      if (Array.isArray(declared)) return declared.map(String);
      const merged = new Set([
        ...Object.keys(summary.by_state || {}),
        ...Object.keys(summary.archived_by_last_known_state || {}),
      ]);
      return Array.from(merged).sort();
    },

    // 어휘를 모르는가. 그러면 화면이 "모른다"고 말해야 한다(추측 금지).
    get stateVocabularyUnknown() {
      return this.stateVocabulary.length === 0;
    },

    isRecognizedState(state) {
      // 어휘를 모를 때는 **아무 것도 미인식이라 단정하지 않는다**(모름 ≠ 계약 위반).
      if (this.stateVocabularyUnknown) return true;
      return this.stateVocabulary.includes(String(state));
    },

    // 표시 대상 = **계약 어휘 ∪ 관측된 상태**. 표시 순서는 **순서일 뿐** 소속을 만들지
    // 않는다(구현리뷰 R4 P1) — 순서 배열에서 소속을 유도하면 서버가 수용한다고 말한 적
    // 없는 상태가 필터 후보로 올라오고, "어휘 미상일 때 추정 폴백 없음"이 거짓이 된다.
    get displayedStates() {
      const members = new Set([
        ...(this.stateVocabularyUnknown ? [] : this.stateVocabulary),
        ...this.observedStates,
      ]);
      const ordered = [];
      const take = (state) => {
        const value = String(state);
        if (members.delete(value)) ordered.push(value);
      };
      for (const state of this.stateDisplayOrder) take(state);   // 순서만 빌려 쓴다
      for (const state of this.stateVocabulary) take(state);
      for (const state of this.observedStates) take(state);
      return ordered;
    },

    get availableStates() {
      // 필터 후보 = 표시 대상 전체. 미인식 상태도 **고를 수 있어야** 한다 —
      // 건수만 보이고 행에 닿을 수 없으면 그것은 도달 가능성이 아니다(설계 D2).
      return this.displayedStates;
    },

    // 계약 어휘 밖인데 실제로 존재하는 상태 + 건수. 침묵은 "없음"으로 읽힌다.
    // 건수는 **범위별로** 나눠 든다: 배지는 보관 미관측 분포를 그리는데(S4a D7)
    // 고지가 둘을 합치면 "배지는 0인데 고지는 2건"이 되어 같은 화면이 두 말을 한다.
    get unrecognizedStateEntries() {
      if (this.stateVocabularyUnknown) return [];
      const summary = this.sessionMeta.summary || {};
      const byState = summary.by_state || {};
      const archived = summary.archived_by_last_known_state || {};
      return this.observedStates
        .filter((state) => !this.isRecognizedState(state))
        .map((state) => [state, Number(byState[state] || 0), Number(archived[state] || 0)]);
    },

    get unrecognizedStateNote() {
      const entries = this.unrecognizedStateEntries;
      if (!entries.length) return "";
      const detail = entries
        .map(([state, live, archived]) => {
          if (live && archived) return `${state} ${live}건(+보관 ${archived})`;
          if (archived) return `${state} 보관 ${archived}건`;
          return `${state} ${live}건`;
        })
        .join(" · ");
      return `계약 어휘 밖 상태: ${detail}`;
    },

    get availableProjects() {
      return Array.from(new Set(this.sessionList.map((session) => session.project_path || "no-project"))).sort();
    },

    get filteredSorted() {
      const sessions = this.sessionList.filter((session) => {
        const app = session.app || "unknown";
        const state = session.state || "unknown";
        const project = session.project_path || "no-project";
        if (this.actionOnly && !this.actionStates.has(state)) return false;
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
      // KPI는 표시 창이 아니라 **서버가 전량 기준으로 센 값**을 쓴다.
      // 잘린 배열에서 세면 "전체 100"·"정상" 같은 거짓 단언이 나온다.
      const summary = this.sessionMeta.summary || {};
      const byState = summary.by_state || {};
      // 표시 대상 **전체**를 센다. 클라 배열로 제한하면 어휘 밖 상태가 고지 없이
      // 사라지고, 배지 합과 총계가 어긋난 채 화면이 침묵한다(실측한 결함).
      const counts = Object.fromEntries(
        this.displayedStates.map((state) => [state, byState[state] || 0])
      );
      const actionRequired = summary.action_required || 0;
      const inactiveTotal = summary.inactive_total ?? summary.cleanup_required ?? 0;
      return {
        total: this.sessionMeta.total || 0,
        apps: Object.keys(summary.by_app || {}).length,
        projects: summary.projects || 0,
        actionRequired,
        inactiveTotal,
        counts,
        archivedTotal: summary.archived_total || 0,
        // 집계가 낡았을 수 있으면 "정상"이라 단언하지 않는다(미확인 ≠ 이상 없음).
        // 무효화 직후(갱신 중)와 갱신 실패를 구분해 표시한다.
        // 두 축을 한 문구로 뭉개지 않는다(T14 S2 D4). 즉시 확인할 게 없어도 정리
        // 대상이 쌓여 있으면 "정상"이라 단언하지 않는다.
        // 등급을 모르는 상태를 action/cleanup 어느 쪽으로도 세지 않으면서 "정상"이라
        // 부르면, 그 세션들이 무엇인지 모르는 채 안심시키는 것이다(T14 S4c-2 D2).
        healthLabel: this.summaryStale
          ? this.fetchError
            ? "집계 갱신 실패"
            : "집계 갱신 중"
          : actionRequired > 0
            ? "확인 필요"
            : this.unrecognizedStateEntries.length > 0
                ? "어휘 확인 필요"
                : "정상",
      };
    },

    get truncationNote() {
      // 표는 **서버 창 + 창 밖 전이 보존분**의 혼합이다. 이걸 "상위 N건"으로 뭉뚱그리면
      // (예: 300 + 보존 50 → "상위 350건") 문구 자체가 거짓이 된다(리뷰 P2).
      // 그래서 출처별로 나눠 적고, 보존 상한에 밀려 뺀 건수도 함께 고지한다.
      const parts = [];
      const fromServer = this.sessionMeta.returned || 0;
      const matched = this.sessionMeta.matched ?? this.sessionMeta.total ?? 0;
      if (this.sessionMeta.truncated) {
        parts.push(
          this.sessionMeta.filtered
            ? `조건 일치 ${matched}건 중 ${fromServer}건 표시`
            : `최근 활동순 상위 ${fromServer}건 표시`,
        );
        parts.push(`전체 ${this.sessionMeta.total}건`);
      }
      if (this.retainedShown > 0) parts.push(`창 밖 전이 ${this.retainedShown}건 유지`);
      // 탈락 고지도 등급을 뭉개지 않는다 — cleanup 다수가 action 탈락을 가리면 안 된다.
      if (this.droppedAction > 0) parts.push(`확인 필요 ${this.droppedAction}건 미표시`);
      if (this.droppedCleanup > 0) parts.push(`비활성 ${this.droppedCleanup}건 미표시`);
      return parts.join(" · ");
    },

    get hasVisibilityNote() {
      return Boolean(this.truncationNote);
    },

    // ── 보관 범위 고지 (T14 S4a D8) ──
    // 상태 분포는 **보관 미관측 행**만 세는데(D7) "전체"는 저장 전량이다. 두 수가
    // 다른 이유를 화면이 말하지 않으면, API 봉투가 아무리 정직해도 사용자에게는
    // 설명 없는 숫자 변화다. 의미가 바뀌는 수와 그 설명은 같은 화면에 있어야 한다.
    get archivedScopeNote() {
      const archived = this.kpis.archivedTotal;
      if (!archived) return "";
      return `보관 ${archived}건은 상태 분포에서 분리됨`;
    },

    // 배지 합과 **범위 총계**를 대조한다(T14 S4c-2 D3). 비교 대상은 `total`이 아니라
    // 상태 분포의 scope 총계다 — S4a가 분포를 보관 미관측 집합으로 좁혔기 때문이다.
    get distributionNote() {
      const summary = this.sessionMeta.summary || {};
      const scopeTotal = summary.archive_not_observed_total;
      if (typeof scopeTotal !== "number" || !Number.isFinite(scopeTotal)) {
        // 기준이 없으면 **0으로 간주하지 않는다**. 0으로 두면 "확인 못 함"이
        // "일치함"으로 위장된다.
        return Object.keys(summary.by_state || {}).length ? "분포 합계 확인 불가" : "";
      }
      const shown = Object.values(this.kpis.counts).reduce((a, b) => a + Number(b || 0), 0);
      if (shown === scopeTotal) return "";
      return `상태 분포 합 ${shown} ≠ 범위 총계 ${scopeTotal}`;
    },

    get hasDistributionNote() {
      return Boolean(this.distributionNote);
    },

    get hasArchivedScopeNote() {
      return Boolean(this.archivedScopeNote);
    },

    // 상태 분포가 어느 집합의 것인지 **서버가 말한 값**으로 표기한다. 하드코딩하면
    // 서버가 범위를 바꿔도 라벨이 옛 주장을 계속 되뇐다.
    get stateScopeLabel() {
      const scope = (this.sessionMeta.summary || {}).state_distribution_scope;
      if (!scope) return "";
      if (scope === "archive_not_observed") return "상태 분포: 보관 제외";
      // 모르는 범위를 "현재"·"정상"으로 축약하지 않는다 — 미확인을 아는 척하지 않기.
      return `상태 분포: 범위 미상(${scope})`;
    },

    // 보관 행의 **마지막으로 알던** 상태 분포. 이름이 현재를 주장하지 않는다.
    get archivedLastKnownEntries() {
      const dist = (this.sessionMeta.summary || {}).archived_by_last_known_state || {};
      return Object.entries(dist)
        .filter(([, n]) => n > 0)
        .sort((a, b) => b[1] - a[1]);
    },

    get heldForQuery() {
      // 표의 분모는 "지금 이 질의로 보유한 행"이어야 한다. 서버 필터가 걸린 상태에서
      // 조건에 안 맞는 보존분까지 세면 렌더 행 수도 조건 일치 수도 아닌 값이 나온다
      // (실측: 표기 "1 / 4" — 4는 아무 의미도 없는 수)(리뷰 P2).
      const states = this.actionOnly ? Array.from(this.actionStates) : this.selectedStates;
      const apps = this.selectedApps;
      if (!states.length && !apps.length) return this.sessionList.length;
      return this.sessionList.filter((session) => {
        if (states.length && !states.includes(session.state)) return false;
        if (apps.length && !apps.includes(session.app)) return false;
        return true;
      }).length;
    },

    // 상태 필터 거절을 **구조화 필드로만** 판정한다(T14 S4c-2 D5 · R5).
    // 산문 `detail`을 파싱하면 서버가 문구를 바꾸는 순간 조용히 깨진다.
    // `code`가 없거나 모르는 값이면 **일반 실패로** 넘긴다 — 모든 422를 어휘 문제라
    // 부르지 않는다.
    async handleFilterRejection(response, generation) {
      if (response.status !== 422) return false;
      let detail = null;
      try {
        const body = await response.json();
        detail = body && body.detail;
      } catch (error) {
        return false;
      }
      if (!detail || typeof detail !== "object") return false;
      if (detail.code !== "unknown_state_filter") return false;
      const invalid = Array.isArray(detail.invalid_values)
        ? detail.invalid_values.map(String)
        : [];
      if (!invalid.length) return false;
      // **이번 질의에 실제로 실렸고 아직 거절 목록에 없는 값**만 새 사실이다.
      // 그 확인 없이 재조회하면, 서버·프록시가 같은 422를 반복할 때 조건이 하나도
      // 바뀌지 않은 채 무한 재귀하고 단일 비행 latch가 영영 풀리지 않는다
      // (구현리뷰 R1 P1 — 무증상 정지는 이 트랙이 S1에서 이미 겪은 실패 모드다).
      const sent = new Set(this.actionOnly ? Array.from(this.actionStates) : this.appliedStates);
      const known = new Set(this.rejectedStates);
      const fresh = invalid.filter((value) => sent.has(value) && !known.has(value));
      if (!fresh.length) return false;
      if (generation !== this.snapshotGeneration) return true;
      // 선택은 **지우지 않는다**. 다음 질의에서만 빼고, 빠졌다는 사실을 계속 말한다.
      this.rejectedStates = [...this.rejectedStates, ...fresh];
      this.fetchError = "";
      // 남은 유효 선택으로 곧바로 다시 조회한다. 조건이 반드시 줄어들었으므로
      // 이 재시도는 유한하다.
      this.snapshotInFlight = false;
      await this.refreshSessions();
      return true;
    },

    replaceSessions(payload) {
      const rows = Array.isArray(payload) ? payload : payload?.items;
      const next = {};
      for (const row of rows || []) {
        const session = normalizeSession(row);
        next[session.session_id] = session;
      }
      // SSE로 알게 된 세션이 스냅샷 창 밖이면 그대로 사라진다. stale/holding 전이는
      // 정의상 last_activity가 오래된 행이라 **항상** 창 밖이고, 결과적으로 조치가
      // 필요한 전이일수록 화면에서 깜빡이고 소멸했다(리뷰 P2).
      //
      // 보존에는 **종료 계약**이 있어야 한다(리뷰 P1). 무기한 쌓으면 로컬 행이
      // 서버 창과 무한히 벌어진다. 그래서 보존은 다음 셋으로 한정한다:
      //   1) 스냅샷에 다시 등장하면 그 값이 이기고 플래그도 사라진다(창 안이므로 보존 불필요).
      //   2) 조치가 필요한 상태(action_states)일 때만 남긴다 — 보존의 이유가 그것이었다.
      //   3) 최신 전이 순으로 상한(RETAINED_MAX)까지만.
      for (const [id, session] of Object.entries(this.sessions)) {
        if (!next[id] && session.outside_window) next[id] = session;
      }
      this.sessions = next;
      this.enforceRetentionBudget();
      if (!Array.isArray(payload) && payload) {
        const { items, ...meta } = payload;
        this.sessionMeta = meta;
      }
    },

    enforceRetentionBudget() {
      // 상한·고지는 **스냅샷 성공 경로에만** 적용하면 안 된다. 재조회가 실패하는 동안
      // SSE만 계속 들어오면 보존분이 무제한 누적되고 고지는 과거 값에 굳는다(리뷰 P2).
      // 그래서 보존 집합이 바뀌는 모든 지점에서 이 예산을 강제한다.
      // **등급 우선순위**(T14 S2 D6): action ∪ cleanup을 같은 예산에 넣으면 다량의
      // cleanup(stale) 전이가 칸을 채워 정작 즉시 확인할 action이 탈락한다.
      // action을 먼저 채우고 남은 칸에만 cleanup을 넣는다.
      const action = this.actionStates;
      const inactive = this.inactiveStates;
      const bySeq = (a, b) => (b[1].sse_seq || 0) - (a[1].sse_seq || 0);
      const outside = Object.entries(this.sessions).filter(([, s]) => s.outside_window);
      const actionable = outside.filter(([, s]) => action.has(s.state)).sort(bySeq);
      const cleanable = outside.filter(([, s]) => inactive.has(s.state)).sort(bySeq);

      const retainedAction = actionable.slice(0, RETAINED_OUTSIDE_WINDOW_MAX);
      const remaining = Math.max(0, RETAINED_OUTSIDE_WINDOW_MAX - retainedAction.length);
      const retainedCleanup = cleanable.slice(0, remaining);
      const retainedIds = new Set([...retainedAction, ...retainedCleanup].map(([id]) => id));

      const next = {};
      for (const [id, session] of Object.entries(this.sessions)) {
        if (!session.outside_window || retainedIds.has(id)) next[id] = session;
      }
      this.sessions = next;
      this.retainedShown = retainedIds.size;

      // 탈락 원장은 **등급별로 분리**하고 상호 배타를 유지한다(T14 S2 D6).
      // 예산 강제는 전이마다 돌기 때문에 순간 초과분만 세면 매번 0으로 리셋돼 고지가
      // 사라진다 — "알고는 있는데 표에 못 올린 세션"을 id로 누적한다.
      const drop = (entries, into, other) => {
        for (const [id] of entries) {
          other.delete(id);
          into.add(id);
        }
      };
      drop(actionable.slice(RETAINED_OUTSIDE_WINDOW_MAX), this._droppedActionIds, this._droppedCleanupIds);
      drop(cleanable.slice(remaining), this._droppedCleanupIds, this._droppedActionIds);
      // 보존 재진입·창 내부 복귀 시 양쪽에서 해제한다.
      for (const id of retainedIds) {
        this._droppedActionIds.delete(id);
        this._droppedCleanupIds.delete(id);
      }
      // 등급 **밖**으로 전이한 세션도 해제한다. 탈락했던 HOLDING이 LIVE가 되면
      // 더는 가시성 손실이 아닌데, 원장에 남으면 "확인 필요 N건 미표시"가 거짓이 된다.
      for (const [id, session] of outside) {
        if (!action.has(session.state) && !inactive.has(session.state)) {
          this._droppedActionIds.delete(id);
          this._droppedCleanupIds.delete(id);
        }
      }
      for (const id of Object.keys(next)) {
        if (!next[id].outside_window) {
          this._droppedActionIds.delete(id);
          this._droppedCleanupIds.delete(id);
        }
      }
      this.droppedAction = this._droppedActionIds.size;
      this.droppedCleanup = this._droppedCleanupIds.size;
      this.retainedDropped = this.droppedAction + this.droppedCleanup;
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
      // 상태·앱은 서버 필터라 조건이 바뀌면 창을 다시 받아야 한다.
      if (listName !== "selectedProjects") this.refreshSessions();
    },

    resetFilters() {
      this.selectedApps = [];
      this.selectedStates = [];
      // 거절 표시는 **그 선택에 대한 사실**이다. 선택을 비웠는데 고지가 남으면
      // 더 이상 걸지 않은 조건이 적용 제외 중인 것처럼 보인다(구현리뷰 R5 P2).
      this.rejectedStates = [];
      this.selectedProjects = [];
      this.actionOnly = false;
      this.persistViewState();
      this.refreshSessions();
    },

    updateActionOnly(value) {
      this.actionOnly = Boolean(value);
      this.persistViewState();
      this.refreshSessions();
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
      // 클라가 어휘를 판정하지 않는다(T14 S4c-2 D1). 예전에는 하드코딩 배열로 걸러
      // **서버가 아는 상태까지 조용히 버렸다**. 수용 여부는 서버가 말하고, 거절되면
      // 구조화 오류로 알려 준다(D5).
      this.selectedStates = this.parseParamList(params.get("states"), ",");
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

    serverFilterQuery() {
      // 상태·앱 필터는 **서버에서** 전량에 건다. 창 안에서만 거르면
      // `last_activity` 정렬과 반상관인 stale/holding이 창 밖에 남아
      // "KPI는 N건이라는데 표에는 없다"가 된다(리뷰 P1).
      const params = new URLSearchParams();
      // 거절된 값은 **어느 경로로도** 다시 보내지 않는다. `actionOnly`가 거절값을
      // 그대로 실어 보내면 화면은 "적용되지 않음"이라 말하는데 요청에는 실려 있고,
      // 조건이 줄지 않아 재시도 유한성 근거도 무너진다(구현리뷰 R2 P1).
      const rejected = new Set(this.rejectedStates);
      const states = (
        this.actionOnly ? Array.from(this.actionStates) : this.selectedStates
      ).filter((state) => !rejected.has(state));
      for (const state of states) params.append("states", state);
      for (const app of this.selectedApps) params.append("apps", app);
      const query = params.toString();
      return query ? `?${query}` : "";
    },

    // 선택된 값 중 **실제로 질의에 적용되는** 것. 거절된 값은 선택에는 남지만
    // 질의에서는 빠진다 — 그 차이를 화면이 말한다(T14 S4c-2 D5).
    get appliedStates() {
      const rejected = new Set(this.rejectedStates);
      return this.selectedStates.filter((state) => !rejected.has(state));
    },

    // 거절은 **그때의 사실**이지 영구 판정이 아니다(구현리뷰 R2 P1). 성공 스냅샷이
    // 그 값을 어휘·관측에 포함해 말하면 표시를 거둔다 — 그러지 않으면 서버가 이미
    // 수용하는 값이 이 페이지에서만 영원히 도달 불가로 남는다.
    clearRecoveredRejections() {
      if (!this.rejectedStates.length) return;
      // **어휘를 선언한 응답에서만** 판단한다(구현리뷰 R3 P1). 관측됐다는 사실은
      // "서버가 그 값으로 거를 수 있다"는 뜻이 아니다 — 어휘를 말하지 않는 서버는
      // 수용 규칙도 옛것일 수 있고, 그때 표시를 거두면 같은 422를 반복하게 된다.
      if (this.stateVocabularyUnknown) return;
      // 수용 규칙은 서버가 세운 것과 같다: 계약 어휘 ∪ 관측된 상태(설계 D2).
      const accepted = new Set([...this.stateVocabulary, ...this.observedStates]);
      this.rejectedStates = this.rejectedStates.filter((state) => !accepted.has(state));
    },

    // 필터 라벨 — 상태 이름에 **그 값에 대한 사실**을 덧붙인다. 고를 수 있다는 것과
    // 계약 어휘라는 것, 그리고 실제로 질의에 적용된다는 것은 서로 다른 사실이다.
    stateFilterLabel(state) {
      const value = String(state);
      if (this.isRejectedState(value)) return `${value} (적용되지 않음)`;
      if (!this.isRecognizedState(value)) return `${value} (계약 어휘 밖)`;
      return value;
    },

    isRejectedState(state) {
      return this.rejectedStates.includes(String(state));
    },

    get rejectedStateNote() {
      if (!this.rejectedStates.length) return "";
      return `서버가 모르는 필터 값(적용되지 않음): ${this.rejectedStates.join(" · ")}`;
    },

    async refreshSessions() {
      // **단일 비행**: 이미 요청이 떠 있으면 새로 쏘지 않고 "끝나면 한 번 더"만 남긴다.
      // 세대 가드만 있으면, 요청이 완료보다 빨리 발생하는 구간(SSE 버스트)에서
      // 매 응답이 직후 요청에 의해 폐기돼 **아무것도 반영되지 않는다**(실측: 필터를
      // 걸어도 화면이 초기 스냅샷에 굳음). 동시성을 1로 묶어 마지막 결과가 반드시 적용되게 한다.
      if (this.snapshotInFlight) {
        this.snapshotQueued = true;
        return;
      }
      this.snapshotInFlight = true;
      const generation = ++this.snapshotGeneration;
      try {
        // limit은 서버 계약(기본 page size)에 맡긴다. 과거 클라 하드코딩 300이
        // 서버 기본 100과 어긋나 계층마다 "전체"가 달랐다.
        // 타임아웃이 없으면 settle 하지 않는 요청 하나가 단일 비행 latch를 영구
        // 고착시켜 표·KPI가 **무증상으로** 얼어붙는다(리뷰 P1 — 이 슬라이스가 새로
        // 만든 실패 모드). 반드시 스스로 끝나는 요청만 띄운다.
        const response = await fetch(`/api/sessions${this.serverFilterQuery()}`, {
          cache: "no-store",
          signal: AbortSignal.timeout(SNAPSHOT_TIMEOUT_MS),
        });
        if (!response.ok) {
          const handled = await this.handleFilterRejection(response, generation);
          if (handled) return;
          throw new Error(`HTTP ${response.status}`);
        }
        const payload = await response.json();
        if (generation !== this.snapshotGeneration) return;
        this.replaceSessions(payload);
        this.clearRecoveredRejections();
        this.fetchError = "";
        this.summaryStale = false;
      } catch (error) {
        if (generation !== this.snapshotGeneration) return;
        this.fetchError =
          error && (error.name === "TimeoutError" || error.name === "AbortError")
            ? "세션 스냅샷 갱신 시간 초과"
            : "세션 스냅샷 갱신 실패";
        // 갱신에 실패한 집계를 현재 사실처럼 두지 않는다. 행은 SSE로 움직이는데
        // KPI만 굳으면 "행은 error인데 요약은 정상"이라는 거짓 안심이 생긴다.
        this.summaryStale = true;
        console.error("[ACP] session snapshot refresh failed", error);
      } finally {
        this.snapshotInFlight = false;
        if (this.snapshotQueued) {
          this.snapshotQueued = false;
          this.refreshSessions();
        }
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
          // 서버 시각을 브라우저 시계로 덮어쓰지 않는다. 이벤트가 준 값만 쓰고,
          // 없으면 기존 값을 유지한다(정렬키 오염 방지).
          updated_at: event.updated_at || current.updated_at,
          last_activity: event.last_activity || current.last_activity,
          // 스냅샷이 이 세션을 다시 주지 않아도(창 밖) 표에서 지우지 않는다.
          // `sse_seq`는 보존 상한을 넘을 때 최신 전이를 남기기 위한 단조 순번이다
          // (시계 대신 순번 — 브라우저 시계에 의존하지 않는다).
          outside_window: true,
          sse_seq: ++this.sseSequence,
        }),
      };
      this.enforceRetentionBudget();
      this.scheduleSummaryRefresh();
    },

    scheduleSummaryRefresh() {
      // KPI 집계는 **서버만** 계산한다. 클라가 previous_state로 카운트를 가감하면
      // 중복·누락·재연결 이벤트에서 총계가 조용히 드리프트한다(리뷰 P2).
      // 대신 전이가 감지되면 권위 스냅샷을 짧게 debounce해 다시 받는다 —
      // 반올림 오차가 누적될 산술 자체를 없앤다.
      // 전이가 관측된 순간 집계는 이미 낡았다. 재조회가 끝날 때까지 기다렸다가
      // 표시하면, 그 구간 동안 "행은 error인데 요약은 정상"이 된다(리뷰 P2).
      // 그래서 무효화 시점에 즉시 stale로 전환하고, 최신 세대의 성공 응답에서만 해제한다.
      this.summaryStale = true;
      // 전이 **이전에** 출발한 요청의 응답은 이 전이를 반영하지 못한다. 세대를 올려
      // in-flight 응답을 폐기하지 않으면, 그 낡은 응답이 도착해 stale을 풀고 다시
      // "정상"이라 단언한다(리뷰 P2 — 이 슬라이스가 없애려던 거짓 안심의 재발).
      this.snapshotGeneration += 1;
      if (this.summaryRefreshTimer) return;
      this.summaryRefreshTimer = window.setTimeout(() => {
        this.summaryRefreshTimer = null;
        this.refreshSessions();
      }, SUMMARY_REFRESH_DEBOUNCE_MS);
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
      const value = String(state || "unknown").toLowerCase();
      // 계약 어휘 밖 상태에 `badge-<state>`를 주면 CSS에 없는 클래스라 **배지가
      // 투명해져 숫자만 뜬다**(실측 — style.css에는 어휘 8개만 있다).
      if (!this.isRecognizedState(value)) return `badge ${ACP_UNRECOGNIZED_BADGE}`;
      return `badge badge-${value}`;
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

    // ── 실행 증거 표시 (T14 S4c-1 D2·D3) ──
    // 표는 "안 돌고 있음"과 "신호를 얻을 수 없음"을 똑같이 `-`로 그렸다. 두 사실은 다르고,
    // 후자를 전자로 그리면 화면이 관측하지 않은 것을 단언한다. 판단 재료는 전부 서버가 준
    // **구조화 필드**다 — 앱 이름으로 분기하지 않는다(R5).

    executionSignal(app) {
      const health = (this.sessionMeta.summary || {}).collector_health || {};
      const row = health[String(app)] || {};
      return {
        // 필드가 아예 없는 응답(옛 서버·캐시)도 `unknown`으로 닫는다.
        capability: String(row.process_signal_capability || "unknown"),
        signal: String(row.process_signal || "unknown"),
      };
    },

    // 값이 없을 때 그 **부재의 성격**을 말한다.
    executionAbsenceText(app) {
      const { capability, signal } = this.executionSignal(app);
      if (capability === "unsupported") return "신호 없음";
      if (capability === "supported" && signal === "ok") return "-";
      // unknown capability · supported인데 관측 실패/미상 · 계약 밖 값 → 전부 확인 불가.
      return "확인 불가";
    },

    executionCmdText(session) {
      const kind = String(session.execution_evidence_kind || "unknown");
      if (kind === "command") {
        // 서버가 `command`라 했는데 값이 없다 — 지어내지 않는다.
        return session.running_cmd ? this.shortText(session.running_cmd) : "확인 불가";
      }
      if (kind === "process_recheck") return "실행 확인됨";
      if (kind === "none") return this.executionAbsenceText(session.app);
      // 미지 enum. 명령으로 승격하지 않는다(fail-closed).
      return "확인 불가";
    },

    executionCmdTitle(session) {
      // 내부 마커 원문은 텍스트에도 `title`에도 넣지 않는다(D3).
      if (String(session.execution_evidence_kind) !== "command") return "";
      return session.running_cmd || "";
    },

    executionPidText(session) {
      if (session.running_pid) return String(session.running_pid);
      return this.executionAbsenceText(session.app);
    },

    rowId(session) {
      return `row-${String(session.session_id).replace(/[\\/]/g, "-")}`;
    },

    isInactiveState(state) {
      return this.inactiveStates.has(String(state || "").toLowerCase());
    },

    isActionState(state) {
      // 서버가 내려준 축을 쓴다(클라 상수는 첫 렌더 폴백일 뿐).
      return this.actionStates.has(String(state || "").toLowerCase());
    },
  };
}
