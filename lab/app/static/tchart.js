// Wide, interactive charts for the homelab tool (a desktop first; Grafana-grade, no library).
//
//   <div class="tchart" data-src="id-of-a-json-script" data-h="260"></div>
//   <script type="application/json" id="...">{...}</script>
//
// kind "area":     {x: [t...], series: [{name, values, href?}], lines: [{name, values, dash?}], unit, stacked: true}
//                  the series stacked (biggest at the bottom); lines drawn over them (the machine's memory, the room)
// kind "lines":    {x, series, unit, yMax?}
// kind "timeline": {x0, x1, rows: [{name, href?, note?, segs: [[t0, t1, state]]}], marks: [{t, row, text}]}
//                  one bar per row: running (green), waiting for room (amber), failed (red); marks: pushed out
//
// Hover: a crosshair and every value at that time, biggest first. Legend: click a name to see it alone (click again
// for all; Ctrl-click to add or remove one); the arrow opens its page. Drag across a chart to zoom in: every chart on
// the page follows (like Grafana's time range); double-click or "Reset zoom" to go back.
(function () {
  const charts = [];
  let zoom = null;                                     // [t0, t1] shared by every chart on the page
  const NS = "http://www.w3.org/2000/svg";
  const css = getComputedStyle(document.documentElement);
  const PALETTE = [1, 2, 3, 4, 5, 7, 6, 8].map((i) => css.getPropertyValue("--s" + i).trim() || "#888")
    .concat(["#8c6d31", "#2f9fb5", "#b04fa8", "#5b8c2a", "#d4784a", "#6b7fd1", "#c7a12b", "#4fa89b",
             "#a35d6a", "#3e7a5e", "#9a7ac9", "#c26f2e"]);
  const STATE = { running: "var(--ok)", waiting: "var(--warn)", failed: "var(--bad)" };
  const tip = document.createElement("div");
  tip.className = "ttip";
  document.body.appendChild(tip);

  function el(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function niceTicks(top, n) {
    if (!(top > 0)) return [0, 1];
    const raw = top / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
    const out = [];
    for (let v = 0; v <= top + step * 0.001; v += step) out.push(+v.toFixed(10));
    if (out[out.length - 1] < top) out.push(+(out[out.length - 1] + step).toFixed(10));
    return out;
  }
  function timeTicks(t0, t1, px) {
    const span = t1 - t0, want = Math.max(2, Math.floor(px / 90));
    const steps = [600, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800];
    const step = steps.find((s) => span / s <= want) || 86400 * 7;
    const out = [];
    const d = new Date(t0 * 1000);
    d.setMinutes(0, 0, 0);
    if (step >= 86400) d.setHours(0);
    let t = d.getTime() / 1000;
    while (t < t0) t += step >= 86400 ? 86400 : step;
    for (; t <= t1; t += step) {
      const x = new Date(t * 1000);
      if (step >= 86400 && x.getHours() !== 0) { t -= x.getHours() * 3600; continue; }   // daylight saving
      out.push([t, step >= 86400 ? x.toLocaleDateString([], { day: "numeric", month: "short" })
                                 : x.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })]);
    }
    return out;
  }
  const fmtVal = (v, unit) => v == null ? "–" :
    (Math.abs(v) >= 100 ? Math.round(v).toLocaleString() : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2)) + (unit || "");
  const fmtTime = (t) => new Date(t * 1000).toLocaleString([], { weekday: "short", day: "numeric", month: "short",
                                                                  hour: "2-digit", minute: "2-digit" });
  const fmtDur = (s) => s < 90 ? Math.round(s) + " s" : s < 5400 ? Math.round(s / 60) + " min" :
    (s / 3600).toFixed(1) + " h";

  function showTip(html, x, y) {
    tip.innerHTML = html;
    tip.style.display = "block";
    const w = tip.offsetWidth, h = tip.offsetHeight;
    let left = x + 14, top = y + 14;
    if (left + w > innerWidth - 8) left = x - w - 14;
    if (top + h > innerHeight - 8) top = Math.max(8, y - h - 14);
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }
  const hideTip = () => { tip.style.display = "none"; };
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function Chart(box) {
    const cfg = JSON.parse(document.getElementById(box.dataset.src).textContent);
    const H = parseInt(box.dataset.h || "240", 10);
    const pad = { l: 46, r: 14, t: 10, b: 24 };
    const hidden = new Set();
    (cfg.series || []).forEach((s, i) => { s.color = s.color || PALETTE[i % PALETTE.length]; });
    const svgWrap = document.createElement("div");
    svgWrap.className = "tc-plot";
    box.appendChild(svgWrap);
    let legend = null;
    if (cfg.kind !== "timeline" && (cfg.series || []).length + (cfg.lines || []).length > 1) {
      legend = document.createElement("div");
      legend.className = "tc-legend";
      box.appendChild(legend);
    }

    function range() {
      const x0 = cfg.kind === "timeline" ? cfg.x0 : cfg.x[0], x1 = cfg.kind === "timeline" ? cfg.x1 : cfg.x[cfg.x.length - 1];
      return zoom ? [Math.max(x0, zoom[0]), Math.min(x1, zoom[1])] : [x0, x1];
    }

    function drawLegend() {
      if (!legend) return;
      legend.innerHTML = "";
      const last = (vals) => { for (let i = vals.length - 1; i >= 0; i--) if (vals[i] != null) return vals[i]; return null; };
      cfg.series.forEach((s) => {
        const it = document.createElement("span");
        it.className = "tc-item" + (hidden.has(s.name) ? " off" : "");
        const peak = Math.max(...s.values.map((v) => v || 0));
        it.innerHTML = `<i style="background:${s.color}"></i><b>${esc(s.name)}</b><small>${fmtVal(last(s.values), cfg.unit)}` +
          ` · peak ${fmtVal(peak, cfg.unit)}</small>` + (s.href ? `<a href="${esc(s.href)}" title="Open ${esc(s.name)}">›</a>` : "");
        it.addEventListener("click", (e) => {
          if (e.target.tagName === "A") return;
          if (e.ctrlKey || e.metaKey) { hidden.has(s.name) ? hidden.delete(s.name) : hidden.add(s.name); }
          else {
            const alone = cfg.series.every((o) => o === s || hidden.has(o.name)) && !hidden.has(s.name);
            hidden.clear();
            if (!alone) cfg.series.forEach((o) => { if (o !== s) hidden.add(o.name); });
          }
          draw();
        });
        legend.appendChild(it);
      });
      (cfg.lines || []).forEach((l) => {
        const it = document.createElement("span");
        it.className = "tc-item line";
        it.innerHTML = `<i class="${l.dash ? "dash" : ""}" style="border-color:${l.color || "var(--text-2)"}"></i>` +
          `<b>${esc(l.name)}</b><small>${fmtVal(last(l.values), cfg.unit)}</small>`;
        legend.appendChild(it);
      });
    }

    function draw() {
      svgWrap.innerHTML = "";
      const W = Math.max(280, svgWrap.clientWidth || box.clientWidth);
      if (cfg.kind === "timeline") return drawTimeline(W);
      const [t0, t1] = range();
      const svg = el("svg", { class: "tc", width: W, height: H, viewBox: `0 0 ${W} ${H}` }, svgWrap);
      const X = cfg.x, idx = X.map((t, i) => i).filter((i) => X[i] >= t0 && X[i] <= t1);
      const shown = cfg.series.filter((s) => !hidden.has(s.name));
      // stack (biggest at the bottom) or plain values
      const stacks = [];
      if (cfg.stacked) {
        const acc = X.map(() => 0);
        shown.forEach((s) => {
          const lo = acc.slice();
          s.values.forEach((v, i) => { acc[i] += v || 0; });
          stacks.push({ s, lo, hi: acc.slice() });
        });
      } else shown.forEach((s) => stacks.push({ s, lo: null, hi: s.values }));
      let top = cfg.yMax || 0;
      idx.forEach((i) => {
        stacks.forEach((k) => { if (k.hi[i] != null) top = Math.max(top, k.hi[i]); });
        (cfg.lines || []).forEach((l) => { if (l.values[i] != null) top = Math.max(top, l.values[i]); });
      });
      const ticks = niceTicks(top * 1.02, 4), ymax = ticks[ticks.length - 1];
      const x0 = pad.l, x1 = W - pad.r, y0 = pad.t, y1 = H - pad.b;
      const xOf = (t) => x0 + ((t - t0) / (t1 - t0 || 1)) * (x1 - x0), yOf = (v) => y1 - (v / ymax) * (y1 - y0);
      ticks.forEach((v) => {
        el("line", { class: "tc-grid" + (v === 0 ? " base" : ""), x1: x0, x2: x1, y1: yOf(v), y2: yOf(v) }, svg);
        el("text", { class: "tc-tick", x: x0 - 6, y: yOf(v) + 3.5, "text-anchor": "end" }, svg).textContent =
          fmtVal(v, "").replace(/\.00$/, "");
      });
      timeTicks(t0, t1, x1 - x0).forEach(([t, label]) => {
        el("text", { class: "tc-tick", x: xOf(t), y: H - 6, "text-anchor": "middle" }, svg).textContent = label;
      });
      const path = (vals, lo) => {
        let d = "", pen = false;
        idx.forEach((i) => {
          if (vals[i] == null) { pen = false; return; }
          d += (pen ? "L" : "M") + xOf(X[i]).toFixed(1) + "," + yOf(vals[i]).toFixed(1);
          pen = true;
        });
        if (lo && d) {
          const back = idx.slice().reverse().map((i) => "L" + xOf(X[i]).toFixed(1) + "," + yOf(lo[i] || 0).toFixed(1)).join("");
          d += back + "Z";
        }
        return d;
      };
      stacks.forEach((k) => {
        if (cfg.stacked) el("path", { d: path(k.hi, k.lo), fill: k.s.color, "fill-opacity": 0.55, stroke: k.s.color,
                                      "stroke-width": 1 }, svg);
        else el("path", { d: path(k.hi), fill: "none", stroke: k.s.color, "stroke-width": 1.8, "stroke-linejoin": "round" }, svg);
      });
      (cfg.lines || []).forEach((l) => {
        el("path", { d: path(l.values), fill: "none", stroke: l.color || "var(--text-2)", "stroke-width": 1.6,
                     "stroke-dasharray": l.dash ? "6 5" : "" }, svg);
      });
      const cross = el("line", { class: "tc-cross", x1: 0, x2: 0, y1: y0, y2: y1, visibility: "hidden" }, svg);
      const sel = el("rect", { class: "tc-sel", x: 0, y: y0, width: 0, height: y1 - y0, visibility: "hidden" }, svg);
      const hit = el("rect", { x: x0, y: y0, width: x1 - x0, height: y1 - y0, fill: "transparent" }, svg);
      const tAt = (cx) => t0 + ((cx - svg.getBoundingClientRect().left - x0) / (x1 - x0)) * (t1 - t0);
      const nearest = (t) => idx.reduce((a, i) => Math.abs(X[i] - t) < Math.abs(X[a] - t) ? i : a, idx[0]);
      let dragFrom = null;
      hit.addEventListener("pointermove", (e) => {
        const i = nearest(tAt(e.clientX));
        if (i == null) return;
        const px = xOf(X[i]);
        cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
        if (dragFrom != null) {
          const a = Math.min(dragFrom, e.clientX), b = Math.max(dragFrom, e.clientX), left = svg.getBoundingClientRect().left;
          sel.setAttribute("x", Math.max(x0, a - left)); sel.setAttribute("width", Math.min(x1, b - left) - Math.max(x0, a - left));
          sel.setAttribute("visibility", "visible");
        }
        const rows = shown.map((s) => [s, s.values[i]]).filter((r) => r[1] != null && (r[1] > 0 || !cfg.stacked))
          .sort((a, b) => b[1] - a[1]);
        let html = `<div class="tt-time">${fmtTime(X[i])}</div>`;
        (cfg.lines || []).forEach((l) => { html += `<div class="tt-row line"><span>${esc(l.name)}</span><b>${fmtVal(l.values[i], cfg.unit)}</b></div>`; });
        if (cfg.stacked) html += `<div class="tt-row total"><span>All shown</span><b>${fmtVal(rows.reduce((a, r) => a + r[1], 0), cfg.unit)}</b></div>`;
        rows.slice(0, 14).forEach(([s, v]) => {
          html += `<div class="tt-row"><i style="background:${s.color}"></i><span>${esc(s.name)}</span><b>${fmtVal(v, cfg.unit)}</b></div>`;
        });
        if (rows.length > 14) html += `<div class="tt-more">and ${rows.length - 14} more</div>`;
        showTip(html, e.clientX, e.clientY);
      });
      hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
      hit.addEventListener("pointerdown", (e) => { dragFrom = e.clientX; hit.setPointerCapture(e.pointerId); });
      hit.addEventListener("pointerup", (e) => {
        const from = dragFrom;
        dragFrom = null;
        sel.setAttribute("visibility", "hidden");
        if (from != null && Math.abs(e.clientX - from) > 8) setZoom([tAt(Math.min(from, e.clientX)), tAt(Math.max(from, e.clientX))]);
      });
      hit.addEventListener("dblclick", () => setZoom(null));
      drawLegend();
    }

    function drawTimeline(W) {
      const [t0, t1] = range();
      const rowH = 22, labelW = Math.min(230, Math.max(150, W * 0.2));
      const rows = cfg.rows;
      const h = rows.length * rowH + 26;
      const svg = el("svg", { class: "tc tl", width: W, height: h, viewBox: `0 0 ${W} ${h}` }, svgWrap);
      const x0 = labelW, x1 = W - 10;
      const xOf = (t) => x0 + ((Math.min(Math.max(t, t0), t1) - t0) / (t1 - t0 || 1)) * (x1 - x0);
      timeTicks(t0, t1, x1 - x0).forEach(([t, label]) => {
        el("line", { class: "tc-grid", x1: xOf(t), x2: xOf(t), y1: 0, y2: h - 20 }, svg);
        el("text", { class: "tc-tick", x: xOf(t), y: h - 6, "text-anchor": "middle" }, svg).textContent = label;
      });
      rows.forEach((r, ri) => {
        const y = ri * rowH;
        if (ri % 2) el("rect", { class: "tl-band", x: 0, y, width: W, height: rowH }, svg);
        const a = el("a", r.href ? { href: r.href } : {}, svg);
        const label = el("text", { class: "tl-label", x: 6, y: y + 15 }, a);
        label.textContent = r.name.length > 30 ? r.name.slice(0, 29) + "…" : r.name;
        if (r.note) { const n = el("text", { class: "tl-note", x: labelW - 8, y: y + 15, "text-anchor": "end" }, svg); n.textContent = r.note; }
        r.segs.forEach(([a0, a1, st]) => {
          if (a1 < t0 || a0 > t1) return;
          const sx = xOf(a0), w = Math.max(2, xOf(a1) - sx);
          const seg = el("rect", { x: sx, y: y + 4, width: w, height: rowH - 8, rx: 2, fill: STATE[st] || "var(--line-2)",
                                   class: "tl-seg" }, svg);
          seg.addEventListener("pointermove", (e) => showTip(`<div class="tt-time">${esc(r.name)}</div><div class="tt-row"><span>${
            { running: "Running", waiting: "Waiting for room", failed: "Failed" }[st]}</span><b>${fmtDur(a1 - a0)}</b></div>` +
            `<div class="tt-more">${fmtTime(a0)} → ${fmtTime(a1)}</div>`, e.clientX, e.clientY));
          seg.addEventListener("pointerleave", hideTip);
        });
      });
      (cfg.marks || []).forEach((m) => {
        if (m.t < t0 || m.t > t1) return;
        const mk = el("path", { d: `M${xOf(m.t) - 5},${m.row * rowH + 1} l5,7 l5,-7 Z`, class: "tl-mark" }, svg);
        mk.addEventListener("pointermove", (e) => showTip(`<div class="tt-time">${fmtTime(m.t)}</div><div class="tt-more">${esc(m.text)}</div>`,
                                                         e.clientX, e.clientY));
        mk.addEventListener("pointerleave", hideTip);
      });
      // drag to zoom over the bars, as on the other charts
      const hit = el("rect", { x: x0, y: 0, width: x1 - x0, height: h - 20, fill: "transparent", style: "pointer-events:none" }, svg);
      let from = null;
      svg.addEventListener("pointerdown", (e) => { if (e.clientX - svg.getBoundingClientRect().left > x0) from = e.clientX; });
      svg.addEventListener("pointerup", (e) => {
        if (from != null && Math.abs(e.clientX - from) > 8) {
          const left = svg.getBoundingClientRect().left, tA = (cx) => t0 + ((cx - left - x0) / (x1 - x0)) * (t1 - t0);
          setZoom([tA(Math.min(from, e.clientX)), tA(Math.max(from, e.clientX))]);
        }
        from = null;
      });
      svg.addEventListener("dblclick", () => setZoom(null));
      void hit;
    }

    return { draw };
  }

  function setZoom(z) {
    zoom = z;
    document.querySelectorAll("[data-zoom-reset]").forEach((b) => { b.hidden = !zoom; });
    charts.forEach((c) => c.draw());
    hideTip();
  }

  document.querySelectorAll(".tchart[data-src]").forEach((box) => { const c = Chart(box); charts.push(c); c.draw(); });
  document.querySelectorAll("[data-zoom-reset]").forEach((b) => b.addEventListener("click", () => setZoom(null)));
  let t = null;
  window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(() => charts.forEach((c) => c.draw()), 120); });
})();
