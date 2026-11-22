#!/usr/bin/env python3
"""The Homepage menu (lab.machaka.net), made from services.yaml: one tile per entry, grouped by its system.

Writes k8s/tools/homepage-menu.yaml: ConfigMap tools/homepage-menu with Homepage's services.yaml and settings.yaml
(the rest of Homepage's config stays in k8s/tools/homepage.yaml). After a change to services.yaml:
  python scripts/menu.py            (--check also asks the cluster that every tile's pods exist)
  kubectl apply -f k8s/tools/homepage-menu.yaml && kubectl -n tools rollout restart deploy/homepage

A tile, by default:
  title        the entry's name
  description  the first sentence of `what`
  href         the first https:// address in `url`
  icon         by kind
  pods         from the first Deployment / StatefulSet / DaemonSet probe: <namespace>/app=<name> (the status dot)
  widget       for CronJob probes not paused on purpose: the time since each one's last good run (Prometheus)
An entry's optional `menu:` block overrides any of these (title, description, href, icon, pods, ping = Homepage's
siteMonitor URL, widget; href: null or pods: null for none). hide: true leaves the entry off. extra: [Homepage
tiles, as Homepage writes them] adds tiles after it in the same group.
"""
import os
import re
import subprocess
import sys

import yaml

INFRA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(INFRA, "k8s", "tools", "homepage-menu.yaml")
PROM = "http://prometheus.monitoring.svc"

# Menu groups, top to bottom: (heading, the systems in it, columns). Every system must be in one.
GROUPS = [
    ("Board games", ["board-games"], 3),
    ("Cats", ["cats"], 3),
    ("Job search", ["free-motion", "job-scrapers"], 4),
    ("Gmail", ["gmail"], 3),
    ("Website and lab", ["website", "lab"], 3),
    ("Monitoring", ["monitoring"], 4),
    ("Platform", ["platform"], 3),
]
SETTINGS = {"title": "Homelab", "theme": "dark", "color": "slate", "headerStyle": "clean", "statusStyle": "dot",
            "hideVersion": True}
ICONS = {"web-app": "mdi-web", "scheduled-job": "mdi-timer-outline", "tool": "mdi-tools",
         "monitoring": "mdi-chart-line", "cluster": "mdi-server-network", "pc-task": "mdi-monitor"}


def first_sentence(text, limit=90):
    s = re.split(r"(?<=[.!?])\s", " ".join(str(text or "").split()), maxsplit=1)[0].rstrip(".")
    return s if len(s) <= limit else s[:limit - 1].rsplit(" ", 1)[0] + "…"


def first_https(url):
    for u in url if isinstance(url, list) else [url]:
        m = re.search(r"https://\S+", str(u or ""))
        if m:
            return m.group(0).rstrip(",;)")
    return None


def default_pods(svc):
    for p in svc.get("probes") or []:
        ref = p.get("k8s", "")
        parts = ref.split("/")
        if len(parts) == 3 and parts[1] in ("Deployment", "StatefulSet", "DaemonSet") and "*" not in ref:
            return f"{parts[0]}/app={parts[2]}"
    return None


def job_widget(svc):
    metrics = []
    jobs = [p["k8s"].split("/") for p in svc.get("probes") or []
            if "/CronJob/" in p.get("k8s", "") and not p.get("paused_on_purpose")]
    for ns, _, name in jobs[:4]:
        metrics.append({"label": "Since the last good run" if len(jobs) == 1 else name,
                        "query": f'time() - kube_cronjob_status_last_successful_time{{namespace="{ns}",cronjob="{name}"}}',
                        "format": {"type": "duration"}})
    return {"type": "prometheusmetric", "url": PROM, "refreshInterval": 60000, "metrics": metrics} if metrics else None


def tile(svc):
    m = svc.get("menu") or {}
    t = {}
    desc = m.get("description", first_sentence(svc.get("what")))
    href = m["href"] if "href" in m else first_https(svc.get("url"))
    pods = m["pods"] if "pods" in m else default_pods(svc)
    widget = m["widget"] if "widget" in m else job_widget(svc)
    if href:
        t["href"] = href
    if desc:
        t["description"] = desc
    t["icon"] = m.get("icon", ICONS.get(svc.get("kind"), "mdi-cube-outline"))
    if pods:
        ns, _, sel = pods.partition("/")
        # Homepage draws the pod status only when `app` is set; with a podSelector it finds the pods by the selector,
        # so the name is only a label (without it, no tile showed a status before 2026-11-22).
        t["namespace"], t["app"], t["podSelector"] = ns, svc["name"], sel
    if m.get("ping"):
        t["siteMonitor"] = m["ping"]
    if widget:
        t["widget"] = widget
    return {m.get("title", svc["name"]): t}


def build(services):
    by_system = {}
    for svc in services:
        by_system.setdefault(svc.get("system"), []).append(svc)
    known = {s for _, systems, _ in GROUPS for s in systems}
    missing = sorted(str(s) for s in by_system if s not in known)
    if missing:
        sys.exit(f"systems with no menu group (add them to GROUPS in scripts/menu.py): {', '.join(missing)}")
    menu, layout = [], {}
    for heading, systems, cols in GROUPS:
        tiles = []
        for system in systems:
            for svc in by_system.get(system, []):
                if (svc.get("menu") or {}).get("hide"):
                    continue
                tiles.append(tile(svc))
                tiles += (svc.get("menu") or {}).get("extra") or []
        if tiles:
            menu.append({heading: tiles})
            layout[heading] = {"style": "row", "columns": cols}
    return menu, {**SETTINGS, "layout": layout}


class Literal(str):
    pass


class Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data):   # write shared values out in full, no &id001 anchors
        return True


Dumper.add_representer(Literal, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v, style="|"))


def main():
    services = yaml.safe_load(open(os.path.join(INFRA, "services.yaml"), encoding="utf-8"))
    menu, settings = build(services)
    dump = lambda d: yaml.dump(d, Dumper=Dumper, sort_keys=False, allow_unicode=True, width=200)
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": "homepage-menu", "namespace": "tools"},
          "data": {"settings.yaml": Literal(dump(settings)), "services.yaml": Literal(dump(menu))}}
    head = ("# MADE BY scripts/menu.py from services.yaml: do not edit by hand. Change services.yaml (an entry's\n"
            "# menu: block) or the script, run it, then apply this file and restart Homepage:\n"
            "#   kubectl apply -f k8s/tools/homepage-menu.yaml && kubectl -n tools rollout restart deploy/homepage\n")
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(head + dump(cm))
    n = sum(len(next(iter(g.values()))) for g in menu)
    print(f"{os.path.relpath(OUT, INFRA)}: {n} tiles in {len(menu)} groups")

    if "--check" in sys.argv:
        bad = 0
        for group in menu:
            for t in next(iter(group.values())):
                name, spec = next(iter(t.items()))
                if "podSelector" not in spec:
                    continue
                r = subprocess.run(["kubectl", "--context", "homelab", "-n", spec["namespace"], "get", "pods",
                                    "-l", spec["podSelector"], "-o", "name"], capture_output=True, text=True)
                if r.returncode or not r.stdout.strip():
                    bad += 1
                    print(f"  {name}: no pods for {spec['namespace']}/{spec['podSelector']} {r.stderr.strip()[:120]}")
        print("pods: all found" if not bad else f"pods: {bad} tile(s) find none")


if __name__ == "__main__":
    main()
