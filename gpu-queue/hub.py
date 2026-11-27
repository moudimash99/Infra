"""The GPU queue's hub (moudimash99/Infra#14): the queue over HTTP, next to the data it feeds.

Workers (the PC first, later volunteers) claim an item, download its input, upload the result; the hub writes the
result where the project keeps it today (for Whisper: bg-taste-match's captions/<id>.json3) and marks the item done.
Standard library only.

  python hub.py serve  --db Q.db --kinds kinds.json [--port 8795]   the HTTP service (token in GPUQ_TOKEN)
  python hub.py fill   --db Q.db --kinds kinds.json KIND            add every input that has no result yet
  python hub.py add    --db Q.db KIND < keys.txt                    add the keys given, one a line
  python hub.py status --db Q.db                                    counts, oldest waiting, last done (JSON)
  python hub.py retry  --db Q.db KIND KEY                           a failed item back in the queue

kinds.json, one entry per kind of work:
  {"whisper": {"input_dir": "/work/audio", "result_dir": "/work/captions", "result_ext": ".json3",
               "check": "json", "skip_ext": [".tries"]}}
  input: the one file <key>.* in input_dir (not .part/.ytdl/.tmp); result: <result_dir>/<key><result_ext>, written
  only when it doesn't exist yet (the Acer's own job may have done the item meanwhile: its file is kept).
  skip_ext: fill leaves out a key with <result_dir>/<key><ext> (bg-taste-match's .tries: the Acer is on it or gave up).

HTTP (all but /healthz and /metrics need "Authorization: Bearer <GPUQ_TOKEN>"; item calls need "X-Lease"):
  POST /claim {"worker", "kinds"?, "newest_first"?}  -> 200 the item, 204 nothing waits
  GET  /items/<id>/input          the input file          PUT  /items/<id>/result   the result (body) -> done
  POST /items/<id>/heartbeat      renew the lease         POST /items/<id>/fail     {"error"}
  POST /items/<id>/release        {"reason"} back to waiting, not counted
  POST /items {"kind", "keys": [...]}  add               GET  /status, /metrics, /healthz
"""
import argparse
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from workqueue import LeaseLost, Queue

KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")      # YouTube ids and the like: never a path
PARTIAL = (".part", ".ytdl", ".tmp")
MAX_RESULT_BYTES = 64 * 1024 * 1024


class Hub:
    """The queue plus where each kind's inputs and results live. One lock: SQLite is used from the server's threads."""

    def __init__(self, queue, kinds):
        self.q, self.kinds, self.lock = queue, kinds, threading.Lock()

    def input_path(self, kind, key):
        conf = self.kinds[kind]
        folder = conf["input_dir"]
        try:
            names = sorted(n for n in os.listdir(folder)
                           if n.rsplit(".", 1)[0] == key and not n.endswith(PARTIAL))
        except FileNotFoundError:
            return None
        return os.path.join(folder, names[0]) if names else None

    def result_path(self, kind, key):
        conf = self.kinds[kind]
        return os.path.join(conf["result_dir"], key + conf.get("result_ext", ""))

    def fill(self, kind):
        """Adds every input with no result yet; returns how many were new."""
        conf = self.kinds[kind]
        skip = conf.get("skip_ext", [])
        new = 0
        for name in sorted(os.listdir(conf["input_dir"])):
            key = name.rsplit(".", 1)[0]
            if name.endswith(PARTIAL) or not KEY_RE.match(key) or os.path.exists(self.result_path(kind, key)):
                continue
            if any(os.path.exists(os.path.join(conf["result_dir"], key + ext)) for ext in skip):
                continue
            with self.lock:
                new += self.q.add(kind, key)
        return new

    def claim(self, worker, kinds=None, newest_first=False):
        """The next item that still needs doing: one whose result appeared meanwhile is marked done, one whose input
        is gone is failed (a person looks at it), and the next is taken."""
        kinds = [k for k in (kinds or self.kinds) if k in self.kinds]
        if not kinds:
            return None
        for _ in range(50):
            with self.lock:
                item = self.q.claim(worker, kinds, newest_first)
                if item is None:
                    return None
                if os.path.exists(self.result_path(item["kind"], item["key"])):
                    self.q.done(item["id"], item["lease"])
                    continue
                if self.input_path(item["kind"], item["key"]) is None:
                    self.q.fail(item["id"], item["lease"], "no input file", final=True)
                    continue
                return item
        return None

    def save_result(self, item, body):
        """Writes the result (unless the item's result already exists) and marks the item done. ValueError when the
        result doesn't pass the kind's check: the item is failed, so a bad worker can't fill the folder."""
        conf = self.kinds[item["kind"]]
        if conf.get("check") == "json":
            try:
                json.loads(body)
            except ValueError as e:
                raise ValueError(f"result is not JSON: {e}") from None
        if not body:
            raise ValueError("empty result")
        path = self.result_path(item["kind"], item["key"])
        kept = os.path.exists(path)
        if not kept:
            tmp = f"{path}.{secrets.token_hex(4)}.tmp"
            with open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, path)
        with self.lock:
            self.q.done(item["id"], item["lease"])
        return {"ok": True, "kept_existing": kept}

    def metrics(self):
        with self.lock:
            st = self.q.status()
        out = ["# HELP gpu_queue_items Work items per kind and state.", "# TYPE gpu_queue_items gauge"]
        for kind, states in sorted(st["counts"].items()):
            for state, n in states.items():
                out.append(f'gpu_queue_items{{kind="{kind}",state="{state}"}} {n}')
        out += ["# HELP gpu_queue_oldest_waiting_seconds Age of the oldest waiting item (0 when none waits).",
                "# TYPE gpu_queue_oldest_waiting_seconds gauge",
                f"gpu_queue_oldest_waiting_seconds {st['oldest_waiting_s'] or 0}",
                "# HELP gpu_queue_last_done_timestamp_seconds When an item last finished (0 when none has).",
                "# TYPE gpu_queue_last_done_timestamp_seconds gauge",
                f"gpu_queue_last_done_timestamp_seconds {st['last_done'] or 0:.0f}",
                "# HELP gpu_queue_leases Items being worked on now.", "# TYPE gpu_queue_leases gauge",
                f"gpu_queue_leases {len(st['leases'])}"]
        return "\n".join(out) + "\n"


