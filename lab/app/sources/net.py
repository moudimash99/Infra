"""Network (lab.machaka.net/machines/network): Grafana's Traefik dashboard, the site checks and the machines'
network traffic, merged.

  Sites         every address Traefik serves (the public sites, Grafana, the lab, the menu): is it up (the lab's own
                check), how many requests today, how many failed (5xx) or were refused (4xx), how fast it answers
                (the share under 300 ms, the slowest 5%)
  Over time     requests a minute, errors a minute and the share of slow answers, by site
  Traffic       each machine's network card in and out, and the apps that moved the most (pods on the host's own
                network are left out: they see the whole machine's traffic, not their own)

Traefik's names are <namespace>-<service>-<port>@kubernetes; the site checks and services.yaml use the app's name.
"""
import concurrent.futures
import re
import time

from . import misc, prom, source
from .memory import MACHINE_NAMES, app_name
from .overview import SITE_SERVICE, Projects

RANGES = {"24h": (24, 300, "5m"), "7d": (168, 1800, "30m")}
NET = 'device=~"(en|eth|wl|ww).*"'
SVC = 'traefik_service_requests_total'
POD_NET = ('sum by (namespace, pod) (increase(container_network_{d}_bytes_total{{job="kubelet-cadvisor",pod!=""}}[24h])) '
           'unless on (namespace, pod) kube_pod_info{{host_network="true"}}')
# A site's link is its services.yaml address (catalog.json); the lab serves several, so it links to this tool.
ADDRESSES = {"lab": "https://lab.machaka.net/machines/"}
NAMES = {"website": "machaka.net", "cat-weight": "Cat weight", "unmatched": "Unmatched", "boardxplorer": "BoardXplorer",
         "freemotion-hub": "FreeMotion", "spirit-island": "Spirit Island", "grafana": "Grafana",
         "homepage": "The lab menu", "lab": "Lab sites (Taste Match, Deals, Cat cam, Homelab)", "beszel": "Beszel",
         "mail-review-button": "Mail review button", "uptime-kuma": "Uptime Kuma", "portainer": "Portainer"}

Q = {
    "req_24h": f"sum by (service) (increase({SVC}[24h]))",
    "req_1h": f"sum by (service) (increase({SVC}[1h]))",
    "err_24h": f'sum by (service) (increase({SVC}{{code=~"5.."}}[24h]))',
    "err_1h": f'sum by (service) (increase({SVC}{{code=~"5.."}}[1h]))',
    "refused_24h": f'sum by (service) (increase({SVC}{{code=~"4.."}}[24h]))',
    "rpm": f"sum by (service) (rate({SVC}[5m])) * 60",
    "fast_24h": ('sum by (service) (increase(traefik_service_request_duration_seconds_bucket{le="0.3"}[24h])) / '
                 'sum by (service) (increase(traefik_service_request_duration_seconds_count[24h]))'),
    "p95_24h": ('histogram_quantile(0.95, sum by (service, le) '
                '(rate(traefik_service_request_duration_seconds_bucket[24h])))'),
    "p50_24h": ('histogram_quantile(0.5, sum by (service, le) '
                '(rate(traefik_service_request_duration_seconds_bucket[24h])))'),
    "codes_24h": f"sum by (code) (increase({SVC}[24h]))",
    "conns": "sum(traefik_open_connections)",
    "net_rx": f"sum by (instance) (rate(node_network_receive_bytes_total{{{NET}}}[5m]))",
    "net_tx": f"sum by (instance) (rate(node_network_transmit_bytes_total{{{NET}}}[5m]))",
    "net_rx_24h": f"sum by (instance) (increase(node_network_receive_bytes_total{{{NET}}}[24h]))",
    "net_tx_24h": f"sum by (instance) (increase(node_network_transmit_bytes_total{{{NET}}}[24h]))",
    "pod_rx_24h": POD_NET.format(d="receive"),
    "pod_tx_24h": POD_NET.format(d="transmit"),
    "hostnet": 'kube_pod_info{host_network="true"}',
}


