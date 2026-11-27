"""The GPU work queue (moudimash99/Infra#14): a list of work items in one SQLite file, each taken with a lease.

An item is one piece of work that can be done on its own ("transcribe video X"), named by (kind, key). A worker
claims the next waiting item and gets a lease: a random token and a time limit. While it works it renews the lease
(heartbeat); it ends with done, fail or release, each checked against its lease token, so a worker whose lease ran
out can never finish or fail an item someone else holds now.

  waiting --claim--> leased --done--> done
                       |----fail----> waiting again, or failed after MAX_ATTEMPTS claims
                       |--release---> waiting (the worker was told to stop: a pause, a game; not counted)
                       '--lease runs out (the PC went to sleep, the worker died)--> waiting, or failed as for fail

A claim counts as an attempt, so an item that kills its worker every time (out of memory, a bad file) ends in
failed after MAX_ATTEMPTS and waits for a person, as bg-taste-match's captions/<id>.tries does on the Acer.
Standard library only, so the same file runs in a small pod and in tests.
"""
import json
import secrets
import sqlite3
import time

MAX_ATTEMPTS = 3
LEASE_SECONDS = 15 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id        INTEGER PRIMARY KEY,
    kind      TEXT NOT NULL,
    key       TEXT NOT NULL,
    payload   TEXT NOT NULL DEFAULT '{}',
    state     TEXT NOT NULL DEFAULT 'waiting',   -- waiting | leased | done | failed
    attempts  INTEGER NOT NULL DEFAULT 0,
    lease     TEXT,
    worker    TEXT,
    lease_until REAL,
    added     REAL NOT NULL,
    started   REAL,
    finished  REAL,
    error     TEXT,
    UNIQUE (kind, key)
);
CREATE INDEX IF NOT EXISTS items_state ON items (state, kind, id);
"""


class LeaseLost(Exception):
    """The lease token is not the item's current lease (it ran out and went back, or the item is finished)."""


