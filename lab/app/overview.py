"""The front page: what needs the owner, one card per job, the machines.

"Needs you" comes first from Alertmanager (the same rules that email), so the page and the emails agree; the lab
adds only what no alert covers (a source it can't read, YouTube blocking right now, the PC's jobs).
"""
import time

from .config import PC_STALE_MINUTES
from .fmt import ago, span, to_ts
from .sources import bgf, bgt, cats, k8s, misc, prom

ALERT_PAGES = [("BgTasteMatch", "/bg-taste-match"), ("bg-taste-match", "/bg-taste-match"),
               ("BoardGameFinder", "/deals"), ("boardgamefinder", "/deals"), ("CatMonitor", "/cats"),
               ("cat-monitor", "/cats"), ("Gpu", "/gpu"), ("GPU", "/gpu"), ("Nvidia", "/gpu"), ("Frigate", "/cats")]
GPU_SYSTEM_APPS = {"nvidia-gpu-exporter", "beszel-agent", "beszel"}


def _page_for(alert):
    text = " ".join([alert["name"], *map(str, alert["labels"].values())])
    return next((href for key, href in ALERT_PAGES if key in text), "/gpu" if "Acer" in text else None)


def attention():
    """[{level: critical|warning|info, title, detail, since, href}], worst first."""
    items = []
    al = prom.alerts.get()
    if al.ok:
        for a in al.data:
            items.append({"level": "critical" if a["severity"] == "critical" else "warning", "title": a["name"],
                          "detail": a["summary"], "since": to_ts(a["since"]), "href": _page_for(a)})
    else:
        items.append({"level": "warning", "title": "Alerts unreadable", "detail": al.error, "since": None,
                      "href": None})
    names = {i["title"] for i in items}
    now = bgt.now.get()
    if now.ok and now.data["youtube_blocked"] and "BgTasteMatchDownloadsStalled" not in names:
        items.append({"level": "warning", "title": "YouTube is refusing downloads",
                      "detail": "The last download pass stopped after 5 refusals in a row; it retries every 2 hours.",
                      "since": None, "href": "/bg-taste-match"})
    sites = misc.sites.get()
    for s in sites.data or []:
        if s["up"] is False:
            items.append({"level": "critical", "title": f"{s['name']} is down",
                          "detail": s.get("error") or f"HTTP {s['code']}", "since": None, "href": s["url"]})
    pc = misc.pc.get()
    if pc.ok and pc.data:
        if pc.data["stale"]:
            items.append({"level": "info", "title": "PC offline or asleep",
                          "detail": "Its jobs aren't reporting.", "since": pc.data["received"], "href": "/pc"})
        else:
            for t in pc.data["body"].get("tasks", []):
                if t.get("status") == "failed":
                    items.append({"level": "warning", "title": f"PC: {t['name']} failed",
                                  "detail": t.get("note", ""), "since": to_ts(t.get("last_run")), "href": "/pc"})
    # Notes: true and worth knowing, but nothing to do right now.
    if now.ok and now.data["gpu_paused"]:
        items.append({"level": "info", "title": "GPU jobs paused",
                      "detail": "Whisper and speaker separation are suspended; downloads continue.", "since": None,
                      "href": "/bg-taste-match"})
    ev = cats.events.get()
    if ev.ok:
        last = ev.data[0]["start"] if ev.data else None
        if not last or time.time() - last > 3 * 86400:
            items.append({"level": "info", "title": "No cat seen by Frigate " + (f"since {ago(last)}" if last else
                                                                                 "in 14 days"),
                          "detail": "Frigate's detector rarely catches the cats; check a frame on the cats page.",
                          "since": last, "href": "/cats"})
    # Failed runs: the newest finished run of each scheduled job, when it failed (the JobFailed alert covers some, not
    # the downloads, and an alert clears; this stays until a run succeeds).
    jobs = k8s.jobs.get()
    if jobs.ok:
        for name, cron in jobs.data["cronjobs"].items():
            runs = [r for r in jobs.data["jobs"] if r["cronjob"] == name and r["state"] in ("ok", "failed")]
            if runs and runs[0]["state"] == "failed" and not (name == "bg-taste-match-fetch" and now.ok
                                                               and now.data["youtube_blocked"]):
                pod = k8s.latest_pod(runs[0]["name"])
                why = (pod or {}).get("reason") or runs[0]["reason"] or "failed"
                items.append({"level": "warning", "title": f"{name}: last run failed ({why})",
                              "detail": "OOMKilled means it ran out of memory." if why == "OOMKilled" else "",
                              "since": runs[0]["failed_at"] or runs[0]["start"], "href": _page_for(
                                  {"name": name, "labels": {}}) or "/gpu"})
    for src in (k8s.jobs, k8s.pods, k8s.nodes, bgt.now, bgt.progress, bgf.deals, bgf.market, cats.events, cats.monitor,
                cats.weights, misc.mail_classifier, prom.machines):
        r = src.get()
        if not r.ok:
            items.append({"level": "warning", "title": f"Lab can't read {src.name}", "detail": r.error,
                          "since": None, "href": None})
    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(items, key=lambda i: (order[i["level"]], -(i["since"] or 0)))


