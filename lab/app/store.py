"""The lab's own memory (lab.db): job runs (Kubernetes keeps only the last few), a daily snapshot of
bg-taste-match's stage totals, the PC's reports, and day-start values of counters."""
import datetime as dt
import json
import os
import sqlite3
import threading
import time

from .config import LAB_DB, TZ

_lock = threading.Lock()
SCHEMA = """
CREATE TABLE IF NOT EXISTS job_runs (uid TEXT PRIMARY KEY, name TEXT, cronjob TEXT, state TEXT, start REAL, end REAL,
                                     reason TEXT);
CREATE INDEX IF NOT EXISTS job_runs_cron ON job_runs (cronjob, start);
CREATE TABLE IF NOT EXISTS bgt_daily (day TEXT PRIMARY KEY, totals TEXT, at REAL);
CREATE TABLE IF NOT EXISTS reports (source TEXT, received REAL, body TEXT);
CREATE INDEX IF NOT EXISTS reports_source ON reports (source, received);
CREATE TABLE IF NOT EXISTS counters (name TEXT, day TEXT, start TEXT, PRIMARY KEY (name, day));
CREATE TABLE IF NOT EXISTS job_logs (uid TEXT PRIMARY KEY, name TEXT, cronjob TEXT, summary TEXT, tail TEXT, at REAL);
"""


def save_log(job, summary, tail):
    with _lock, conn() as c:
        c.execute("INSERT OR REPLACE INTO job_logs (uid, name, cronjob, summary, tail, at) VALUES (?,?,?,?,?,?)",
                  (job["uid"], job["name"], job["cronjob"], json.dumps(summary), tail, time.time()))
        c.execute("DELETE FROM job_logs WHERE at < ?", (time.time() - 60 * 86400,))


def logged_uids():
    with conn() as c:
        return {r[0] for r in c.execute("SELECT uid FROM job_logs")}


def job_logs(prefix, days=14):
    """Saved summaries of finished runs whose CronJob or name starts with prefix, newest first, with run times."""
    since = time.time() - days * 86400
    with conn() as c:
        rows = c.execute("""SELECT l.uid, l.name, l.cronjob, l.summary, l.tail, r.state, r.start, r.end
                            FROM job_logs l LEFT JOIN job_runs r USING (uid)
                            WHERE (l.cronjob LIKE ? OR l.name LIKE ?) AND COALESCE(r.start, l.at) >= ?
                            ORDER BY COALESCE(r.start, l.at) DESC""", (prefix + "%", prefix + "%", since)).fetchall()
    return [{**dict(r), "summary": json.loads(r["summary"] or "{}")} for r in rows]


def conn():
    os.makedirs(os.path.dirname(LAB_DB) or ".", exist_ok=True)
    c = sqlite3.connect(LAB_DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, conn() as c:
        c.executescript(SCHEMA)


def today():
    return dt.datetime.now(TZ).date().isoformat()


def record_jobs(jobs):
    """Upsert every Job seen; a run that finished keeps its outcome after Kubernetes deletes it."""
    with _lock, conn() as c:
        for j in jobs:
            c.execute("""INSERT INTO job_runs (uid, name, cronjob, state, start, end, reason) VALUES (?,?,?,?,?,?,?)
                         ON CONFLICT(uid) DO UPDATE SET state=excluded.state, end=excluded.end, reason=excluded.reason""",
                      (j["uid"], j["name"], j["cronjob"], j["state"], j["start"], j["end"] or j.get("failed_at"),
                       j["reason"]))
        # Runs we saw running that vanished without finishing (deleted by hand) stay "running" forever: close them.
        live = {j["uid"] for j in jobs}
        for row in c.execute("SELECT uid FROM job_runs WHERE state IN ('running', 'pending')").fetchall():
            if row["uid"] not in live:
                c.execute("UPDATE job_runs SET state = 'stopped' WHERE uid = ?", (row["uid"],))


def job_history(cronjob=None, days=14, prefix=None):
    since = time.time() - days * 86400
    with conn() as c:
        if prefix:
            rows = c.execute("SELECT * FROM job_runs WHERE (cronjob LIKE ? OR name LIKE ?) AND start >= ? "
                             "ORDER BY start DESC", (prefix + "%", prefix + "%", since))
        else:
            rows = c.execute("SELECT * FROM job_runs WHERE cronjob = ? AND start >= ? ORDER BY start DESC",
                             (cronjob, since))
        return [dict(r) for r in rows]


def record_bgt(totals):
    with _lock, conn() as c:
        c.execute("INSERT OR REPLACE INTO bgt_daily (day, totals, at) VALUES (?, ?, ?)",
                  (today(), json.dumps(totals), time.time()))


def bgt_history(days=60):
    with conn() as c:
        rows = c.execute("SELECT day, totals, at FROM bgt_daily ORDER BY day DESC LIMIT ?", (days,)).fetchall()
    return [{"day": r["day"], "at": r["at"], **json.loads(r["totals"])} for r in reversed(rows)]


def add_report(source, body):
    with _lock, conn() as c:
        c.execute("INSERT INTO reports (source, received, body) VALUES (?, ?, ?)", (source, time.time(), json.dumps(body)))
        c.execute("DELETE FROM reports WHERE received < ?", (time.time() - 7 * 86400,))


def latest_report(source):
    with conn() as c:
        r = c.execute("SELECT received, body FROM reports WHERE source = ? ORDER BY received DESC LIMIT 1",
                      (source,)).fetchone()
    return {"received": r["received"], "body": json.loads(r["body"])} if r else None


def counter_today(name, totals):
    """Today's increase of monotonic counters (they reset when their pod restarts: then today restarts too)."""
    day = today()
    with _lock, conn() as c:
        r = c.execute("SELECT start FROM counters WHERE name = ? AND day = ?", (name, day)).fetchone()
        start = json.loads(r["start"]) if r else None
        if start is None or any(totals.get(k, 0) < v for k, v in start.items()):
            start = dict(totals)
            c.execute("INSERT OR REPLACE INTO counters (name, day, start) VALUES (?, ?, ?)", (name, day, json.dumps(start)))
        c.execute("DELETE FROM counters WHERE day < ?", ((dt.date.fromisoformat(day) - dt.timedelta(days=3)).isoformat(),))
    return {k: v - start.get(k, 0) for k, v in totals.items()}
