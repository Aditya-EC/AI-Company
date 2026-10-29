// Shared office logic: state polling + WebSocket refresh, inspector drawer and 2D fallback.
// All data comes from authorized backend endpoints; nothing here is simulated.
window.VCOOSOffice = (function () {
  const cfg = window.VCOOS_OFFICE;
  const STATES = {
    working: ["Working", "#22c55e"], idle: ["Idle", "#94a3b8"], waiting: ["Waiting for input", "#eab308"],
    blocked: ["Blocked", "#f97316"], awaiting_approval: ["Awaiting approval", "#a855f7"], under_review: ["Under review", "#3b82f6"],
    failed: ["Failed", "#ef4444"], paused: ["Paused", "#64748b"], offline: ["Offline", "#334155"],
  };
  const listeners = [];
  let state = null, mode = cfg.force2d ? "2d" : "3d";

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const csrf = () => (document.querySelector("input[name=csrfmiddlewaretoken]") || {}).value || "";
  const initials = (n) => String(n).split(/\s+/).slice(0, 2).map((p) => p[0] || "").join("").toUpperCase();
  const sync = (txt) => { const el = document.getElementById("office-sync"); if (el) el.textContent = txt; };

  async function refresh() {
    try {
      const r = await fetch(cfg.stateUrl, { headers: { Accept: "application/json" }, credentials: "same-origin" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      state = await r.json();
      sync(`Live · updated ${new Date().toLocaleTimeString()}`);
      listeners.forEach((fn) => fn(state));
      if (mode === "2d") render2d();
    } catch (e) {
      sync(`Showing last known state (refresh failed: ${e.message})`);
    }
  }

  let wsTimer = null;
  function connectWs() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    let ws;
    try { ws = new WebSocket(`${proto}://${location.host}${cfg.wsPath}`); } catch (e) { return; }
    ws.onmessage = () => { clearTimeout(wsTimer); wsTimer = setTimeout(refresh, 250); };
    ws.onclose = () => setTimeout(connectWs, 5000);
  }

  function legendHtml() {
    return Object.entries(STATES).map(([k, [label]]) => `<span class="state st-${k}">${label}</span>`).join("");
  }

  async function showEmployee(id) {
    const drawer = document.getElementById("drawer");
    drawer.innerHTML = '<div class="spinner"></div>';
    try {
      const r = await fetch(`${cfg.employeeUrl}${id}/`, { credentials: "same-origin" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const e = await r.json();
      const [label] = STATES[e.state] || [e.state];
      const t = e.task;
      drawer.innerHTML = `
        <div class="row"><div class="avatar" style="width:auto;pointer-events:none"><div class="face st-${esc(e.state)}" style="background:${esc(e.color)}">${esc(initials(e.name))}</div></div>
        <div><h2 style="margin:0">${esc(e.name)}</h2><div class="muted small">${esc(e.employee_id)} · ${esc(e.job_title)}</div></div></div>
        <p><span class="state st-${esc(e.state)}">${esc(label)}</span></p>
        <h3>Current task</h3>
        ${t ? `<div><a href="/tasks/${t.id}/">${esc(t.code)} ${esc(t.title)}</a></div><div class="small muted">${esc(t.status_label)}${t.project ? " · " + esc(t.project) : ""}</div>
              <div class="small">Last progress: ${esc(t.progress_note || "no progress reported")}</div><div class="small muted">Updated ${new Date(t.updated_at).toLocaleString()}</div>`
            : '<div class="muted small">No open task.</div>'}
        <h3 style="margin-top:12px">Models (priority order)</h3>
        ${(e.models || []).map((m) => `<div class="small">#${m.priority} ${esc(m.name)} <span class="muted">${esc(m.provider)}</span></div>`).join("") || '<div class="small muted">None configured</div>'}
        ${e.last_model_used ? `<div class="small">Last used: <strong>${esc(e.last_model_used)}</strong></div>` : ""}
        <h3 style="margin-top:12px">Tools</h3><div class="small mono">${(e.tools || []).map(esc).join("<br>") || '<span class="muted">none granted</span>'}</div>
        ${(e.recent_steps || []).length ? `<h3 style="margin-top:12px">Recent activity</h3>${e.recent_steps.map((s) => `<div class="small">step ${s.step}: ${esc(s.action)}${s.tool ? " " + esc(s.tool) : ""}${s.summary ? " — " + esc(s.summary) : ""}</div>`).join("")}` : ""}
        <div class="row" style="margin-top:14px"><a class="btn sm" href="/employees/${e.id}/">Full profile</a>
        ${state && state.can_control && (e.profile_status === "active" || e.profile_status === "paused") ? `
          <form method="post" action="/employees/${e.id}/"><input type="hidden" name="csrfmiddlewaretoken" value="${esc(csrf())}">
          <button class="btn sm" name="action" value="${e.profile_status === "active" ? "pause" : "resume"}">${e.profile_status === "active" ? "Pause employee" : "Resume employee"}</button></form>` : ""}</div>`;
    } catch (err) {
      drawer.innerHTML = `<div class="messages"><li class="error">Could not load employee: ${esc(err.message)}</li></div>`;
    }
  }

  function showRoom(dept) {
    const drawer = document.getElementById("drawer");
    const counts = Object.entries(dept.counts || {}).map(([k, n]) => `<div><span class="state st-${esc(k)}">${n} ${esc((STATES[k] || [k])[0])}</span></div>`).join("");
    drawer.innerHTML = `<h2><span class="dot" style="background:${esc(dept.color)}"></span> ${esc(dept.name)}</h2>
      ${dept.paused ? '<p><span class="badge b-warn">Department paused</span></p>' : ""}
      <div class="small">${dept.employees.length} employee(s)</div>${counts}
      <h3 style="margin-top:12px">People</h3>
      ${dept.employees.map((e) => `<button class="avatar" data-emp="${e.id}"><div class="face st-${esc(e.state)}" style="background:${esc(e.color)}">${esc(initials(e.name))}</div><div><div>${esc(e.name)}</div><div class="small muted">${esc((STATES[e.state] || [e.state])[0])}${e.task ? " · " + esc(e.task.code) : ""}</div></div></button>`).join("") || '<div class="muted small">No employees.</div>'}
      <p style="margin-top:12px"><a class="btn sm" href="/departments/${dept.id}/">Open department dashboard</a> <a class="btn sm" href="/tasks/?department=${dept.id}">Tasks</a></p>`;
    drawer.querySelectorAll("[data-emp]").forEach((b) => b.addEventListener("click", () => showEmployee(b.dataset.emp)));
  }

  function render2d() {
    const el = document.getElementById("office-2d");
    if (!state) return;
    el.innerHTML = state.departments.map((d) => `
      <div class="card room2d" style="--room:${esc(d.color)}"><div class="row" style="justify-content:space-between"><button class="btn sm" data-room="${d.id}">${esc(d.name)}</button>${d.paused ? '<span class="badge b-warn">paused</span>' : ""}</div>
      <div style="margin-top:8px">${d.employees.map((e) => `<button class="avatar" data-emp="${e.id}"><div class="face st-${esc(e.state)}" style="background:${esc(e.color)}">${esc(initials(e.name))}</div><div><div>${esc(e.name)}</div><div class="small muted">${esc((STATES[e.state] || [e.state])[0])}${e.task ? " · " + esc(e.task.code) + " " + esc(String(e.task.title).slice(0, 40)) : ""}</div></div></button>`).join("") || '<div class="muted small">Empty room</div>'}</div></div>`).join("")
      || '<div class="empty">No departments visible to you.</div>';
    el.querySelectorAll("[data-emp]").forEach((b) => b.addEventListener("click", () => showEmployee(b.dataset.emp)));
    el.querySelectorAll("[data-room]").forEach((b) => b.addEventListener("click", () => showRoom(state.departments.find((d) => d.id == b.dataset.room))));
  }

  function fallback2d(reason) {
    if (mode === "2d") return;
    mode = "2d";
    document.getElementById("office-canvas").hidden = true;
    document.getElementById("office-2d").hidden = false;
    sync(`2D view (${reason})`);
    render2d();
  }

  function onState(fn) { listeners.push(fn); if (state) fn(state); }

  document.addEventListener("DOMContentLoaded", () => {
    const legend = document.getElementById("office-legend");
    if (legend) legend.innerHTML = legendHtml();
    refresh();
    setInterval(refresh, 8000);
    connectWs();
  });

  return { STATES, onState, showEmployee, showRoom, fallback2d, getState: () => state, esc };
})();
