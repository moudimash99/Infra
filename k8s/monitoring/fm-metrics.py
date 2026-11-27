"""Free Motion's numbers for Prometheus, read from its own files (fm-metrics.yaml mounts only these, read-only):

  data/posting-liveness.json        the live check's results       -> crashes of the check, results in 24 h
  data/freemotion-submissions.tsv   one row per application try    -> outcomes, 24 h and a 14-day baseline
  data/burn-settings.json, tmp/fm/burn/state.json   auto-apply on? which drivers may apply now
  tmp/fm/night/full-night.log       each round's pool summary      -> kept, live-checked, skipped, list size;
                                                                      applying paused for want of a writer;
                                                                      "note: agy is signed out"
  tmp/fm/night/out/                 run.sh's out marks (one epoch  -> writers that can write now, drivers that
                                    number per driver or writer)      can apply now
  tmp/fm/scheduled/                 the daemon's daily logs        -> jobs that ended badly, Gmail OVERQUOTA,
                                                                      the 06:00 scan's pool summary, heap aborts;
                                                                      the round progress emails lost (night-*.log)
  tmp/fm/hub/agent.log              the hub agent                  -> its last report (it recycles every 6 h)
  data/agent-inbox.md               the agent inbox                -> open items by kind and the oldest's age;
                                                                      open "web-llm: <site> is signed out" items
  tmp/fm/night/closing/             the rounds' end steps          -> steps that failed in a row (OFF: not written
                                    (not there yet, not mounted)      there or mounted yet, see CLOSING_DIR)

The writer and driver checks were added 2026-11-27, when applying started to pause on a job while no CV or letter
writer can write (fm-agent run.sh, the user's rule), so a writer outage stops applying instead of sending generic CVs.
The progress emails, the inbox and the end steps were added the same day (architecture review 2026-11-27, finding 11,
recommendation 4): each failed for days and nobody saw it (emails 11-13..20, inbox unread since 11-01, lessons review
failing in most rounds since 11-10).

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


def secs(u):
    u = float(u or 0)
    return u / 1000 if u > 1e12 else u


def out_marks():
    """run.sh's and fm-llm's out marks: tmp/fm/night/out/<name> holds the epoch second it comes back."""
    marks = {}
    d = path("tmp/fm/night/out")
    for name in os.listdir(d):
        try:
            marks[name] = secs(open(os.path.join(d, name), encoding="utf-8").read().strip())
        except (OSError, ValueError):
            continue
    return marks


def burn(out, now):
    s = json.load(open(path("data/burn-settings.json"), encoding="utf-8"))
    st = json.load(open(path("tmp/fm/burn/state.json"), encoding="utf-8"))
    drivers, off, paused = s.get("drivers", []), set(s.get("off", [])), st.get("paused", {}) or {}

    def held(d):
        return secs(paused.get(d)) > now
    allowed = [d for d in drivers if d not in off and not held(d)]
    out.add("fm_auto_apply_enabled", 1 if s.get("enabled") else 0, help_="1 while auto-apply (burn.mjs) is on")
    out.add("fm_drivers", len(drivers), help_="Drivers in the order")
    out.add("fm_drivers_allowed", len(allowed), help_="Drivers that may apply now (not off, not paused by a rule)")


# The CV and letter writers, in fm-llm/lib/doc-writers.mjs WRITER_ORDER. WEB_LLM_OFF is not set on the controller
# since 2026-11-27 (both websites on); if it comes back, prepare-docs also leaves those websites out.
WRITER_ORDER = ["chatgpt-web", "gemini-web", "codex", "agy", "sonnet1", "copilot"]


def writers(out, now):
    """Who can work now, from the out marks: writers as prepare-docs counts them (writersFree), drivers as run.sh
    does (an out mark is a limit or a sign-out) plus auto-apply's own off list and pauses."""
    marks = out_marks()
    try:
        st = json.load(open(path("tmp/fm/burn/state.json"), encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    s = json.load(open(path("data/burn-settings.json"), encoding="utf-8"))
    paused, why = st.get("paused", {}) or {}, st.get("why", {}) or {}
    for name in sorted(marks):
        out.add("fm_out_until_timestamp_seconds", marks[name], {"name": name},
                help_="When a driver or writer marked out (limit, sign-out, errors) is back")

    def writer_out(n):
        # doc-writers.mjs isOut: auto-apply's hold "for CVs and letters" shares the file but only stops applying.
        u = marks.get(n, 0)
        if u <= now:
            return False
        return not (secs(paused.get(n)) == u and "for CVs and letters" in str(why.get(n, "")))
    free = [n for n in WRITER_ORDER if not writer_out(n)]
    out.add("fm_writers", len(WRITER_ORDER), help_="CV and letter writers in the chain")
    out.add("fm_writers_free", len(free), help_="CV and letter writers not marked out now")
    usable = [d for d in s.get("drivers", []) if d not in set(s.get("off", []))
              and marks.get(d, 0) <= now and secs(paused.get(d)) <= now]
    out.add("fm_drivers_usable", len(usable),
            help_="Drivers that can apply now: not off, not paused by auto-apply, not out at a limit or signed out")


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


PAUSE = re.compile(r"^(note: no CV or letter writer available at job \d+; applying paused"
                   r"|docs for job \d+: waiting, no CV or letter writer)")
# Anything that shows a lane moved on: documents made, a job's outcome line, the agent's steps ("05:16:51  Read") and
# narration (indented » → ✗), a stop, a new round or run. A paused lane prints only its pause lines, again at each
# try (1 to 60 minutes apart). With several lanes, one lane still working counts as not paused: all lanes share the
# same writers, so a real outage pauses them all within minutes.
PROGRESS = re.compile(r"^(job \d+\b|docs for job \d+: (?!waiting)|\d\d:\d\d:\d\d  |\s+[»→✗] "
                      r"|note: stop file found|===== round|##### full night run)")
SIGNED_OUT = re.compile(r"^note: ([\w-]+) is signed out")
PAUSE_FRESH_S = 75 * 60      # the longest wait between two tries is 60 min (prepare-docs writersWait) + 30 s


def night(out, now):
    """From the rounds' log: is applying paused for want of a writer, and when was a driver last found signed out."""
    lines = tail("tmp/fm/night/full-night.log", 4 << 20)   # about a day of rounds (2.8 MB from 02:25 to 19:00 on 11-27)
    last_pause = last_progress = -1
    signed = {}
    for i, line in enumerate(lines):
        if PAUSE.match(line):
            last_pause = i
        elif PROGRESS.match(line):
            last_progress = i
        m = SIGNED_OUT.match(line)
        if m:
            signed[m.group(1)] = i
    fresh = now - os.stat(path("tmp/fm/night/full-night.log")).st_mtime < PAUSE_FRESH_S
    out.add("fm_applying_paused_for_writer", 1 if last_pause > last_progress and fresh else 0,
            help_="1 while the rounds wait on a job because no CV or letter writer can write (the log's newest step)")
    for who, i in sorted(signed.items()):
        t = round_time(lines, i)
        if t:
            out.add("fm_signed_out_note_timestamp_seconds", t, {"who": who},
                    help_="Start of the last round in which run.sh found this driver signed out")


INBOX_SIGNED_OUT = re.compile(r"^- \[ \] (\d{4}-\d\d-\d\d \d\d:\d\d) .*?web-llm: ([\w-]+) is signed out")
# An item as the dashboard reads it (fm-agent hub-records.mjs alerts()): "[ ]" and "[!]" are open, the date is optional.
INBOX_ITEM = re.compile(r"^- \[([ xX!])\] (?:(\d{4}-\d\d-\d\d)(?: (\d\d:\d\d))?\s*[—-]+\s*)?(.*)$")
# A fixed list of kinds, never the item's text (it names companies). The first that matches wins; the words are the
# dashboard's (hub-records.mjs KINDS): its "info" lines are the notices, its warnings and sign-outs the failures.
INBOX_KINDS = (
    ("recruiter", re.compile(r"Recruiter wrote|wants to talk", re.I)),          # scheduled.mjs, from the reply check
    ("notice", re.compile(r"tracker change\(s\) (?:applied|made) from employers' replies|Weekly lessons report ready"
                          r"|When no round runs: ", re.I)),
    ("failure", re.compile(r"signed out|sign(?:ed)? in again|failed|not sent|could not|refused|broke|error|CAPTCHA"
                           r"|no fit score|^posting text", re.I)),
)


def inbox(out, now):
    """data/agent-inbox.md: open items by kind and when the oldest of each was added; and the open items
    "web-llm: <site> is signed out" (fm-core lib/llm-hooks.mjs adds one per site). Nothing closes an item but a person
    or a session ticking it, so the count only grows while nobody reads the file (101 open on 2026-11-27, none ticked
    since 11-01)."""
    opened, count, oldest = {}, {k: 0 for k, _ in INBOX_KINDS + (("other", None),)}, {}
    for line in open(path("data/agent-inbox.md"), encoding="utf-8", errors="replace"):
        m = INBOX_SIGNED_OUT.match(line)
        if m:
            opened[m.group(2)] = max(opened.get(m.group(2), 0), from_paris(m.group(1)))
        m = INBOX_ITEM.match(line.rstrip("\r\n"))
        if not m or m.group(1) not in " !":
            continue
        t = from_paris(f"{m.group(2)} {m.group(3) or '00:00'}") if m.group(2) else None
        if t is not None and t > now:
            continue                                # added after `now` (a backtest)
        kind = next((k for k, rx in INBOX_KINDS if rx.search(m.group(4))), "other")
        count[kind] += 1
        if t is not None:
            oldest[kind] = min(oldest.get(kind, t), t)
    for site, t in sorted(opened.items()):
        out.add("fm_inbox_signed_out_opened_timestamp_seconds", t, {"who": site},
                help_="When the open agent-inbox item 'web-llm: <site> is signed out' was added")
    for kind, n in count.items():
        out.add("fm_inbox_open_items", n, {"kind": kind},
                help_="Open agent-inbox items by kind (recruiter, failure, notice, other)")
    for kind, t in sorted(oldest.items()):
        out.add("fm_inbox_oldest_open_timestamp_seconds", t, {"kind": kind},
                help_="When the oldest open agent-inbox item of this kind was added")


# The steps after a round (fm-orchestrator closing.sh, started by run.sh through closing.mjs, detached): each runs
# "|| true", so a failure leaves only its own lines in closing-<run>-<HHMMSS>.log (CLOSING_DIR). A step is judged by
# the line it prints when it worked, never by error wording: a changed sentence then reads as a failure, not as quiet.
END_STEPS = {
    "sent-check": re.compile(r"^!! = our record disagrees with the inbox: \d+"),     # check-sent.py's last line
    "replies": re.compile(r"^Nothing to apply: "),       # inbox-replies.py --apply all, run only if the check exited 0
    "follow-ups": re.compile(r"( -> \d+ picked$|^\d+ follow-up draft\(s\) in Gmail's Drafts folder)"),  # followups.mjs
    "lessons": re.compile(r"^lessons: run \S+(: \d+ failed job| has no failures)"),  # lessons.mjs review
}
MAIL_STEPS = {"sent-check", "replies", "follow-ups"}   # a closing whose log shows OVERQUOTA: FreeMotionGmailOverQuota's
FOLLOWUPS_RAN = re.compile(r"^(\d+ follow-up\(s\) sent since the last check|followups: )")   # absent: NO_FOLLOWUPS=1
CLOSING_LOG = re.compile(r"^closing-.*?(\d{4}-\d\d-\d\d).*\.log$")
# OFF until this folder exists and is mounted. The logs are written to tmp/fm/night itself today, beside the agents'
# job files (40,000 entries, transcripts that name freemotion-credentials), which fm-metrics must not mount. It turns
# on by itself once closing.mjs writes them here (fm-orchestrator, one line) and fm-metrics.yaml mounts the folder.
CLOSING_DIR = "tmp/fm/night/closing"
CLOSING_QUIET_S = 3 * 3600   # a closing took 66 min (median) to 107 min (most), 11-01..27: silent 3 h is not running
CLOSING_DAYS = 8


def judge_closing(file):
    """{step: "ok" | "failed" | None (not judged)} for one closing log, and whether it reached "CLOSED"."""
    ok, closed, quota, followups = set(), False, False, False
    for line in open(file, encoding="utf-8", errors="replace"):
        line = line.rstrip("\r\n")
        closed = closed or line.startswith("CLOSED ")
        quota = quota or "OVERQUOTA" in line
        followups = followups or bool(FOLLOWUPS_RAN.match(line))
        for step, rx in END_STEPS.items():
            if step not in ok and rx.search(line):
                ok.add(step)
    res = {}
    for step in END_STEPS:
        if step in ok:
            res[step] = "ok"
        elif (quota and step in MAIL_STEPS) or (step == "follow-ups" and closed and not followups):
            res[step] = None
        else:
            res[step] = "failed"                    # also every step a killed or hung closing never got to
    return res, closed


def end_steps(out, now):
    """How many of the last closings in a row each step failed in, newest first, and when it last worked. A closing is
    judged once it printed "CLOSED" or has been silent CLOSING_QUIET_S; closings overlap (rounds 40 min apart, a
    closing about an hour), so they are taken in the order they ended (file mtime). Names carry the run's date (a
    round after midnight keeps the day before), so only the last CLOSING_DAYS by name are looked at."""
    since = paris_date(now - CLOSING_DAYS * DAY)
    logs = []
    with os.scandir(path(CLOSING_DIR)) as it:      # 40,000 entries on 2026-11-27: only names are read
        for e in it:
            m = CLOSING_LOG.match(e.name)
            if m and m.group(1) >= since:
                t = e.stat().st_mtime
                if t <= now:
                    logs.append((t, e.name))
    streak, last_ok, settled, last = {s: 0 for s in END_STEPS}, {}, set(), None
    for t, name in sorted(logs, reverse=True):
        res, closed = judge_closing(path(f"{CLOSING_DIR}/{name}"))
        if not closed and now - t < CLOSING_QUIET_S:
            continue                                # still running
        last = last or t
        for step, r in res.items():
            if step in settled or r is None:
                continue
            if r == "ok":
                last_ok[step] = t
                settled.add(step)
            else:
                streak[step] += 1
        if len(settled) == len(END_STEPS):
            break
    if last:
        out.add("fm_end_steps_last_closing_timestamp_seconds", last,
                help_="When the last judged closing (a round's end steps) ended")
    for step in END_STEPS:
        out.add("fm_end_step_failed_in_a_row", streak[step], {"step": step},
                help_="Closings in a row, newest first, in which this end-of-round step failed or never ran")
        if step in last_ok:
            out.add("fm_end_step_last_ok_timestamp_seconds", last_ok[step], {"step": step},
                    help_="When this end-of-round step last worked (the closing's end)")


# The progress emails (fm-orchestrator night-mail.py beside each round; its output goes to the night task's log):
# send() tries 3 times, a minute apart, then gives up without a word, so a lost email is "attempt 3" with no "sent:".
MAIL_SENT = re.compile(r"^sent: ")
MAIL_LOST = re.compile(r"^not sent \(.*\); attempt 3$")
NIGHT_STAMP = re.compile(r"^(?:##### )?(\d{4}-\d\d-\d\d \d\d:\d\d) ")
MAIL_DAYS = 7


def progress_mail(out, now):
    """The round progress emails of the last MAIL_DAYS: lost in a row (newest first), the last sent, the last try.
    Their lines carry no time: each takes the time of the stamped line before it."""
    events = []
    for back in range(MAIL_DAYS, -1, -1):
        d = paris_date(now - back * DAY)
        f = path(f"tmp/fm/scheduled/night-{d}.log")
        if not os.path.exists(f):
            continue
        t = from_paris(f"{d} 00:00")
        for line in open(f, encoding="utf-8", errors="replace"):
            line = line.rstrip("\r\n")
            m = NIGHT_STAMP.match(line)
            if m:
                t = from_paris(m.group(1))
            elif t <= now and MAIL_SENT.match(line):
                events.append((t, True))
            elif t <= now and MAIL_LOST.match(line):
                events.append((t, False))
    lost = 0
    for t, sent in reversed(events):
        if sent:
            out.add("fm_progress_email_last_sent_timestamp_seconds", t,
                    help_="When the last round progress email went out (to the minute)")
            break
        lost += 1
    out.add("fm_progress_emails_lost_in_a_row", lost,
            help_="Round progress emails given up on after 3 tries, in a row, newest first")
    if events:
        out.add("fm_progress_email_last_try_timestamp_seconds", events[-1][0],
                help_="When a round progress email was last sent or given up on")


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
                "tmp/fm/night/full-night.log", "data/agent-inbox.md"]   # mounted one by one (fm-metrics.yaml): each follows an inode


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
    source(out, "writers", "tmp/fm/night/out", lambda o: writers(o, now))
    source(out, "pool", "tmp/fm/night/full-night.log", lambda o: pool(o, now))
    source(out, "night", "tmp/fm/night/full-night.log", lambda o: night(o, now))
    source(out, "inbox", "data/agent-inbox.md", lambda o: inbox(o, now))
    source(out, "daemon", "tmp/fm/scheduled", lambda o: daemon(o, now))
    if os.path.isdir(path(CLOSING_DIR)):           # not mounted yet: no numbers, and no "broken" either
        source(out, "end-steps", CLOSING_DIR, lambda o: end_steps(o, now))
    source(out, "progress-mail", "tmp/fm/scheduled", lambda o: progress_mail(o, now))
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
