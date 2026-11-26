"""Free Motion's numbers for Prometheus, read from its own files (fm-metrics.yaml mounts only these, read-only):

  data/posting-liveness.json        the live check's results       -> crashes of the check, results in 24 h
  data/freemotion-submissions.tsv   one row per application try    -> outcomes, 24 h and a 14-day baseline
  data/burn-settings.json, tmp/fm/burn/state.json   auto-apply on? which drivers may apply now
  tmp/fm/night/full-night.log       each round's pool summary      -> kept, live-checked, skipped, list size
  tmp/fm/scheduled/                 the daemon's daily logs        -> jobs that ended badly, Gmail OVERQUOTA,
                                                                      the 06:00 scan's pool summary, heap aborts
  tmp/fm/hub/agent.log              the hub agent                  -> its last report (it recycles every 6 h)

Made after the live check failed unseen for 14 days (2026-11-12..26: the image had no Chromium for Node's
Playwright, so every browser-checked posting was "uncertain" and skipped). Labels stay few: outcome, result, job,
source. No company, role or URL ever becomes a label. Run on the PC against a copy: FM_ROOT=<folder> python
fm-metrics.py --once
"""
import datetime as dt
import http.server
import json
import os
import re
import statistics
import sys
import time

ROOT = os.environ.get("FM_ROOT", "/fm-data")
PORT = int(os.environ.get("PORT", "9838"))
CACHE_S = 60
DAY = 86400

OUTCOMES = {"submitted", "errored", "validation-failed", "captcha", "already-applied", "blocked-waf", "in-progress",
            "rehearsal"}
FAILED = {"errored", "validation-failed", "captcha", "blocked-waf"}
FINISHED = FAILED | {"submitted", "already-applied"}
BROKEN_FROM, BROKEN_TO = "2026-11-12", "2026-11-26"   # the live check outage: kept out of the baselines


def path(rel):
    return os.path.join(ROOT, rel)


def paris_offset(t):
    """Europe/Paris offset in seconds at UTC time t (CET, CEST from the last Sunday of March 01:00 UTC to the last
    Sunday of October 01:00 UTC). The slim image may have no tz database."""
    y = time.gmtime(t).tm_year

    def last_sunday(month):
        d = dt.datetime(y, month + 1, 1, 1, tzinfo=dt.timezone.utc) - dt.timedelta(days=1)
        return (d - dt.timedelta(days=(d.weekday() + 1) % 7)).timestamp()
    return 7200 if last_sunday(3) <= t < last_sunday(10) else 3600


def paris_date(t):
    return time.strftime("%Y-%m-%d", time.gmtime(t + paris_offset(t)))


def from_paris(s):
    """'2026-11-26 15:35' (Paris) -> UTC epoch seconds."""
    naive = dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc).timestamp()
    return naive - paris_offset(naive - 3600)