class Queue:
    def __init__(self, path, max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS, clock=time.time):
        self.path, self.max_attempts, self.lease_seconds, self.now = path, max_attempts, lease_seconds, clock
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def _tx(self):
        """One write at a time (BEGIN IMMEDIATE), so two workers can't claim the same item."""
        return _Tx(self.db)

    # ---- the jobs that make the work ---------------------------------------------------------------------------

    def add(self, kind, key, payload=None):
        """Adds an item; True when it is new. An item already known (in any state) is left alone, so a job can add
        the whole list every run."""
        cur = self.db.execute("INSERT OR IGNORE INTO items (kind, key, payload, added) VALUES (?, ?, ?, ?)",
                              (kind, key, json.dumps(payload or {}), self.now()))
        return cur.rowcount == 1

    def retry(self, kind, key):
        """Puts a failed item back in the queue with its attempts cleared (a person's decision)."""
        cur = self.db.execute("UPDATE items SET state='waiting', attempts=0, error=NULL WHERE kind=? AND key=? "
                              "AND state='failed'", (kind, key))
        return cur.rowcount == 1

    # ---- workers -----------------------------------------------------------------------------------------------

    def expire(self):
        """Items whose lease ran out go back to waiting (or to failed after max_attempts claims)."""
        now = self.now()
        with self._tx():
            self.db.execute("UPDATE items SET state='failed', lease=NULL, worker=NULL, lease_until=NULL, finished=?, "
                            "error='lease ran out ' || attempts || ' times' "
                            "WHERE state='leased' AND lease_until < ? AND attempts >= ?",
                            (now, now, self.max_attempts))
            self.db.execute("UPDATE items SET state='waiting', lease=NULL, worker=NULL, lease_until=NULL, "
                            "error='lease ran out' WHERE state='leased' AND lease_until < ?", (now,))

    def claim(self, worker, kinds=None, newest_first=False):
        """The next waiting item, leased to this worker: a dict with id, kind, key, payload, lease, lease_until; None
        when nothing waits. newest_first takes from the end of the list (a helper working from the other end than
        the main worker, so they rarely meet on one item)."""
        self.expire()
        now = self.now()
        where, args = "state='waiting'", []
        if kinds:
            where += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        with self._tx():
            row = self.db.execute(f"SELECT * FROM items WHERE {where} ORDER BY id {'DESC' if newest_first else 'ASC'} "
                                  "LIMIT 1", args).fetchone()
            if row is None:
                return None
            lease = secrets.token_urlsafe(16)
            until = now + self.lease_seconds
            self.db.execute("UPDATE items SET state='leased', lease=?, worker=?, lease_until=?, started=?, "
                            "attempts=attempts+1 WHERE id=?", (lease, worker, until, now, row["id"]))
        return {"id": row["id"], "kind": row["kind"], "key": row["key"], "payload": json.loads(row["payload"]),
                "lease": lease, "lease_until": until, "attempt": row["attempts"] + 1}

    def get(self, item_id):
        row = self.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def holder(self, item_id, lease):
        """The item, when this lease still holds it; LeaseLost otherwise."""
        self.expire()
        row = self.db.execute("SELECT * FROM items WHERE id=? AND state='leased' AND lease=?",
                              (item_id, lease or "")).fetchone()
        if row is None:
            raise LeaseLost(item_id)
        return dict(row)

    def heartbeat(self, item_id, lease):
        """Renews the lease; returns the new time limit."""
        until = self.now() + self.lease_seconds
        self._finish(item_id, lease, "lease_until=?", (until,))
        return until

    def done(self, item_id, lease):
        self._finish(item_id, lease, "state='done', lease=NULL, lease_until=NULL, finished=?, error=NULL",
                     (self.now(),))

    def fail(self, item_id, lease, error, final=False):
        """The work failed: back to waiting, or failed once it has had max_attempts claims (or at once with final,
        when trying again can't help: the input is gone)."""
        item = self.holder(item_id, lease)
        final = final or item["attempts"] >= self.max_attempts
        self._finish(item_id, lease, "state=?, lease=NULL, worker=NULL, lease_until=NULL, finished=?, error=?",
                     ("failed" if final else "waiting", self.now() if final else None, str(error)[:500]))
        return "failed" if final else "waiting"

    def release(self, item_id, lease, reason="stopped"):
        """The worker was told to stop (pause file, a game, out of its hours): back to waiting, the attempt not
        counted."""
        self._finish(item_id, lease, "state='waiting', lease=NULL, worker=NULL, lease_until=NULL, "
                     "attempts=MAX(attempts-1, 0), error=?", (f"released: {reason}"[:500],))

    def _finish(self, item_id, lease, sets, args):
        self.expire()
        with self._tx():
            cur = self.db.execute(f"UPDATE items SET {sets} WHERE id=? AND state='leased' AND lease=?",
                                  (*args, item_id, lease or ""))
            if cur.rowcount != 1:
                raise LeaseLost(item_id)

    # ---- checks ------------------------------------------------------------------------------------------------

    def status(self):
        """What the probe and the alert read: counts per kind and state, the oldest waiting item's age, the last
        finished item, and the leases held now."""
        self.expire()
        now = self.now()
        counts = {}
        for r in self.db.execute("SELECT kind, state, COUNT(*) n FROM items GROUP BY kind, state"):
            counts.setdefault(r["kind"], {"waiting": 0, "leased": 0, "done": 0, "failed": 0})[r["state"]] = r["n"]
        oldest = self.db.execute("SELECT MIN(added) FROM items WHERE state='waiting'").fetchone()[0]
        last = self.db.execute("SELECT MAX(finished) FROM items WHERE state='done'").fetchone()[0]
        leases = [{"kind": r["kind"], "key": r["key"], "worker": r["worker"], "started": r["started"],
                   "lease_until": r["lease_until"], "attempt": r["attempts"]}
                  for r in self.db.execute("SELECT * FROM items WHERE state='leased' ORDER BY started")]
        return {"counts": counts, "oldest_waiting_s": round(now - oldest) if oldest else None,
                "last_done": last, "leases": leases, "read_at": now}


class _Tx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")

    def __exit__(self, kind, *_):
        self.db.execute("ROLLBACK" if kind else "COMMIT")
