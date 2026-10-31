// Shared office logic: state polling + WebSocket refresh, live event feed, inspector panels and the 2D fallback.
// All data comes from authorized backend endpoints; nothing here is simulated.
window.VCOOSOffice = (function () {
  const cfg = window.VCOOS_OFFICE;
  const STATES = {
    working: ["Working", "#22c55e"], idle: ["Idle", "#94a3b8"], waiting: ["Waiting for input", "#eab308"],
    blocked: ["Blocked", "#f97316"], awaiting_approval: ["Awaiting approval", "#a855f7"], under_review: ["Under review", "#3b82f6"],
    failed: ["Failed", "#ef4444"], paused: ["Paused", "#64748b"], offline: ["Offline", "#334155"],
  };
  const TASK_LABELS = {
    queued: "Queued", running: "Running", under_review: "Under review", completed: "Completed", failed: "Failed",
    blocked: "Blocked", paused: "Paused", cancelled: "Cancelled", awaiting_approval: "Awaiting approval",
    waiting_input: "Waiting for input", revision_required: "Revision required", draft: "Draft",
  };
  const listeners = [];
  const hooks = { focusRoom: null, focusHub: null, home: null };
  let state = null, mode = cfg.force2d ? "2d" : "3d", currentRoom = null;

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const csrf = () => (document.querySelector("input[name=csrfmiddlewaretoken]") || {}).value || "";
  const initials = (n) => String(n).split(/\s+/).slice(0, 2).map((p) => p[0] || "").join("").toUpperCase();
  const stateLabel = (k) => (STATES[k] || [k])[0];
  const sync = (txt) => { const el = $("office-sync"); if (el) el.textContent = txt; };
  const dept = (id) => state && state.departments.find((d) => d.id === Number(id));

  // ------------------------------------------------------------------ data

  async function refresh() {
    try {
      const r = await fetch(cfg.stateUrl, { headers: { Accept: "application/json" }, credentials: "same-origin" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      state = await r.json();
      sync(`Live · ${new Date().toLocaleTimeString()}`);
      renderChrome();
      listeners.forEach((fn) => fn(state));
      if (mode === "2d") render2d();
      if (currentRoom && $("drawer") && !$("drawer").dataset.employee) showRoom(currentRoom, { keepTab: true });
    } catch (e) {
      sync(`Last known state (refresh failed: ${e.message})`);
    }
  }

  let wsTimer = null;
  function connectWs() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    let ws;
    try { ws = new WebSocket(`${proto}://${location.host}${cfg.wsPath}`); } catch (e) { return; }
    ws.onmessage = (m) => {
      try { pushFeed(JSON.parse(m.data)); } catch (e) { /* ignore malformed */ }
      clearTimeout(wsTimer);
      wsTimer = setTimeout(refresh, 250);
    };
    ws.onclose = () => setTimeout(connectWs, 5000);
  }

  // ------------------------------------------------------------------ chrome (pills, feed, models)

  function renderChrome() {
    const h = state.hub || {};
    if ($("hq-agents")) $("hq-agents").innerHTML = `<span class="dot" style="background:#22c55e"></span>${h.agents ?? 0} agents · ${h.running ?? 0} running · ${h.queued ?? 0} queued`;
    if ($("hq-engine")) {
      const ok = (h.workers || 0) > 0;
      $("hq-engine").innerHTML = `<span class="dot" style="background:${ok ? "#22d3ee" : "#f59e0b"}"></span>${ok ? `engine · ${h.workers} worker${h.workers > 1 ? "s" : ""}` : "engine · no worker running"}`;
      $("hq-engine").classList.toggle("warn", !ok);
    }
    if ($("hq-models")) {
      const models = h.models_24h || [];
      $("hq-models").innerHTML = models.length ? `<span class="hq-muted">Models used (24h)</span>${models.map((m) => `<span class="hq-chip">${esc(m)}</span>`).join("")}`
        : `<span class="hq-muted">No model calls in the last 24 hours</span>`;
    }
  }

  function pushFeed(ev) {
    const feed = $("hq-feed");
    if (!feed || ev.event !== "task") return;
    const p = ev.payload || {};
    const d = dept(p.department_id);
    const item = document.createElement("div");
    item.className = "hq-toast";
    item.innerHTML = `<span class="hq-icon" style="background:${esc(d ? d.color : "#64748b")}">${esc(d ? initials(d.name) : "·")}</span>
      <div><div class="hq-toast-title">${esc(d ? d.name : "Task")} · ${esc(p.code || "")}</div>
      <div class="hq-toast-body">${esc(String(p.title || "").slice(0, 60))} → <strong>${esc(TASK_LABELS[p.status] || p.status || "")}</strong></div></div>`;
    feed.prepend(item);
    while (feed.children.length > 4) feed.lastElementChild.remove();
    setTimeout(() => { item.classList.add("out"); setTimeout(() => item.remove(), 400); }, 12000);
  }

  function legendHtml() {
    return Object.entries(STATES).map(([k, [label]]) => `<span class="state st-${k}">${label}</span>`).join("");
  }

  // ------------------------------------------------------------------ panels

  function openPanel(html) {
    const drawer = $("drawer");
    if (!drawer) return null;
    drawer.hidden = false;
    drawer.innerHTML = `<button class="hq-close" type="button" aria-label="Close panel">×</button>${html}`;
    drawer.querySelector(".hq-close").addEventListener("click", closePanel);
    return drawer;
  }

  function closePanel() {
    const drawer = $("drawer");
    if (drawer) { drawer.hidden = true; delete drawer.dataset.employee; }
    currentRoom = null;
    if (hooks.home) hooks.home();
  }

  function agentRow(e) {
    return `<button class="hq-agent" data-emp="${e.id}"><span class="face st-${esc(e.state)}" style="background:${esc(e.color)}">${esc(initials(e.name))}</span>
      <span class="hq-agent-main"><span>${esc(e.name)}</span><span class="hq-muted">${esc(e.job_title)}${e.task ? " · " + esc(e.task.code) : ""}</span></span>
      <span class="hq-badge st-${esc(e.state)}">${esc(stateLabel(e.state))}</span></button>`;
  }

  function showRoom(id, opts = {}) {
    const d = dept(id);
    if (!d) return;
    currentRoom = d.id;
    const s = d.stats || {};
    const tab = opts.keepTab && $("drawer") && $("drawer").dataset.tab || "overview";
    const drawer = openPanel(`
      <div class="hq-head"><span class="hq-icon lg" style="background:${esc(d.color)}">${esc(initials(d.name))}</span>
        <div><h2>${esc(d.name)}</h2><div class="hq-muted">${d.employees.length} agent${d.employees.length === 1 ? "" : "s"}${d.manager ? " · lead " + esc(d.manager) : ""}${d.paused ? " · <strong>paused</strong>" : ""}</div></div></div>
      <div class="hq-tabs"><button data-t="overview">Overview</button><button data-t="agents">Agents</button><button data-t="tools">Tools</button></div>
      <div data-p="overview">
        <div class="hq-stats">
          <div><span>Done today</span><strong>${s.completed_today ?? 0}</strong></div>
          <div><span>Running</span><strong>${s.running ?? 0}</strong></div>
          <div><span>Avg turnaround</span><strong>${s.avg_turnaround_min != null ? s.avg_turnaround_min + "m" : "—"}</strong></div>
        </div>
        <div class="hq-stats">
          <div><span>Queued</span><strong>${s.queued ?? 0}</strong></div>
          <div><span>In review</span><strong>${s.review ?? 0}</strong></div>
          <div><span>Needs attention</span><strong>${s.blocked ?? 0}</strong></div>
        </div>
        <h3>Agents</h3>${d.employees.slice(0, 5).map(agentRow).join("") || '<div class="hq-muted">No agents in this team yet.</div>'}
      </div>
      <div data-p="agents" hidden>${d.employees.map(agentRow).join("") || '<div class="hq-muted">No agents.</div>'}</div>
      <div data-p="tools" hidden><p class="hq-muted">Tools granted to this team or its agents (default deny).</p>
        ${(s.tools || []).map((t) => `<div class="hq-tool"><span class="dot" style="background:#22c55e"></span><code>${esc(t)}</code></div>`).join("") || '<div class="hq-muted">No tools granted.</div>'}</div>
      <div class="hq-actions"><a class="hq-btn" href="/departments/${d.id}/">Team dashboard</a><a class="hq-btn" href="/tasks/?department=${d.id}">Tasks</a></div>`);
    if (!drawer) return;
    delete drawer.dataset.employee;
    const select = (t) => {
      drawer.dataset.tab = t;
      drawer.querySelectorAll("[data-t]").forEach((b) => b.classList.toggle("active", b.dataset.t === t));
      drawer.querySelectorAll("[data-p]").forEach((p) => { p.hidden = p.dataset.p !== t; });
    };
    drawer.querySelectorAll("[data-t]").forEach((b) => b.addEventListener("click", () => select(b.dataset.t)));
    select(tab);
    drawer.querySelectorAll("[data-emp]").forEach((b) => b.addEventListener("click", () => showEmployee(b.dataset.emp)));
    if (!opts.keepTab && hooks.focusRoom) hooks.focusRoom(d.id);
  }

  function showHub() {
    const h = (state && state.hub) || {};
    openPanel(`<div class="hq-head"><span class="hq-icon lg" style="background:#0891b2">⚙</span><div><h2>Orchestration engine</h2>
      <div class="hq-muted">One shared engine runs every AI employee</div></div></div>
      <div class="hq-stats"><div><span>Running</span><strong>${h.running ?? 0}</strong></div><div><span>Queued</span><strong>${h.queued ?? 0}</strong></div><div><span>Done today</span><strong>${h.completed_today ?? 0}</strong></div></div>
      <div class="hq-stats"><div><span>Workers live</span><strong>${h.workers ?? 0}</strong></div><div><span>In review</span><strong>${h.review ?? 0}</strong></div><div><span>Knowledge docs</span><strong>${h.knowledge_docs ?? "—"}</strong></div></div>
      ${(h.workers || 0) === 0 ? '<p class="hq-warn">No engine worker is running: queued work will not start. Run <code>python manage.py run_engine</code>.</p>' : ""}
      <div class="hq-actions"><a class="hq-btn" href="/monitor/">Live monitor</a><a class="hq-btn" href="/tasks/">Task board</a><a class="hq-btn" href="/knowledge/">Knowledge</a></div>`);
    if (hooks.focusHub) hooks.focusHub();
  }

  async function showEmployee(id) {
    const drawer = openPanel('<div class="spinner"></div>');
    if (!drawer) return;
    drawer.dataset.employee = id;
    try {
      const r = await fetch(`${cfg.employeeUrl}${id}/`, { credentials: "same-origin" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const e = await r.json();
      const t = e.task;
      const back = currentRoom ? `<button class="hq-back" type="button">← ${esc((dept(currentRoom) || {}).name || "Team")}</button>` : "";
      openPanel(`${back}
        <div class="hq-head"><span class="face lg st-${esc(e.state)}" style="background:${esc(e.color)}">${esc(initials(e.name))}</span>
        <div><h2>${esc(e.name)}</h2><div class="hq-muted">${esc(e.employee_id)} · ${esc(e.job_title)}</div></div></div>
        <p><span class="hq-badge st-${esc(e.state)}">${esc(stateLabel(e.state))}</span></p>
        <h3>Current task</h3>
        ${t ? `<div><a href="/tasks/${t.id}/">${esc(t.code)} ${esc(t.title)}</a></div><div class="hq-muted">${esc(t.status_label)}${t.project ? " · " + esc(t.project) : ""}</div>
              <div class="small">Last progress: ${esc(t.progress_note || "no progress reported")}</div><div class="hq-muted">Updated ${new Date(t.updated_at).toLocaleString()}</div>`
            : '<div class="hq-muted">No open task.</div>'}
        <h3>Models (priority order)</h3>
        ${(e.models || []).map((m) => `<div class="small">#${m.priority} ${esc(m.name)} <span class="hq-muted">${esc(m.provider)}</span></div>`).join("") || '<div class="hq-muted">None configured</div>'}
        ${e.last_model_used ? `<div class="small">Last used: <strong>${esc(e.last_model_used)}</strong></div>` : ""}
        <h3>Tools</h3>${(e.tools || []).map((x) => `<div class="hq-tool"><code>${esc(x)}</code></div>`).join("") || '<div class="hq-muted">none granted</div>'}
        ${(e.recent_steps || []).length ? `<h3>Recent activity</h3>${e.recent_steps.map((s) => `<div class="small">step ${s.step}: ${esc(s.action)}${s.tool ? " " + esc(s.tool) : ""}${s.summary ? " — " + esc(s.summary) : ""}</div>`).join("")}` : ""}
        <div class="hq-actions"><a class="hq-btn" href="/employees/${e.id}/">Full profile</a>
        ${state && state.can_control && (e.profile_status === "active" || e.profile_status === "paused") ? `
          <form method="post" action="/employees/${e.id}/"><input type="hidden" name="csrfmiddlewaretoken" value="${esc(csrf())}">
          <button class="hq-btn" name="action" value="${e.profile_status === "active" ? "pause" : "resume"}">${e.profile_status === "active" ? "Pause employee" : "Resume employee"}</button></form>` : ""}</div>`);
      $("drawer").dataset.employee = id;
      const b = $("drawer").querySelector(".hq-back");
      if (b) b.addEventListener("click", () => showRoom(currentRoom, { keepTab: true }));
    } catch (err) {
      openPanel(`<div class="hq-warn">Could not load employee: ${esc(err.message)}</div>`);
    }
  }

  // ------------------------------------------------------------------ 2D fallback

  function render2d() {
    const el = $("office-2d");
    if (!el || !state) return;
    el.innerHTML = state.departments.map((d) => `
      <div class="room2d" style="--room:${esc(d.color)}"><div class="row" style="justify-content:space-between"><button class="hq-btn" data-room="${d.id}">${esc(d.name)}</button>
      <span class="hq-muted">${d.employees.length} agent${d.employees.length === 1 ? "" : "s"} · ${(d.stats || {}).running || 0} running${d.paused ? " · paused" : ""}</span></div>
      <div style="margin-top:8px">${d.employees.map(agentRow).join("") || '<div class="hq-muted">Empty room</div>'}</div></div>`).join("")
      || '<div class="hq-muted">No departments visible to you.</div>';
    el.querySelectorAll("[data-emp]").forEach((b) => b.addEventListener("click", () => showEmployee(b.dataset.emp)));
    el.querySelectorAll("[data-room]").forEach((b) => b.addEventListener("click", () => showRoom(b.dataset.room)));
  }

  function fallback2d(reason) {
    if (mode === "2d") return;
    mode = "2d";
    if ($("office-canvas")) $("office-canvas").hidden = true;
    if ($("hq-minimap")) $("hq-minimap").hidden = true;
    if ($("hq-fx")) $("hq-fx").hidden = true;
    if ($("office-2d")) $("office-2d").hidden = false;
    sync(`2D view (${reason})`);
    render2d();
  }

  function onState(fn) { listeners.push(fn); if (state) fn(state); }

  document.addEventListener("DOMContentLoaded", () => {
    if (mode === "2d" && $("hq-fx")) $("hq-fx").hidden = true;
    if ($("office-legend")) $("office-legend").innerHTML = legendHtml();
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanel(); });
    refresh();
    setInterval(refresh, 8000);
    connectWs();
  });

  return { STATES, onState, showEmployee, showRoom, showHub, closePanel, fallback2d, getState: () => state, esc, hooks, initials };
})();
