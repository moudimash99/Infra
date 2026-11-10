"""Small server-made SVG charts, phone sized (viewBox 360 wide, scaled to the card).

Colors are CSS roles (lab.css): series slots s1..s8 in the fixed categorical order, never cycled. Marks follow the
house spec: columns <= 24 px with a 4 px rounded data end and a square base, 2 px lines, hairline grid, a legend for
two or more series, labels in text colors only. Every mark carries data-tip for the hover/tap layer in lab.js, and
the crosshair layer gets the line data as JSON.
"""
import html
import json
import math

from markupsafe import Markup

W = 360
PAD_L, PAD_R, PAD_T, PAD_B = 34, 12, 10, 22


def esc(s):
    return html.escape(str(s), quote=True)


def nice_ticks(top, count=4, whole=False):
    """0 and round steps up to at least `top`; whole numbers only for counts."""
    if not top or top <= 0:
        return [0, 1]
    raw = top / count
    mag = 10 ** math.floor(math.log10(raw))
    steps = (1, 2, 5, 10) if whole else (1, 2, 2.5, 5, 10)
    step = next(m * mag for m in steps if m * mag >= raw)
    if whole:
        step = max(1, step)
    n = math.ceil(top / step)
    return [round(i * step, 10) for i in range(n + 1)]


def tick_label(v):
    if abs(v) >= 10000:
        return f"{v / 1000:g}K"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:g}"


def _grid(ticks, y_of, x0, x1):
    out = []
    for t in ticks:
        y = y_of(t)
        out.append(f'<line class="grid{" base" if t == 0 else ""}" x1="{x0}" x2="{x1}" y1="{y:.1f}" y2="{y:.1f}"/>')
        out.append(f'<text class="tick" x="{x0 - 5}" y="{y + 3.5:.1f}" text-anchor="end">{esc(tick_label(t))}</text>')
    return out


def columns(points, *, height=150, title="", slot=1, unit=""):
    """One series of columns. points: [{"label": x label or "", "value": n, "tip": text}]."""
    if not points:
        return Markup('<p class="muted">No data yet.</p>')
    top = max(p["value"] for p in points) or 1
    ticks = nice_ticks(top, whole=all(float(p["value"]).is_integer() for p in points))
    x0, x1, y0, y1 = PAD_L, W - PAD_R, PAD_T, height - PAD_B
    y_of = lambda v: y1 - (y1 - y0) * v / ticks[-1]
    slot_w = (x1 - x0) / len(points)
    bw = min(24, max(2, slot_w - 2))        # 2 px surface gap between touching columns
    parts = _grid(ticks, y_of, x0, x1)
    peak = max(range(len(points)), key=lambda i: points[i]["value"])
    for i, p in enumerate(points):
        cx = x0 + slot_w * (i + 0.5)
        x, y = cx - bw / 2, y_of(p["value"])
        if p["value"] > 0:
            r = min(4, bw / 2, y1 - y)
            parts.append(f'<path class="mark s{slot}" d="M{x:.1f},{y1:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}'
                         f'H{x + bw - r:.1f}Q{x + bw:.1f},{y:.1f} {x + bw:.1f},{y + r:.1f}V{y1:.1f}Z"/>')
        parts.append(f'<rect class="hit" x="{cx - slot_w / 2:.1f}" y="{y0}" width="{slot_w:.1f}" height="{y1 - y0}" '
                     f'tabindex="0" data-tip="{esc(p.get("tip") or p["value"])}"/>')
        if p.get("label"):
            parts.append(f'<text class="tick" x="{cx:.1f}" y="{height - 6}" text-anchor="middle">{esc(p["label"])}</text>')
        if i == peak and p["value"] > 0:      # one direct label: the peak
            parts.append(f'<text class="val" x="{cx:.1f}" y="{y - 4:.1f}" text-anchor="middle">'
                         f'{esc(tick_label(p["value"]))}{esc(unit)}</text>')
    return _svg(parts, height, title)


