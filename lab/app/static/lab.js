// Lab: tooltips for marks (hover, focus, tap), a crosshair for line charts, and a quiet refresh of the
// "Needs you" strip (only that strip: the rest of the page, a playing clip, the scroll position stay put).
(function () {
  const tip = document.createElement("div");
  tip.id = "tip";
  document.body.appendChild(tip);
  let pinned = null;

  function place(x, y) {
    const w = tip.offsetWidth, h = tip.offsetHeight;
    let left = x + 12, top = y - h - 12;
    if (left + w > window.innerWidth - 8) left = x - w - 12;
    if (left < 8) left = 8;
    if (top < 8) top = y + 16;
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }
  function showText(text, x, y) {
    tip.textContent = "";
    const b = document.createElement("b");
    b.textContent = text;
    tip.appendChild(b);
    tip.style.display = "block";
    place(x, y);
  }
  function hide() { tip.style.display = "none"; pinned = null; }

  // Marks with data-tip: columns, meters.
  document.addEventListener("pointerover", (e) => {
    const el = e.target.closest("[data-tip]");
    if (el && e.pointerType === "mouse") showText(el.dataset.tip, e.clientX, e.clientY);
  });
  document.addEventListener("pointermove", (e) => {
    const el = e.target.closest("[data-tip]");
    if (el && e.pointerType === "mouse") place(e.clientX, e.clientY);
  });
  document.addEventListener("pointerout", (e) => {
    if (e.pointerType === "mouse" && e.target.closest("[data-tip]")) hide();
  });
  document.addEventListener("click", (e) => {        // touch: tap a mark to see it, tap elsewhere to close
    const el = e.target.closest("[data-tip]");
    if (el) { pinned = el; showText(el.dataset.tip, e.clientX, e.clientY); }
    else if (pinned) hide();
  });
  document.addEventListener("focusin", (e) => {
    const el = e.target.closest("[data-tip]");
    if (el) { const r = el.getBoundingClientRect(); showText(el.dataset.tip, r.left + r.width / 2, r.top); }
  });
  document.addEventListener("focusout", hide);
  window.addEventListener("scroll", () => { if (!pinned) hide(); }, { passive: true });

  // Line charts: a crosshair that snaps to the nearest point, listing every series at that time.
  const fmtTime = (t) => new Date(t * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" });
  const fmtVal = (v, unit) => (Math.abs(v) >= 100 ? Math.round(v).toLocaleString() : (Math.round(v * 10) / 10).toLocaleString()) + (unit || "");
  document.querySelectorAll("svg.chart[data-lines]").forEach((svg) => {
    const d = JSON.parse(svg.dataset.lines);
    const cross = svg.querySelector(".cross");
    const area = svg.querySelector(".hit-area");
    const xs = [...new Set(d.series.flatMap((s) => s.points.map((p) => p[0])))].sort((a, b) => a - b);
    function at(clientX) {
      const r = svg.getBoundingClientRect();
      const vx = ((clientX - r.left) / r.width) * svg.viewBox.baseVal.width;
      const t = d.xmin + ((vx - d.x0) / (d.x1 - d.x0)) * (d.xmax - d.xmin);
      let best = xs[0];
      for (const x of xs) if (Math.abs(x - t) < Math.abs(best - t)) best = x;
      return best;
    }
    function show(e) {
      const t = at(e.clientX);
      const px = d.x0 + ((t - d.xmin) / (d.xmax - d.xmin || 1)) * (d.x1 - d.x0);
      cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
      tip.textContent = "";
      const head = document.createElement("div");
      head.className = "muted";
      head.textContent = fmtTime(t);
      tip.appendChild(head);
      d.series.forEach((s) => {
        const p = s.points.reduce((a, b) => (Math.abs(b[0] - t) < Math.abs(a[0] - t) ? b : a));
        if (p[1] === null) return;
        const row = document.createElement("div");
        row.className = "row";
        if (d.series.length > 1) { const k = document.createElement("i"); k.className = "key line-key s" + s.slot; row.appendChild(k); }
        const v = document.createElement("b"); v.textContent = fmtVal(p[1], d.unit); row.appendChild(v);
        if (d.series.length > 1) { const n = document.createElement("span"); n.className = "muted"; n.textContent = s.name; row.appendChild(n); }
        tip.appendChild(row);
      });
      tip.style.display = "block";
      place(e.clientX, e.clientY);
    }
    area.addEventListener("pointermove", show);
    area.addEventListener("pointerdown", show);
    area.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); tip.style.display = "none"; });
  });

  // Refresh the "Needs you" strip every minute, and the page's own timestamp.
  const strip = document.getElementById("attention");
  if (strip) {
    setInterval(async () => {
      if (document.hidden) return;
      try {
        const r = await fetch("/fragment/attention", { headers: { "Accept": "text/html" } });
        if (r.ok) strip.innerHTML = await r.text();   // server-rendered, escaped by the template
      } catch (e) { /* offline for a moment: keep what's shown */ }
    }, 60000);
  }
})();