def tail(rel, max_bytes):
    with open(path(rel), "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        return f.read().decode("utf-8", "replace").splitlines()


class Out:
    def __init__(self):
        self.lines, self.typed = [], set()

    def add(self, name, value, labels=None, kind="gauge", help_=""):
        if name not in self.typed:
            if help_:
                self.lines.append(f"# HELP {name} {help_}")
            self.lines.append(f"# TYPE {name} {kind}")
            self.typed.add(name)
        lab = ""
        if labels:
            lab = "{" + ",".join(f'{k}="{str(v)[:60]}"' for k, v in labels.items()) + "}"
        v = float(value)
        self.lines.append(f"{name}{lab} {int(v) if v.is_integer() else round(v, 6)}")


def source(out, name, rel, fn):
    """Run one reader; fm_source_ok says whether it worked, fm_source_mtime_seconds how fresh its file is."""
    try:
        out.add("fm_source_mtime_seconds", os.stat(path(rel)).st_mtime, {"source": name},
                help_="When the file this source reads last changed")
        fn(out)
        ok = 1
    except Exception as e:                      # a broken reader must not hide the others
        print(f"{name}: {type(e).__name__}: {e}", file=sys.stderr)
        ok = 0
    out.add("fm_source_ok", ok, {"source": name}, help_="1 if this source was read without error")


def liveness(out, now):
    cache = json.load(open(path("data/posting-liveness.json"), encoding="utf-8"))
    res, crashes = {"active": 0, "expired": 0, "uncertain": 0}, 0
    for v in cache.values():
        if not isinstance(v, dict) or now - (v.get("at", 0) / 1000) > DAY:
            continue
        r = v.get("result", "other")
        res[r if r in res else "other"] = res.get(r if r in res else "other", 0) + 1
        if str(v.get("reason", "")).startswith("check failed:"):
            crashes += 1
    for r, n in res.items():
        out.add("fm_live_checks_24h", n, {"result": r}, help_="Live checks of the last 24 h by result")
    out.add("fm_live_check_crashes_24h", crashes,
            help_="Live checks of the last 24 h that crashed before a page loaded (reason 'check failed:')")


def submissions(out, now):
    total, last24, days = {}, {}, {}
    today = paris_date(now)
    with open(path("data/freemotion-submissions.tsv"), encoding="utf-8", errors="replace") as f:
        next(f, None)
        for line in f:
            c = line.rstrip("\n").split("\t")
            if len(c) < 7:
                continue
            o = c[5] if c[5] in OUTCOMES else "other"
            try:
                t = dt.datetime.fromisoformat(c[6].replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            total[o] = total.get(o, 0) + 1
            if now - t <= DAY:
                last24[o] = last24.get(o, 0) + 1
            d = paris_date(t)
            days.setdefault(d, {})[o] = days.setdefault(d, {}).get(o, 0) + 1
    for o in sorted(OUTCOMES | {"other"}):
        out.add("fm_submissions_total", total.get(o, 0), {"outcome": o}, "counter",
                "Application tries ever recorded, by outcome (in-progress = a try started)")
        out.add("fm_submissions_24h", last24.get(o, 0), {"outcome": o}, help_="Application tries in the last 24 h")
    # Baseline: the 14 days before today with any try, the outage left out.
    base = [d for d in sorted(days) if d < today and not (BROKEN_FROM <= d <= BROKEN_TO)][-14:]
    sub = [days[d].get("submitted", 0) for d in base]
    fin = sum(days[d].get(o, 0) for d in base for o in FINISHED)
    bad = sum(days[d].get(o, 0) for d in base for o in FAILED)
    out.add("fm_baseline_days", len(base), help_="Days the baselines are made of")
    if sub:
        out.add("fm_baseline_submitted_daily", statistics.median(sub),
                help_="Median applications sent per day, the last 14 normal days")
    if fin:
        out.add("fm_baseline_failed_share", bad / fin,
                help_="Share of finished tries that failed (errored, validation, captcha, WAF), last 14 normal days")
    out.add("fm_submitted_today", days.get(today, {}).get("submitted", 0), help_="Applications sent today (Paris)")


def burn(out, now):
    s = json.load(open(path("data/burn-settings.json"), encoding="utf-8"))
    st = json.load(open(path("tmp/fm/burn/state.json"), encoding="utf-8"))
    drivers, off, paused = s.get("drivers", []), set(s.get("off", [])), st.get("paused", {}) or {}

    def held(d):
        u = paused.get(d) or 0
        return (u / 1000 if u > 1e12 else u) > now
    allowed = [d for d in drivers if d not in off and not held(d)]
    out.add("fm_auto_apply_enabled", 1 if s.get("enabled") else 0, help_="1 while auto-apply (burn.mjs) is on")
    out.add("fm_drivers", len(drivers), help_="Drivers in the order")
    out.add("fm_drivers_allowed", len(allowed), help_="Drivers that may apply now (not off, not paused by a rule)")


POOL = {"kept": re.compile(r"^\s*kept: (\d+) after"),
        "live_checked": re.compile(r"^\s*live check: (\d+) checked"),
        "live_skipped": re.compile(r"^\s*live check: \d+ checked.*?, (\d+) skipped"),
        "unscored": re.compile(r"^\s*no fit score yet: (\d+)"),
        "list": re.compile(r"^\s*tonight's list: (\d+) jobs")}
RUN = re.compile(r"^##### full night run, \w+ (\d{4}-\d\d-\d\d) (\d\d:\d\d)")
ROUND = re.compile(r"^===== round \d+, \w+ (\d\d:\d\d)")
SCANHEAD = re.compile(r"^=== (\d{4}-\d\d-\d\d \d\d:\d\d) make-pool:")


def last_pool(lines, head):
    """The last complete pool summary in `lines` (ending in "tonight's list") and when it was built."""
    end = next((i for i in range(len(lines) - 1, -1, -1) if POOL["list"].match(lines[i])), None)
    if end is None:
        return None
    vals = {}
    i = end
    while i >= 0 and len(vals) < len(POOL):
        for k, rx in POOL.items():
            m = rx.match(lines[i])
            if m and k not in vals:
                vals[k] = int(m.group(1))
        if i < end and POOL["list"].match(lines[i]):
            break                                   # the summary before: stop
        i -= 1
    when = head(lines, end)
    return (when, vals) if when else None


def round_time(lines, end):
    hm, day = None, None
    for i in range(end, -1, -1):
        if hm is None:
            m = ROUND.match(lines[i])
            if m:
                hm = m.group(1)
        m = RUN.match(lines[i])
        if m:
            day = m.group(1)
            t = from_paris(f"{day} {hm or m.group(2)}")
            if hm and hm < m.group(2):
                t += DAY                             # the round started after midnight
            return t
    return None


def scan_time(lines, end):
    for i in range(end, -1, -1):
        m = SCANHEAD.match(lines[i])
        if m:
            return from_paris(m.group(1))
    return None


def pool(out, now):
    found = []
    for size in (4 << 20, 32 << 20):              # a long round can push its run header out of the first window
        r = last_pool(tail("tmp/fm/night/full-night.log", size), round_time)
        if r:
            found.append(r)
            break
    for d in (paris_date(now), paris_date(now - DAY)):
        f = f"tmp/fm/scheduled/scan-{d}.log"
        if os.path.exists(path(f)):
            r = last_pool(tail(f, 8 << 20), scan_time)
            if r:
                found.append(r)
            break
    if not found:
        return
    when, vals = max(found, key=lambda x: x[0])
    out.add("fm_pool_build_timestamp_seconds", when, help_="When the last pool (a round's list of jobs) was built")
    for k, v in vals.items():
        out.add(f"fm_pool_{k}", v, help_=f"The last pool build: {k.replace('_', ' ')}")


ENDED = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d) ([\w.-]+): ended \(exit (-?\d+)\)")
NIGHT_END = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d) full-night\.sh ended \(exit (-?\d+)\)")
DONE = re.compile(r"FM: DONE · (\d+) sent of (\d+) jobs")


