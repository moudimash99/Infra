"""The Jobs page's 7 days against saved Prometheus answers: each run's result (fine, failed, out of memory, running),
the day squares (a few failed runs among many make a day amber), the graphs (hourly peaks, five jobs at most), the
PC's tasks, and the page rendering. Run: PYTHONPATH=. python -m pytest -q tests/test_jobs.py (from lab/)."""
import time

from app.sources import Reading, jobruns, overview
from tests.test_machines import G, s
from tests.test_status import CATALOG, NOW, raw

H = 3600


def week_raw():
    """fetch's 7 days: three cat-flash runs (fine, failed, killed for memory), a running gpu job, and the archiver
    running 40 times in the last hour with 2 failures."""
    v = lambda x: x / G                    # s() multiplies by G
    owner, start, end, failed, pods, peak, limit = [], [], [], [], [], [], []

    def run(job, cj, t0, took=None, bad=False, mem=None, lim=None, node="acer-laptop"):
        owner.append(s(v(1), job_name=job, owner_name=cj))
        start.append(s(v(t0), job_name=job))
        if took is not None:
            end.append(s(v(t0 + took), job_name=job))
        if bad:
            failed.append(s(v(1), job_name=job))
        pods.append(s(v(1), pod=job + "-abcde", created_by_name=job, node=node))
        if mem:
            peak.append(s(v(mem), pod=job + "-abcde"))
            limit.append(s(v(lim), pod=job + "-abcde"))

    run("cat-flash-1", "cat-flash", NOW - 50 * H, took=300, mem=500e6, lim=1e9)
    run("cat-flash-2", "cat-flash", NOW - 26 * H, took=200, bad=True)
    run("cat-flash-3", "cat-flash", NOW - 2 * H, took=280, mem=990e6, lim=1e9)
    run("gpu-1", "bg-taste-match-gpu", NOW - 3 * H)
    for i in range(40):
        run(f"email-remove-{i}", "email-remove", NOW - 1 * H + i * 60, took=50, bad=i in (3, 7))
    return {"owner": owner, "start": start, "end": end, "failed": failed,
            "active": [s(v(1), job_name="gpu-1")], "pod_job": pods, "mem_peak": peak, "mem_limit": limit,
            "oom": [s(v(1), namespace="apps", pod="cat-flash-3-abcde", container="flash")],
            "pc": [({"task": "\\job-scrapers upwork round"}, [(NOW - 30 * H, 1.0), (NOW - 1 * H, 0.0)])],
            "at": NOW}


def test_runs_and_days():
    d = jobruns.build(week_raw(), now=NOW)
    flash = d["runs"]["cat-flash"]
    assert [r["result"] for r in flash] == ["ok", "failed", "oom"]
    assert d["runs"]["bg-taste-match-gpu"][0]["result"] == "running"
    assert len(d["cells"]["cat-flash"]) == 7 and d["cells"]["cat-flash"][-1]["state"] == "oom"
    today = d["cells"]["email-remove"][-1]
    assert today["runs"] == 40 and today["failed"] == 2 and today["state"] == "some"       # amber, not red
    assert "40 runs (2 failed)" in today["tip"]
    assert d["pc_cells"]["job-scrapers upwork round"][-1]["state"] == "failed"
    assert [o["name"] for o in d["ooms"]] == ["cat-flash"]
    assert d["ooms"][0]["at"] == NOW - 2 * H + 280 and d["ooms"][0]["limit"] == 1e9     # no exact time: the run's end
    assert d["pc_cells"]["job-scrapers upwork round"][-1]["state"] == "failed"         # no "stopped" note here


def test_failures_say_if_normal():
    def runs(bad_before, n_before, bad_today, n_today):
        out = [{"start": NOW - 3 * 86400 + i, "result": "failed" if i < bad_before else "ok"} for i in range(n_before)]
        out += [{"start": NOW - 3600 + i, "result": "failed" if i < bad_today else "ok"} for i in range(n_today)]
        return out
    usual = jobruns.failures(runs(10, 500, 1, 100), NOW)
    assert usual["text"] == "11 of 600 runs failed this week (2%): normal for it (usually 2%)" and not usual["unusual"]
    worse = jobruns.failures(runs(10, 500, 30, 100), NOW)
    assert worse["unusual"] and "more than usual, 30 of 100 in the last 24 h (usually 2%)" in worse["text"]
    assert jobruns.failures(runs(0, 0, 0, 2), NOW)["text"] == "2 runs this week, none failed"


def test_paused_pc_task_has_no_colour():
    raw = week_raw()
    raw["pc"] = [({"task": "\\job-scrapers upwork round", "note": "stopped: Upwork asked for a login"},
                  [(NOW - 30 * H, 1.0), (NOW - 1 * H, 0.0)])]
    d = jobruns.build(raw, now=NOW)
    assert d["pc_cells"]["job-scrapers upwork round"][-1]["state"] == "paused"


def test_graphs_peaks_and_five_at_most():
    d = jobruns.build(week_raw(), now=NOW)
    g = jobruns.graphs(d, 24)
    archiver = next(x for x in g["took"] if x["name"] == "email-remove")
    assert len(archiver["points"]) <= 2                       # 40 runs in an hour: one or two hourly peaks
    flash = next(x for x in g["mem"] if x["name"] == "cat-flash")
    assert round(flash["points"][-1][1]) == 99
    many = {"runs": {f"job{i}": [{"start": NOW - H, "took": 60 * i, "result": "ok", "mem": None, "limit": None}]
                     for i in range(1, 9)}, "at": NOW}
    assert [x["name"] for x in jobruns.graphs(many, 24)["took"]] == ["job8", "job7", "job6", "job5", "job4"]


class Fixed:
    def __init__(self, data):
        self.data = data

    def get(self, *args):
        return Reading(True, self.data, "", time.time())


def test_jobs_page(monkeypatch):
    from starlette.testclient import TestClient
    from app import site_status
    from app.sources import prom
    monkeypatch.setattr(overview, "now", Fixed(overview.build(raw(), CATALOG, now=NOW)))
    monkeypatch.setattr(jobruns, "week", Fixed(jobruns.build(week_raw(), now=NOW)))
    monkeypatch.setattr(jobruns, "memory", Fixed({"cat-flash": [(NOW - 2 * H, 0.5e9), (NOW - 2 * H + 300, 0.99e9)]}))
    monkeypatch.setattr(prom, "alerts", Fixed([]))
    client = TestClient(site_status.app, root_path="/machines")
    for path in ("/jobs", "/jobs?range=7d", "/jobs?range=nonsense"):
        r = client.get(path)
        assert r.status_code == 200 and "Traceback" not in r.text, path
    text = client.get("/jobs").text
    for words in ("Every job", "Killed for using too much memory", "Each job, run by run", "Time per run",
                  "the red line is its limit", "runs failed this week", "into its run"):
        assert words in text, words
    assert 'id="job-cat-flash"' in text and 'viewBox="0 0 260 130"' in text
    assert text.count('class="day ') >= 7 * 5 and 'class="day some"' in text and 'class="day oom"' in text
    assert 'data-tip="' in text and "job-scrapers upwork round" in text
    assert 'svg class="chart"' in text