def handler_for(hub, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "gpu-queue"

        def log_message(self, fmt, *args):          # one line per call, without the client's address
            sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), fmt % args))

        def _send(self, code, body=b"", ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self, limit=1024 * 1024):
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
                raise OverflowError
            return self.rfile.read(n) if n else b""

        def _json(self):
            raw = self._body()
            return json.loads(raw) if raw else {}

        def _authed(self):
            got = self.headers.get("Authorization", "")
            return bool(token) and secrets.compare_digest(got, f"Bearer {token}")

        def _item(self, item_id):
            with hub.lock:
                return hub.q.holder(item_id, self.headers.get("X-Lease", ""))

        def _route(self, method):
            path = urlparse(self.path).path.rstrip("/")
            if method == "GET" and path == "/healthz":
                return self._send(200, {"ok": True})
            if method == "GET" and path == "/metrics":
                return self._send(200, hub.metrics(), "text/plain; version=0.0.4")
            if not self._authed():
                return self._send(401, {"error": "token"})
            try:
                if method == "GET" and path == "/status":
                    with hub.lock:
                        return self._send(200, hub.q.status())
                if method == "POST" and path == "/claim":
                    req = self._json()
                    item = hub.claim(str(req.get("worker", "?"))[:64], req.get("kinds"), bool(req.get("newest_first")))
                    return self._send(200, item) if item else self._send(204)
                if method == "POST" and path == "/items":
                    req = self._json()
                    kind, keys = req.get("kind"), req.get("keys") or []
                    if kind not in hub.kinds or not all(isinstance(k, str) and KEY_RE.match(k) for k in keys):
                        return self._send(400, {"error": "unknown kind or bad key"})
                    with hub.lock:
                        new = sum(hub.q.add(kind, k) for k in keys)
                    return self._send(200, {"added": new})
                m = re.fullmatch(r"/items/(\d+)/(input|result|heartbeat|fail|release)", path)
                if not m:
                    return self._send(404, {"error": "no such call"})
                item, call = self._item(int(m.group(1))), m.group(2)
                if method == "GET" and call == "input":
                    src = hub.input_path(item["kind"], item["key"])
                    if src is None:
                        return self._send(404, {"error": "input gone"})
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(os.path.getsize(src)))
                    self.send_header("X-Filename", os.path.basename(src))
                    self.end_headers()
                    with open(src, "rb") as f:
                        shutil.copyfileobj(f, self.wfile, 1024 * 1024)
                    return
                if method == "PUT" and call == "result":
                    body = self._body(MAX_RESULT_BYTES)
                    try:
                        return self._send(200, hub.save_result(item, body))
                    except ValueError as e:
                        with hub.lock:
                            state = hub.q.fail(item["id"], item["lease"], e)
                        return self._send(422, {"error": str(e), "state": state})
                if method == "POST" and call == "heartbeat":
                    with hub.lock:
                        return self._send(200, {"lease_until": hub.q.heartbeat(item["id"], item["lease"])})
                if method == "POST" and call == "fail":
                    with hub.lock:
                        return self._send(200, {"state": hub.q.fail(item["id"], item["lease"],
                                                                     self._json().get("error", "?"))})
                if method == "POST" and call == "release":
                    with hub.lock:
                        hub.q.release(item["id"], item["lease"], self._json().get("reason", "stopped"))
                    return self._send(200, {"state": "waiting"})
                return self._send(405, {"error": "method"})
            except LeaseLost:
                return self._send(409, {"error": "lease lost: the item went back to the queue or is finished"})
            except OverflowError:
                return self._send(413, {"error": "too big"})
            except ValueError:
                return self._send(400, {"error": "JSON expected"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

    return Handler


def make_server(hub, token, host="0.0.0.0", port=8795):
    return ThreadingHTTPServer((host, port), handler_for(hub, token))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["serve", "fill", "add", "status", "retry"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--db", required=True)
    ap.add_argument("--kinds", help="kinds.json (serve, fill)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8795)
    a = ap.parse_args(argv)
    kinds = json.load(open(a.kinds, encoding="utf-8")) if a.kinds else {}
    hub = Hub(Queue(a.db), kinds)
    if a.cmd == "serve":
        token = os.environ.get("GPUQ_TOKEN", "")
        if not token:
            sys.exit("GPUQ_TOKEN is not set")
        print(f"gpu-queue on :{a.port}, kinds {sorted(kinds)}", flush=True)
        make_server(hub, token, a.host, a.port).serve_forever()
    elif a.cmd == "fill":
        print(json.dumps({"added": hub.fill(a.args[0])}))
    elif a.cmd == "add":
        keys = [k.strip() for k in sys.stdin if KEY_RE.match(k.strip())]
        print(json.dumps({"added": sum(hub.q.add(a.args[0], k) for k in keys)}))
    elif a.cmd == "status":
        print(json.dumps(hub.q.status(), indent=1))
    elif a.cmd == "retry":
        print(json.dumps({"retried": hub.q.retry(a.args[0], a.args[1])}))


if __name__ == "__main__":
    main()
