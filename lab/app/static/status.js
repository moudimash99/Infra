// Homelab overview: one set of filters (machine, project, problems only) for every card, a few lines per card with
// "show more", and each card's count of things to look at. The filters live in the address (?m=&p=&only=1), so a
// filtered view can be bookmarked; the Memory tab gets the same machine.
(function () {
  const bar = document.getElementById("filters");
  if (!bar) return;
  const state = { m: "", p: "", only: false };
  const q = new URLSearchParams(location.search);
  state.m = q.get("m") || "";
  state.p = q.get("p") || "";
  state.only = q.get("only") === "1";
  const open = new Set();                        // lists the reader expanded

  function passes(it) {
    const m = it.dataset.m || "", p = it.dataset.p || "";
    if (state.m && m !== "*" && m !== state.m) return false;
    if (state.p && p !== "*" && p !== state.p) return false;
    if (state.only && it.dataset.bad !== "1") return false;
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
      card.hidden = state.only && bad === 0 && card.id !== "wrong";
      if (card.id === "wrong") total = bad;
    });
    const v = document.getElementById("ov-verdict");
    if (v) {
      const scope = [state.m && bar.querySelector(`[data-f="m"] [data-v="${state.m}"]`)?.textContent,
                     state.p].filter(Boolean).join(", ");
      v.textContent = (total ? `${total} thing${total > 1 ? "s" : ""} to look at` : "All good") + (scope ? ` · ${scope}` : "");
      v.className = "ov-verdict " + (total ? "bad" : "ok");
    }
    bar.querySelectorAll('[data-f="m"] button').forEach((b) => b.classList.toggle("on", b.dataset.v === state.m));
    bar.querySelector('[data-f="p"]').value = state.p;
    bar.querySelector('[data-f="only"]').checked = state.only;
    const p = new URLSearchParams();
    if (state.m) p.set("m", state.m);
    if (state.p) p.set("p", state.p);
    if (state.only) p.set("only", "1");
    history.replaceState(null, "", location.pathname + (p.toString() ? "?" + p : "") + location.hash);
  }

  bar.querySelector('[data-f="m"]').addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) { state.m = b.dataset.v; apply(); }
  });
  bar.querySelector('[data-f="p"]').addEventListener("change", (e) => { state.p = e.target.value; apply(); });
  bar.querySelector('[data-f="only"]').addEventListener("change", (e) => { state.only = e.target.checked; apply(); });
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