def daemon(out, now):
    fails, last = {}, {}
    rnd = None
    for d in (paris_date(now - DAY), paris_date(now)):
        for name, rx in ((f"tmp/fm/scheduled/daemon-{d}.log", ENDED), (f"tmp/fm/scheduled/night-{d}.log", NIGHT_END)):
            if not os.path.exists(path(name)):
                continue
            pending = None
            for line in open(path(name), encoding="utf-8", errors="replace"):
                m = rx.match(line)
                if m:
                    t = from_paris(m.group(1))
                    job, code = (m.group(2), m.group(3)) if rx is ENDED else ("night", m.group(2))
                    last[job] = max(last.get(job, 0), t)
                    if code != "0" and now - t <= DAY:
                        fails[job] = fails.get(job, 0) + 1
                    pending = t if rx is NIGHT_END else pending
                    continue
                m = DONE.search(line)
                if m and pending:
                    rnd = (pending, int(m.group(1)), int(m.group(2)))
    for job in sorted(set(last) | set(fails)):
        out.add("fm_daemon_job_failures_24h", fails.get(job, 0), {"job": job},
                help_="Runs of each daemon job that ended with a non-zero exit in the last 24 h")
        out.add("fm_daemon_job_last_end_timestamp_seconds", last[job], {"job": job},
                help_="When each daemon job last ended")
    if rnd:
        out.add("fm_round_last_end_timestamp_seconds", rnd[0], help_="When the last round ended")
        out.add("fm_round_last_sent", rnd[1], help_="Applications the last round sent")
        out.add("fm_round_last_jobs", rnd[2], help_="Jobs the last round had")
    over = 0
    today = paris_date(now)
    for f in os.listdir(path("tmp/fm/scheduled")):
        if f.endswith(f"-{today}.log"):
            over += sum(l.count("OVERQUOTA") for l in open(path(f"tmp/fm/scheduled/{f}"), encoding="utf-8", errors="replace"))
    out.add("fm_gmail_overquota_today", over, help_="Gmail [OVERQUOTA] answers in today's job logs (IMAP limit)")
    heap = 0
    for f in os.listdir(path("tmp/fm/scheduled")):
        if f == "loops.err" or (f.startswith("loop-") and f.endswith(".err")):
            heap += sum(l.count("heap limit") for l in open(path(f"tmp/fm/scheduled/{f}"), encoding="utf-8", errors="replace"))
    out.add("fm_score_loop_heap_aborts", heap, kind="counter",
            help_="Score loop heap-limit aborts recorded in the keeper's error files")


