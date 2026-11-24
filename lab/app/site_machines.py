"""lab.machaka.net/machines: where each machine's memory goes, and who is using it.

  /    one card per machine (a bar split into pods, k3s, Docker, other, kernel, cache, free; its lowest point in the
       last 24 h and who was using memory then), and one table of everything using memory, to search, sort and filter

It sits under lab.machaka.net (Homepage's address, already behind the Cloudflare login; Traefik sends /machines here),
so links and static files start with R, the site's root ("/machines").
"""
from fastapi import Request
from fastapi.responses import HTMLResponse
from markupsafe import Markup

from .sources import memory
from .web import page, site

app = site("machines", [("/", "Memory", "layers")])

PARTS = [  # key, label, colour slot, what it is (shown under the bar)
    ("pods", "Kubernetes apps", "s1", "the pods: Free Motion, Frigate, Grafana, the jobs..."),
    ("k3s", "k3s itself", "s7", "Kubernetes and containerd"),
    ("docker", "Docker", "s2", "the server's Docker containers"),
    ("other", "Other programs", "s4", "system services and anything else outside containers"),
    ("kernel", "Kernel and drivers", "s5", "memory the kernel holds for itself (on the Acer, the GPU driver too)"),
    ("cache", "Reusable cache", "cache", "file cache, handed back the moment a program needs it"),
    ("free", "Free", "free", "not used at all"),
]
KINDS = {"pod": "Kubernetes app", "k3s": "k3s itself", "docker": "Docker", "program": "Program"}


def gb(v, digits=1):
    """Bytes as GB (1 GB = 1024³ bytes, like free -h); under 1 GB as MB."""
    if v is None:
        return "–"
    if v >= memory.G * 0.995:
        return f"{v / memory.G:.{digits}f} GB"
    return f"{v / 1024 ** 2:.0f} MB"


def spark(values, width=96, height=24):
    """A small line for a table row: the last 24 hours, scaled to its own peak."""
    pts = [v for v in values if v is not None]
    if len(pts) < 2:
        return Markup("")
    top = max(pts) or 1
    step = width / (len(pts) - 1)
    path = " ".join(f"{'M' if i == 0 else 'L'}{i * step:.1f},{height - 2 - (v / top) * (height - 4):.1f}"
                    for i, v in enumerate(pts))
    return Markup(f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
                  f'aria-hidden="true"><path d="{path}"/></svg>')


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    now = memory.now.get()
    data = now.data or {"machines": [], "rows": []}
    machines = []
    for m in data["machines"]:
        state, sentence = memory.verdict(m)
        machines.append({**m, "state": state, "sentence": sentence, "low": memory.low.get(m["node"])})
    return page(request, "machines/memory.html", reading=now, machines=machines, rows=data["rows"],
                stuck=data.get("stuck", []), PARTS=PARTS,
                KINDS=KINDS, gb=gb, spark=spark, G=memory.G)
