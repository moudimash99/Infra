"""Each data source is a function wrapped in `source(name, ttl)`: called at most once per ttl, its errors caught.

A page asks for `X.get()` and gets a Reading: ok, data, error, read_at. A broken source (Frigate down, a database
locked) becomes one grey card with its error; it never breaks the page.
"""
import dataclasses
import logging
import threading
import time
import traceback

log = logging.getLogger("lab")


@dataclasses.dataclass
class Reading:
    ok: bool
    data: object = None
    error: str = ""
    read_at: float = 0.0


class Source:
    def __init__(self, name, ttl, fn):
        self.name, self.ttl, self.fn = name, ttl, fn
        self._lock = threading.Lock()
        self._cache = {}          # args -> Reading

    def get(self, *args):
        now = time.time()
        hit = self._cache.get(args)
        if hit and now - hit.read_at < self.ttl:
            return hit
        with self._lock:      # one read at a time per source: a slow source isn't read ten times in parallel
            hit = self._cache.get(args)
            if hit and time.time() - hit.read_at < self.ttl:
                return hit
            try:
                reading = Reading(True, self.fn(*args), "", time.time())
            except Exception as e:
                log.warning("source %s failed: %s", self.name, traceback.format_exc(limit=3))
                reading = Reading(False, hit.data if hit else None, f"{type(e).__name__}: {e}"[:300], time.time())
            if len(self._cache) > 200:
                self._cache.clear()
            self._cache[args] = reading
            return reading


def source(name, ttl):
    def wrap(fn):
        return Source(name, ttl, fn)
    return wrap