AGENT = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z) sent \d+ records")


def hub(out, now):
    t = None
    for line in tail("tmp/fm/hub/agent.log", 1 << 20):
        m = AGENT.match(line)
        if m:
            t = dt.datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp()
    if t:
        out.add("fm_hub_agent_last_report_timestamp_seconds", t,
                help_="When the hub agent last sent its records to the dashboard (it recycles every 6 h)")


SINGLE_FILES = ["data/posting-liveness.json", "data/freemotion-submissions.tsv", "data/burn-settings.json",
                "tmp/fm/night/full-night.log"]   # mounted one by one (fm-metrics.yaml): each mount follows an inode


def replaced():
    """A file replaced on the host by a rename (git merge of an fm-data snapshot: three of these are tracked) leaves
    this mount on the old, unlinked inode, whose link count is 0. Restarting the container binds the current file."""
    return [f for f in SINGLE_FILES if os.path.exists(path(f)) and os.stat(path(f)).st_nlink == 0]


def metrics():
    gone = replaced()
    if gone:
        print(f"replaced on the host, restarting to read the new copies: {', '.join(gone)}", file=sys.stderr, flush=True)
        os._exit(3)
    now = time.time()
    out = Out()
    source(out, "liveness", "data/posting-liveness.json", lambda o: liveness(o, now))
    source(out, "submissions", "data/freemotion-submissions.tsv", lambda o: submissions(o, now))
    source(out, "burn", "tmp/fm/burn/state.json", lambda o: burn(o, now))
    source(out, "pool", "tmp/fm/night/full-night.log", lambda o: pool(o, now))
    source(out, "daemon", "tmp/fm/scheduled", lambda o: daemon(o, now))
    source(out, "hub", "tmp/fm/hub/agent.log", lambda o: hub(o, now))
    out.add("fm_metrics_scrape_seconds", time.time() - now, help_="How long reading the files took")
    return "\n".join(out.lines) + "\n"


_cache = (0.0, "")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global _cache
        if self.path != "/metrics":
            self.send_response(404); self.end_headers(); return
        if time.time() - _cache[0] > CACHE_S:
            _cache = (time.time(), metrics())
        body = _cache[1].encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if "--once" in sys.argv:
        sys.stdout.write(metrics())
    else:
        http.server.ThreadingHTTPServer(("", PORT), Handler).serve_forever()
