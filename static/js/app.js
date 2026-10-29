// Theme toggle, tabs and confirmation dialogs (no secrets or privileged config live client-side).
(function () {
  const root = document.documentElement;
  const menu = document.getElementById("menu-toggle");
  if (menu) menu.addEventListener("click", () => document.getElementById("sidebar").classList.toggle("open"));
  const btn = document.getElementById("theme-toggle");
  if (btn) btn.addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("vcoos-theme", root.dataset.theme); } catch (e) {}
    window.dispatchEvent(new Event("vcoos-theme"));
  });
  document.querySelectorAll("[data-tabs]").forEach((tabs) => {
    const buttons = tabs.querySelectorAll("button[data-tab]");
    buttons.forEach((b) => b.addEventListener("click", () => {
      buttons.forEach((x) => x.classList.toggle("active", x === b));
      document.querySelectorAll(`[data-tab-panel="${tabs.dataset.tabs}"]`).forEach((p) => { p.hidden = p.dataset.name !== b.dataset.tab; });
    }));
  });
  document.addEventListener("submit", (ev) => {
    const msg = ev.submitter && ev.submitter.dataset.confirm || ev.target.dataset.confirm;
    if (msg && !confirm(msg)) ev.preventDefault();
  });
})();