def ranged(w):
    return {
        "rpm": f"sum by (service) (rate({SVC}[{w}])) * 60",
        "err": f'sum by (service) (rate({SVC}{{code=~"5.."}}[{w}])) * 60',
        "slow": ('1 - (sum by (service) (rate(traefik_service_request_duration_seconds_bucket{le="0.3"}[' + w + '])) / '
                 'sum by (service) (rate(traefik_service_request_duration_seconds_count[' + w + '])))'),
        "net_rx": f"sum by (instance) (rate(node_network_receive_bytes_total{{{NET}}}[{w}]))",
        "net_tx": f"sum by (instance) (rate(node_network_transmit_bytes_total{{{NET}}}[{w}]))",
    }


def fetch(range_key):
    hours, step, w = RANGES[range_key]
    raw, errors = {"r": {}}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = {("i", k): pool.submit(prom.query, e) for k, e in Q.items()}
        futs.update({("r", k): pool.submit(prom.query_range, e, hours, step) for k, e in ranged(w).items()})
        for (kind, key), fut in futs.items():
            try:
                res = fut.result()
            except Exception as e:          # noqa: BLE001 - one gap, not a broken page
                res = []
                errors.append(e)
            (raw if kind == "i" else raw["r"])[key] = res
    if len(errors) > len(futs) // 2:
        raise errors[0]
    checks = misc.sites.get()
    raw["checks"] = checks.data if checks.ok else None
    raw["at"] = time.time()
    return raw


def split(service):
    """apps-cat-weight-http@kubernetes -> ("apps", "cat-weight")."""
    name = service.split("@")[0]
    ns, _, rest = name.partition("-")
    app = re.sub(r"-(http|https|page|web|ui|port|[0-9]+)$", "", rest)
    return ns, app


def _num(v):
    return None if v is None or v != v or v in (float("inf"), float("-inf")) else v     # NaN, +Inf -> None


