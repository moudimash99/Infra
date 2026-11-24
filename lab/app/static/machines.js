// Machines: the memory table's search, filters and sorting. The choices live in the address (?q=&m=&k=&f=1&s=now),
// so a filtered view can be bookmarked or sent.
(function () {
  const table = document.getElementById("mem-table");
  if (!table) return;
  const tbody = table.tBodies[0];
  const rows = [...tbody.rows];
  const q = document.getElementById("mem-q");
  const finished = document.getElementById("mem-finished");
  const none = document.getElementById("mem-none");
  const groups = { m: document.getElementById("mem-machine"), k: document.getElementById("mem-kind") };
  const state = { q: "", m: "", k: "", f: false, s: "now", d: "desc" };

  const params = new URLSearchParams(location.search);
  state.q = params.get("q") || "";
  state.m = params.get("m") || "";
  state.k = params.get("k") || "";
  state.f = params.get("f") === "1";
  if (params.get("s")) state.s = params.get("s");
  if (params.get("d")) state.d = params.get("d") === "asc" ? "asc" : "desc";

  function save() {
    const p = new URLSearchParams();
    if (state.q) p.set("q", state.q);
    if (state.m) p.set("m", state.m);
    if (state.k) p.set("k", state.k);
    if (state.f) p.set("f", "1");
    if (state.s !== "now" || state.d !== "desc") { p.set("s", state.s); p.set("d", state.d); }
    const qs = p.toString();
    history.replaceState(null, "", location.pathname + (qs ? "?" + qs : ""));
  }

  function apply() {
    const words = state.q.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    rows.forEach((r) => {
      const ok = (!state.m || r.dataset.machine === state.m)
        && (!state.k || r.dataset.kind === state.k)
        && (state.f || r.dataset.running === "1")
        && words.every((w) => r.dataset.name.includes(w));
      r.hidden = !ok;
      if (ok) shown++;
    });
    none.hidden = shown > 0;
    const text = ["name", "machine", "kind"].includes(state.s);
    const sign = state.d === "asc" ? 1 : -1;
    rows.slice().sort((a, b) => {
      const x = a.dataset[state.s] || "", y = b.dataset[state.s] || "";
      const c = text ? x.localeCompare(y) : (parseFloat(x) || 0) - (parseFloat(y) || 0);
      return c * sign || (parseFloat(b.dataset.now) - parseFloat(a.dataset.now));
    }).forEach((r) => tbody.appendChild(r));
    table.querySelectorAll("th button").forEach((b) => {
      const on = b.dataset.sort === state.s;
      b.classList.toggle("on", on);
      b.classList.toggle("asc", on && state.d === "asc");
      b.classList.toggle("desc", on && state.d === "desc");
      b.closest("th").setAttribute("aria-sort", on ? (state.d === "asc" ? "ascending" : "descending") : "none");
    });
    Object.entries(groups).forEach(([key, g]) => g.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.v === state[key])));
    q.value = state.q;
    finished.checked = state.f;
    save();
  }

  q.addEventListener("input", () => { state.q = q.value.trim(); apply(); });
  finished.addEventListener("change", () => { state.f = finished.checked; apply(); });
  Object.entries(groups).forEach(([key, g]) => g.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) { state[key] = b.dataset.v; apply(); }
  }));
  table.querySelector("thead").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-sort]");
    if (!b) return;
    const key = b.dataset.sort;
    if (state.s === key) state.d = state.d === "asc" ? "desc" : "asc";
    else { state.s = key; state.d = ["name", "machine", "kind"].includes(key) ? "asc" : "desc"; }
    apply();
  });
  apply();
})();
