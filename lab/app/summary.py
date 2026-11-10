"""Short lines for Homepage's tiles on lab.machaka.net (its customapi widget reads these, inside the cluster)."""
import time

from .fmt import ago
from .sources import bgf, bgt, k8s


def _pct(part, whole):
    return f"{round(100 * part / whole)}%" if whole else "-"


# Taste Match's tile row per stage: (row, stage key in the progress totals, the CronJob that does it).
STAGE_ROWS = [("download", "audio", "bg-taste-match-fetch"), ("transcribe", "text", "bg-taste-match-gpu"),
              ("speakers", "speakers", "bg-taste-match-gpu"), ("rate", "rated", "bg-taste-match-llm")]


def _running(run):
    """The row a running pass belongs to and what it is on: ('download', 'now The Dice Tower 30/537')."""
    st = (run.get("log") or {}).get("now")
    if run["kind"] == "bg-taste-match-fetch":
        row = "download"
    elif run["kind"] == "bg-taste-match-llm":
        row = "rate"
    else:
        row = "speakers" if st and st["what"].startswith("speaker") else "transcribe"
    if not st:
        return row, "starting"
    what = st.get("label") or st["what"]
    for verb in ("Downloading ", "Whisper on ", "Rating "):     # the row already names the stage
        what = what.removeprefix(verb)
    what = "running" if row == "speakers" else f"now {what}"   # speaker separation takes every channel in one pass
    return row, what + (f" {st['pos'][0]}/{st['pos'][1]}" if st.get("pos") else "")


def taste_match():
    """Each stage: how far this round is (% of its videos) and what the stage is doing, when that's news."""
    prog, now = bgt.progress.get(), bgt.now.get()
    state = {}
    if now.ok and now.data:
        runs, cron = now.data["runs"], now.data["cron"]
        for row, _, job in STAGE_ROWS:
            last = next((r for r in runs if r["kind"] == job and r["state"] in ("ok", "failed")), None)
            if cron.get(job) and cron[job]["suspended"]:
                state[row] = "paused"
            elif last and last["state"] == "failed":
                state[row] = "last pass failed"
        if now.data["youtube_blocked"]:
            state["download"] = "YouTube blocking"
        for r in runs:
            if r["state"] == "running":
                row, what = _running(r)
                state[row] = what
    out = {row: state.get(row, "-") for row, _, _ in STAGE_ROWS}
    if prog.ok and prog.data:
        t = prog.data["totals"]
        for row, key, _ in STAGE_ROWS:
            out[row] = " · ".join(filter(None, [_pct(t[key], t["videos"]), state.get(row)]))
    return out


def deals():
    d, fresh, b, m = bgf.deals.get(), bgf.new_deals.get(), bgf.bundles.get(), bgf.market.get()
    out = {"best": "-", "deals": "-", "new": "-", "bundles": "-"}
    if d.ok and d.data and d.data["deals"]:
        top = d.data["deals"][0]
        out["best"] = f"{top['name'][:22]} {top['price']:.0f} € (−{top['net']:.0f})"
        out["deals"] = str(len(d.data["deals"]))
    if fresh.ok and fresh.data:
        out["new"] = str(len(fresh.data["deals"])) if fresh.data["available"] else "from tomorrow"
    if b.ok and b.data:
        out["bundles"] = str(len(b.data["items"]))
    if m.ok and m.data and (m.data["live"] == 0 or (m.data["last_scrape"] and time.time() - m.data["last_scrape"] > 30 * 3600)):
        out["best"] = "last scrape failed"
    out["run"] = _job_line("boardgamefinder", _failed_steps)
    return out


def _failed_steps(run):
    """BoardGameFinder goes on when a step fails: count them from its run's log."""
    r = bgf.run.get()
    detail = (r.data or {}).get("detail") if r.ok else None
    n = sum(1 for s in (detail or {}).get("steps") or [] if s.get("failed"))
    return f"{n} step{'s' if n > 1 else ''} failed" if n else ""


def _job_line(cronjob, extra=None):
    """'paused', 'running now', 'today 07:27 · ok', 'today 07:27 · 2 steps failed', from Kubernetes."""
    j = k8s.jobs.get()
    if not j.ok or not j.data:
        return "-"
    c = j.data["cronjobs"].get(cronjob)
    runs = [x for x in j.data["jobs"] if x["cronjob"] == cronjob]
    if c and c["suspended"]:
        return "paused"
    if not runs:
        return "no run yet"
    last = runs[0]
    if last["state"] == "running":
        return "running now"
    note = (extra(last) if extra else "") or {"ok": "ok", "failed": "failed"}.get(last["state"], last["state"])
    return f"{ago(last['start'])} · {note}"


def catcam():
    from .sources import cats
    out = {"flash": "first one tonight", "weighed": "-"}
    f = cats.flashes(1)
    if f:
        out["flash"] = f"{f[0]['day'][5:]}: {f[0].get('moments', 0)} moments, {round(f[0].get('seconds', 0))} s"
    w = cats.weights.get()
    if w.ok and w.data:      # a weigh-in's time of day doesn't matter: '16 days ago'
        days = int((time.time() - max(p[0] for pts in w.data.values() for p in pts)) // 86400)
        out["weighed"] = "today" if days < 1 else "yesterday" if days < 2 else f"{days} days ago"
    out["monitor"] = _job_line("cat-monitor")
    return out