def _cron(name):
    j = k8s.jobs.get().data or {}
    return (j.get("cronjobs") or {}).get(name)


def _last(name):
    runs = k8s.runs_of(name)
    running = next((r for r in runs if r["state"] == "running"), None)
    done = next((r for r in runs if r["state"] in ("ok", "failed")), None)
    return running, done


def job_cards():
    cards = []

    # bg-taste-match: three stages in one card.
    prog, now = bgt.progress.get(), bgt.now.get()
    card = {"title": "BG Taste Match", "href": "/bg-taste-match", "state": "ok" if (prog.ok and now.ok) else "unknown",
            "lines": [], "meter": None, "note": None,
            "error": None if (prog.ok and now.ok) else (prog.error or now.error)}
    if now.ok:
        running = [r for r in now.data["runs"] if r["state"] == "running"]
        if running:
            r = running[0]
            st = r["log"]["now"]
            what = st.get("label") or st["what"] if st else r["label"]
            pos = f" ({st['pos'][0]} of {st['pos'][1]})" if st and st.get("pos") else ""
            card["state"], card["status"] = "running", f"{what}{pos}"
        elif now.data["youtube_blocked"]:
            card["state"], card["status"] = "failed", "YouTube is refusing downloads"
        else:
            card["status"] = "Idle between passes"
        if now.data["gpu_paused"]:
            card["note"] = "GPU jobs paused"
            if card["state"] == "ok":
                card["state"] = "paused"
    if prog.ok:
        t, sp = prog.data["totals"], prog.data["speed"]
        card["meter"] = (t["rated"], t["videos"], "rated")
        card["lines"] = [
            ("Audio", f"{t['audio']:,} of {t['videos']:,} videos"),
            ("Whisper left", f"{t['hours_to_transcribe']:.0f} h of audio"),
            ("Last 24 h", f"{sp['downloads_24h']} downloaded, {sp['transcribed_24h']} transcribed"),
        ]
    cards.append(card)

    # BoardGameFinder.
    run, deals, market = bgf.run.get(), bgf.deals.get(), bgf.market.get()
    jobs_ok = k8s.jobs.get().ok
    card = {"title": "BoardGameFinder", "href": "/deals", "state": "ok" if (deals.ok and jobs_ok) else "unknown",
            "lines": [], "meter": None, "note": None, "error": None if deals.ok else deals.error}
    running, done = _last("boardgamefinder")
    cron = _cron("boardgamefinder")
    if running:
        card["state"], card["status"] = "running", f"Running since {ago(running['start'])}"
    elif done:
        card["state"] = "ok" if done["state"] == "ok" else "failed"
        card["status"] = f"Last run {ago(done['start'])}, took {span((done['end'] or done.get('failed_at') or time.time()) - done['start'])}"
    if cron and cron["suspended"]:
        card["state"] = "paused"
    if run.ok:
        failing = [s["label"] for s in run.data["steps"] if s["failures"]]
        if failing:
            card["note"] = f"{len(failing)} steps failing: " + ", ".join(failing)
    if deals.ok:
        n_new = sum(1 for d in deals.data["deals"] if d["first_seen"] and time.time() - d["first_seen"] < 86400)
        card["lines"].append(("Deals", f"{len(deals.data['deals'])} ({n_new} new today)"))
    if market.ok:
        card["lines"].append(("Live listings", f"{market.data['live']:,} across {market.data['tracked']} games"))
        card["lines"].append(("New in 24 h", f"{market.data['n_new']} listings, {market.data['n_drops']} price drops"))
        if market.data["live"] == 0 or (market.data["last_scrape"] and time.time() - market.data["last_scrape"] > 30 * 3600):
            card["state"], card["note"] = "failed", "The last scrape failed or didn't run: listings aren't current" 
    cards.append(card)

    # Cat monitor + Frigate.
    mon, ev, w = cats.monitor.get(), cats.events.get(), cats.weights.get()
    card = {"title": "Cats", "href": "/cats", "state": "ok" if jobs_ok else "unknown", "lines": [], "meter": None,
            "note": "Feeding can't be measured: the bowl tag is missing", "error": None}
    running, done = _last("cat-monitor")
    cron = _cron("cat-monitor")
    if running:
        card["state"], card["status"] = "running", f"Monitor running since {ago(running['start'])}"
    elif done:
        card["state"] = "ok" if done["state"] == "ok" else "failed"
        card["status"] = f"Monitor ran {ago(done['start'])}"
    if cron and cron["suspended"]:
        card["state"], card["status"] = "paused", "Monitor paused"
    if ev.ok:
        last = ev.data[0]["start"] if ev.data else None
        card["lines"].append(("Frigate saw a cat", ago(last) if last else "not in 14 days"))
    if w.ok and w.data:
        latest = max(p[0] for pts in w.data.values() for p in pts)
        card["lines"].append(("Weights", " · ".join(f"{c} {pts[-1][1]:.2f} kg" for c, pts in sorted(w.data.items()))))
        card["lines"].append(("Last weigh-in", ago(latest)))
    cards.append(card)

    # Mail classifier (always on, a Deployment).
    mail = misc.mail_classifier.get()
    card = {"title": "Mail classifier", "href": None, "state": "ok" if mail.ok else "failed", "lines": [],
            "meter": None, "note": None, "error": None if mail.ok else mail.error,
            "status": "Shadow mode: suggests, the archiver decides"}
    if mail.ok:
        today, week = mail.data["today"], mail.data["week"]
        card["lines"] = [("Today", ", ".join(f"{v} {k}" for k, v in sorted(today.items()) if v) or "nothing yet"),
                         ("7 days", f"{sum(week.values()):,} emails"),
                         ("Model", str(mail.data["health"].get("model_version") or mail.data["health"].get("model", "?")))]
    cards.append(card)

    # The PC's jobs (reported every 10 minutes by the PC).
    pc = misc.pc.get()
    card = {"title": "PC jobs", "href": "/pc", "state": "ok", "lines": [], "meter": None, "note": None, "error": None}
    if not pc.ok or not pc.data:
        card["state"], card["status"] = "unknown", "The PC hasn't reported yet"
    else:
        body = pc.data["body"]
        if pc.data["stale"]:
            card["state"], card["status"] = "unknown", f"PC offline since {ago(pc.data['received'])}"
        else:
            bad = [t for t in body.get("tasks", []) if t.get("status") == "failed"]
            card["state"] = "failed" if bad else "ok"
            card["status"] = f"{len(bad)} failing" if bad else f"All {len(body.get('tasks', []))} jobs fine"
        for k, v in (body.get("headline") or {}).items():
            card["lines"].append((k, v))
    cards.append(card)
    return cards


def gpu_users():
    """Pods using the NVIDIA GPU right now: running pods with the nvidia runtime, minus the exporters."""
    pods = k8s.pods.get().data or []
    return [p for p in pods if p["runtime"] == "nvidia" and p["phase"] == "Running"
            and (p["app"] or "") not in GPU_SYSTEM_APPS and p["namespace"] == "apps"]


def machines():
    nodes, m, gpu = k8s.nodes.get(), prom.machines.get(), prom.gpu.get()
    out = []
    for n in nodes.data or []:
        stats = (m.data or {}).get(n["name"], {})
        row = {"name": n["name"], "ready": n["ready"], "ip": n["ip"], **stats,
               "role": "control plane" if n["name"] == "moudimash99" else "worker"}
        if n["name"] == "acer-laptop" and gpu.ok and gpu.data:
            row["gpu"] = {**gpu.data, "users": gpu_users()}
        out.append(row)
    return {"rows": out, "error": None if (nodes.ok and m.ok) else (nodes.error or m.error)}
