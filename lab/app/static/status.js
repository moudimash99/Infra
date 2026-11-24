// The homelab tool: one set of filters (machine, project, a search, problems only) for every card on a page, the
// first lines of a long list with "show more", and tabs. The filters live in the address (?m=&p=&q=&only=1), so a
// filtered view can be bookmarked, and links marked data-keep-filters carry them to the next page. A page without
// one of the controls (a machine's own page has no machine buttons) just leaves that filter off.
(function () {
  const bar = document.getElementById("filters");
  if (!bar) return;
  const ctl = (f) => bar.querySelector(`[data-f="${f}"]`);
  const state = { m: "", p: "", q: "", only: false };
  const q = new URLSearchParams(location.search);
  state.m = ctl("m") ? q.get("m") || "" : "";
  state.p = ctl("p") ? q.get("p") || "" : "";
  state.q = ctl("q") ? q.get("q") || "" : "";
  state.only = q.get("only") === "1";
  const open = new Set();                        // lists the reader expanded

  function passes(it) {
    const m = it.dataset.m || "", p = it.dataset.p || "";
    if (state.m && m !== "*" && m !== state.m) return false;
    if (state.p && p !== "*" && p !== state.p) return false;
    if (state.only && it.dataset.bad !== "1") return false;
    if (state.q) {
      const name = (it.dataset.name || it.textContent).toLowerCase();
      if (!state.q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => name.includes(w))) return false;
    }
    return true;
  }

  function apply() {
    let total = 0;
    document.querySelectorAll("[data-card]").forEach((card) => {
      let shown = 0, bad = 0;
      card.querySelectorAll(".items").forEach((list) => {
        if (list.hidden) {                       // the other tab: count nothing, but keep it filtered
          list.querySelectorAll(".it").forEach((it) => { it.hidden = !passes(it); });
          return;
        }
        const limit = parseInt(list.dataset.limit || "0", 10);
        let n = 0;
        const items = [...list.querySelectorAll(":scope > .it")];
        items.forEach((it) => {
          const ok = passes(it);
          if (ok) { n++; if (it.dataset.bad === "1") bad++; }
          it.hidden = !ok || (limit && !open.has(list) && n > limit);
        });
        shown += n;
        let more = list.nextElementSibling && list.nextElementSibling.classList.contains("show-more") ? list.nextElementSibling : null;
        if (limit && n > limit) {
          if (!more) {
            more = document.createElement("button");
            more.type = "button";
            more.className = "show-more";
            more.addEventListener("click", () => { open.has(list) ? open.delete(list) : open.add(list); apply(); });
            list.after(more);
          }
          more.hidden = false;
          more.textContent = open.has(list) ? "Show fewer" : `Show ${n - limit} more`;
        } else if (more) {
          more.hidden = true;
        }
      });
      const none = card.querySelector("[data-none]");
      if (none) none.hidden = shown > 0;
      const chip = card.querySelector("[data-chip]");
      if (chip) {
        chip.textContent = bad ? `${bad} to look at` : "All fine";
        chip.className = "qchip " + (bad ? "bad" : "ok");
      }
      card.hidden = state.only && bad === 0 && !card.hasAttribute("data-keep");
      if (card.id === "wrong") total = bad;
    });
    document.querySelectorAll(".room[data-m], .jobcard[data-m]").forEach((el) => {   // blocks that are one machine's
      el.hidden = (!!state.m && !!el.dataset.m && el.dataset.m !== state.m) ||
                  (!!state.p && el.dataset.p !== undefined && el.dataset.p !== state.p);
    });
    const cards = [...document.querySelectorAll("[data-card]")];
    let calm = document.getElementById("all-calm");
    if (state.only && cards.length && cards.every((c) => c.hidden)) {
      if (!calm) {
        calm = document.createElement("p");
        calm.id = "all-calm";
        calm.className = "calm";
        calm.textContent = "Nothing needs a look here.";
        cards[0].parentElement.before(calm);
      }
      calm.hidden = false;
    } else if (calm) {
      calm.hidden = true;
    }
    const v = document.getElementById("ov-verdict");
    if (v) {
      const scope = [state.m && bar.querySelector(`[data-f="m"] [data-v="${state.m}"]`)?.textContent,
                     state.p].filter(Boolean).join(", ");
      v.textContent = (total ? `${total} thing${total > 1 ? "s" : ""} to look at` : "All good") + (scope ? ` · ${scope}` : "");
      v.className = "ov-verdict " + (total ? "bad" : "ok");
    }
    bar.querySelectorAll('[data-f="m"] button').forEach((b) => b.classList.toggle("on", b.dataset.v === state.m));
    if (ctl("p")) ctl("p").value = state.p;
    if (ctl("q") && ctl("q").value !== state.q) ctl("q").value = state.q;
    if (ctl("only")) ctl("only").checked = state.only;
    const p = new URLSearchParams(location.search);
    ["m", "p", "q", "only"].forEach((k) => p.delete(k));
    if (state.m) p.set("m", state.m);
    if (state.p) p.set("p", state.p);
    if (state.q) p.set("q", state.q);
    if (state.only) p.set("only", "1");
    history.replaceState(null, "", location.pathname + (p.toString() ? "?" + p : "") + location.hash);
    document.querySelectorAll("a[data-keep-filters]").forEach((a) => {     // the range links keep the filters
      const u = new URL(a.getAttribute("href"), location.href);
      ["m", "p", "q", "only"].forEach((k) => (p.get(k) ? u.searchParams.set(k, p.get(k)) : u.searchParams.delete(k)));
      a.setAttribute("href", u.pathname === location.pathname ? u.search + u.hash : u.pathname + u.search + u.hash);
    });
  }

  if (ctl("m")) ctl("m").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) { state.m = b.dataset.v; apply(); }
  });
  if (ctl("p")) ctl("p").addEventListener("change", (e) => { state.p = e.target.value; apply(); });
  if (ctl("q")) ctl("q").addEventListener("input", (e) => { state.q = e.target.value.trim(); apply(); });
  if (ctl("only")) ctl("only").addEventListener("change", (e) => { state.only = e.target.checked; apply(); });
  // Sort buttons: data-sort-for names a list; each item carries data-k-<key> ("" = the page's own order).
  document.querySelectorAll("[data-sort-for]").forEach((seg) => {
    const list = document.getElementById(seg.dataset.sortFor);
    if (!list) return;
    [...list.children].forEach((it, i) => { it.dataset.i = i; });
    seg.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-sort]");
      if (!b) return;
      seg.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      const key = b.dataset.sort;
      const val = (it) => (key ? it.dataset["k" + key[0].toUpperCase() + key.slice(1)] : it.dataset.i);
      const items = [...list.children];
      items.sort((x, y) => {
        if (!key) return x.dataset.i - y.dataset.i;
        const a = val(x), c = val(y);
        const na = parseFloat(a), nc = parseFloat(c);
        return isNaN(na) || isNaN(nc) ? String(a).localeCompare(String(c)) : nc - na;   // numbers biggest first
      });
      items.forEach((it) => list.appendChild(it));
      apply();
    });
  });
  document.querySelectorAll("[data-tabs]").forEach((tabs) => tabs.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (!b) return;
    const card = tabs.closest("[data-card]");
    tabs.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    card.querySelectorAll("[data-pane]").forEach((p) => { p.hidden = p.dataset.pane !== b.dataset.tab; });
    apply();
  }));
  apply();
})();