def lines(series, *, height=170, title="", x_labels=None, y_max=None, unit="", step=False):
    """Lines over time. series: [{"name", "slot", "points": [(x, y), ...]}] with x as epoch seconds.
    x_labels: [(x, text)] for the bottom axis. A legend (HTML) comes with two or more series."""
    series = [s for s in series if s["points"]]
    if not series:
        return Markup('<p class="muted">No data yet.</p>')
    xs = [x for s in series for x, _ in s["points"]]
    ys = [y for s in series for _, y in s["points"] if y is not None]
    xmin, xmax = min(xs), max(xs)
    if xmax == xmin:
        xmax = xmin + 1
    ticks = nice_ticks(y_max if y_max is not None else (max(ys) if ys else 1))
    x0, x1, y0, y1 = PAD_L, W - PAD_R - (44 if 1 < len(series) <= 4 else 0), PAD_T, height - PAD_B
    x_of = lambda x: x0 + (x1 - x0) * (x - xmin) / (xmax - xmin)
    y_of = lambda v: y1 - (y1 - y0) * v / ticks[-1]
    parts = _grid(ticks, y_of, x0, x1)
    for x, text in x_labels or []:
        if xmin <= x <= xmax:
            parts.append(f'<text class="tick" x="{x_of(x):.1f}" y="{height - 6}" text-anchor="middle">{esc(text)}</text>')
    ends = []
    for s in series:
        pts = [(x_of(x), y_of(y)) for x, y in s["points"] if y is not None]
        if step:     # a value holds until the next one (prices)
            path = f"M{pts[0][0]:.1f},{pts[0][1]:.1f}" + "".join(f"H{px:.1f}V{py:.1f}" for px, py in pts[1:])
        else:
            path = "M" + "L".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        if len(series) == 1:      # one series: a light wash under the line
            parts.append(f'<path class="wash s{s["slot"]}" d="{path}V{y1}H{pts[0][0]:.1f}Z"/>')
        parts.append(f'<path class="line s{s["slot"]}" d="{path}"/>')
        ex, ey = pts[-1]
        parts.append(f'<circle class="dot s{s["slot"]}" cx="{ex:.1f}" cy="{ey:.1f}" r="4"/>')
        ends.append((ey, s, ex))
    # End labels for 2 to 4 series, only when they don't collide (else the legend and the crosshair carry it).
    if 1 < len(series) <= 4:
        ys_sorted = sorted(e[0] for e in ends)
        if all(b - a >= 12 for a, b in zip(ys_sorted, ys_sorted[1:])):
            for ey, s, ex in ends:
                parts.append(f'<text class="end" x="{ex + 7:.1f}" y="{ey + 3.5:.1f}">{esc(s["name"])}</text>')
    data = {"x0": x0, "x1": x1, "xmin": xmin, "xmax": xmax, "unit": unit,
            "series": [{"name": s["name"], "slot": s["slot"], "points": s["points"]} for s in series]}
    parts.append(f'<line class="cross" x1="0" x2="0" y1="{y0}" y2="{y1}" visibility="hidden"/>')
    parts.append(f'<rect class="hit-area" x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}"/>')
    legend = ""
    if len(series) > 1:
        legend = '<div class="legend">' + "".join(
            f'<span><i class="key line-key s{s["slot"]}"></i>{esc(s["name"])}</span>' for s in series) + "</div>"
    return Markup(legend) + _svg(parts, height, title, data=json.dumps(data))


def _svg(parts, height, title, data=None):
    attr = f' data-lines="{esc(data)}"' if data else ""
    label = f' aria-label="{esc(title)}"' if title else ""
    return Markup(f'<svg class="chart" viewBox="0 0 {W} {height}" role="img"{label}{attr}>' + "".join(parts) + "</svg>")


def meter(done, total, *, slot=1, label=""):
    """A thin progress bar: done of total."""
    share = 0 if not total else max(0.0, min(1.0, done / total))
    tip = f"{label}: {done:,} of {total:,} ({share:.0%})" if label else f"{done:,} of {total:,}"
    return Markup(f'<span class="meter s{slot}" role="meter" aria-valuenow="{done}" aria-valuemax="{total}" '
                  f'aria-label="{esc(tip)}" data-tip="{esc(tip)}" tabindex="0">'
                  f'<span style="width:{share * 100:.1f}%"></span></span>')