def build(raw, catalog=None, range_key="24h"):
    P = Projects(catalog or {})
    by = lambda key: {m.get("service"): _num(v) for m, v in raw.get(key, [])}
    req, req1, err, err1, refused, rpm, fast, p95, p50 = (by(k) for k in (
        "req_24h", "req_1h", "err_24h", "err_1h", "refused_24h", "rpm", "fast_24h", "p95_24h", "p50_24h"))
    checks = {SITE_SERVICE.get(c["name"]): c for c in (raw.get("checks") or [])}
    sites = []
    for svc in sorted(set(req) | set(rpm)):
        ns, app = split(svc)
        check = checks.get(app)
        n24, e24, e1, n1 = req.get(svc) or 0, err.get(svc) or 0, err1.get(svc) or 0, req1.get(svc) or 0
        share_fast = fast.get(svc)
        slow95 = p95.get(svc)
        if check and check.get("up") is False:
            state, why = "bad", f"down: answered {check.get('code') or check.get('error') or 'nothing'}"
        elif n1 >= 20 and e1 / n1 > 0.05:
            state, why = "bad", f"{e1 / n1:.0%} of the last hour's requests failed"
        elif n24 and e24 / n24 > 0.01:
            state, why = "warn", f"{e24:.0f} requests failed today"
        elif (slow95 or 0) >= 1.2:
            state, why = "warn", ""            # slow: the line's own "the slowest 5% in ..." says so
        else:
            state, why = "ok", ""
        entry = P.obj(ns, app)
        url = ADDRESSES.get(app) or (P.services.get(entry["service"], {}).get("url") if entry else None)
        sites.append({
            "service": svc, "ns": ns, "app": app, "name": NAMES.get(app, app), "url": url,
            "p": P.group(ns, app), "state": state, "why": why, "requests": n24, "errors": e24,
            "refused": refused.get(svc) or 0, "rpm": rpm.get(svc) or 0, "fast": share_fast, "p95": slow95,
            "p50": p50.get(svc), "check": check,
        })
    order = {"bad": 0, "warn": 1, "ok": 2}
    sites.sort(key=lambda s: (order[s["state"]], -s["requests"]))
    for c in raw.get("checks") or []:              # a checked site Traefik doesn't serve (FreeMotion's own tunnel)
        app = SITE_SERVICE.get(c["name"])
        if not any(s["app"] == app for s in sites) and c.get("up") is not None:
            sites.append({"service": "", "ns": "", "app": app, "name": c["name"], "url": c.get("url"),
                          "p": P.services.get(app, {}).get("group", ""), "state": "ok" if c.get("up") else "bad",
                          "why": "" if c.get("up") else f"down: answered {c.get('code') or c.get('error')}",
                          "requests": None, "errors": None, "refused": None, "rpm": None, "fast": None, "p95": None,
                          "p50": None, "check": c})

    codes = sorted(((m.get("code", "?"), v) for m, v in raw.get("codes_24h", []) if v >= 0.5), key=lambda c: c[0])
    classes = {}
    for code, v in codes:
        classes[code[0] + "xx"] = classes.get(code[0] + "xx", 0) + v

    # machines
    rx, tx = ({m.get("instance"): v for m, v in raw.get(k, [])} for k in ("net_rx", "net_tx"))
    rx24, tx24 = ({m.get("instance"): v for m, v in raw.get(k, [])} for k in ("net_rx_24h", "net_tx_24h"))
    machines = [{"node": n, "name": MACHINE_NAMES.get(n, n), "rx": rx.get(n, 0), "tx": tx.get(n, 0),
                 "rx_24h": rx24.get(n), "tx_24h": tx24.get(n)} for n in MACHINE_NAMES if n in rx]
    movers = {}
    hostnet = {(m.get("namespace"), app_name(m.get("pod", ""))) for m, _ in raw.get("hostnet", [])}
    for key, direction in (("pod_rx_24h", "rx"), ("pod_tx_24h", "tx")):
        for m, v in raw.get(key, []):
            if (m.get("namespace"), app_name(m.get("pod", ""))) in hostnet:
                continue                    # an older pod of an app on the host's network (its new pod is left out)
            a = movers.setdefault((m.get("namespace"), app_name(m.get("pod", ""))), {"rx": 0, "tx": 0})
            a[direction] += v
    movers = sorted(({"ns": ns, "name": app, "rx": d["rx"], "tx": d["tx"], "p": P.group(ns, app)}
                     for (ns, app), d in movers.items() if d["rx"] + d["tx"] > 1024 ** 2),
                    key=lambda a: -(a["rx"] + a["tx"]))

    # graphs
    R = raw.get("r", {})
    hours, step, _ = RANGES[range_key]
    def by_site(key, scale=1.0, most=5, at_least=0.0, cover=0.0):
        """Sites as lines; cover: the share of the range a site needs points for (a share of slow answers has no
        point when nobody asked, and a line drawn across those gaps would invent a trend)."""
        named = {}
        for labels, pts in R.get(key, []):
            name = NAMES.get(split(labels.get("service", ""))[1], split(labels.get("service", ""))[1])
            pts = [(t, _num(v) * scale) for t, v in pts if _num(v) is not None]
            if pts and max(v for _, v in pts) > at_least and len(pts) >= cover * hours * 3600 / step:
                named[name] = pts
        ranked = sorted(named.items(), key=lambda kv: -sum(v for _, v in kv[1]))[:most]
        return [{"name": n, "slot": i + 1, "points": p} for i, (n, p) in enumerate(ranked)]
    traffic = []
    for i, m in enumerate(machines):
        for key, word, slot in (("net_rx", "in", 1 + 2 * i), ("net_tx", "out", 2 + 2 * i)):
            pts = next((p for labels, p in R.get(key, []) if labels.get("instance") == m["node"]), [])
            if pts:
                traffic.append({"name": f"{m['name']} {word}", "slot": slot, "points": [(t, v / 1024 ** 2) for t, v in pts]})
    graphs = {"rpm": by_site("rpm"), "err": by_site("err", at_least=0.001), "slow": by_site("slow", 100, at_least=1, cover=0.5),
              "traffic": traffic}
    return {"sites": sites, "codes": codes, "classes": classes, "conns": next((v for _, v in raw.get("conns", [])), None),
            "machines": machines, "movers": movers, "graphs": graphs, "groups": P.groups, "range": range_key,
            "at": raw.get("at"), "total": sum(s["requests"] or 0 for s in sites)}


@source("net.raw", ttl=60)
def read(range_key):
    return fetch(range_key)
