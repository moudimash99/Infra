"""The GPU worker (moudimash99/Infra#14): takes work items from the hub and runs them on this machine's GPU, as a
plain scheduled task (no Kubernetes, no VM). The PC is the first worker; later, volunteers.

Each run: if this machine may work now, claim an item, download its input, run the kind's command on it, upload
the result; then the next, until nothing waits, the run's time is up or the machine may not work any more. One item
at a time, one run at a time (a lock file). While the command runs, every check_s seconds it checks again and stops
the command at once (the whole process tree) when:
  - the pause file exists                          - the time is outside "hours"
  - a process in busy_processes runs (a game)      - the user is back (idle_minutes set and input in the last 2 min)
  - the GPU is at max_gpu_c or hotter, or its temperature can't be read while max_gpu_c is set
  - the command's processes use more than memory_cap_mib together (working set)
A stopped item is released (back to the queue, not counted as a try). A command that fails, or writes no output, is
reported failed (back to the queue; failed for good after 3 tries, see workqueue.py). If this machine goes to sleep
the lease runs out and the hub puts the item back by itself.

  python worker.py [--config ~/.gpu-worker.json] [--once] [--dry-run]
    --dry-run  prints whether it may work now, and why not, then exits (claims nothing)

Config (JSON), with what each key does when left out:
  hub             "http://<hub>:<port>" (required); token in token_file (default ~/.gpu-worker-token)
  worker          name shown in the hub's leases (default: the computer's name)
  kinds           {"whisper": {"command": [...], "cwd": "...", "env": {...}, "output_ext": ".json3"}} (required);
                  in command, {input} is the downloaded file and {output} the file the command must write
  hours           "01:00-07:30" local time, may cross midnight (default: any time)
  idle_minutes    start only after this long without keyboard or mouse, stop when the user is back (default: off)
  busy_processes  process names (case blind, e.g. "eldenring.exe") that stop the work (default: none)
  max_gpu_c       stop at this GPU temperature (default: off)
  max_gpu_mem_used_mib  do not start while other programs use more GPU memory than this (default: off)
  memory_cap_mib  stop the command above this much memory (default 2560)
  max_minutes     longest run, then exit (default 480)
  newest_first    take from the end of the queue, away from the main worker (default true)
  pause_file      default ~/.gpu-worker-pause      work_dir  default ~/.gpu-worker (inputs, outputs, log, lock)
"""
import argparse
import datetime as dt
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

IS_WINDOWS = os.name == "nt"
HOME = os.path.expanduser("~")
DEFAULTS = {"worker": socket.gethostname(), "hours": None, "idle_minutes": None, "busy_processes": [],
            "max_gpu_c": None, "max_gpu_mem_used_mib": None, "memory_cap_mib": 2560, "max_minutes": 480,
            "newest_first": True, "check_s": 15, "heartbeat_s": 120,
            "pause_file": os.path.join(HOME, ".gpu-worker-pause"), "work_dir": os.path.join(HOME, ".gpu-worker"),
            "token_file": os.path.join(HOME, ".gpu-worker-token")}
BACK_S = 120          # input within this many seconds = the user is back
LOG_FILE = None       # main() sets it to <work_dir>/worker.log: a scheduled task's printed output goes nowhere


def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    if LOG_FILE:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---- what the machine says --------------------------------------------------------------------------------------

def in_hours(spec, now):
    """True when now (a datetime) is inside "HH:MM-HH:MM" (which may cross midnight); always True without a spec."""
    if not spec:
        return True
    start, end = ([int(x) for x in part.strip().split(":")] for part in spec.split("-"))
    t, a, b = now.hour * 60 + now.minute, start[0] * 60 + start[1], end[0] * 60 + end[1]
    return a <= t < b if a <= b else (t >= a or t < b)


def processes():
    """[(pid, parent pid, name)] of every process."""
    if IS_WINDOWS:
        return _win_processes()
    out = subprocess.run(["ps", "-eo", "pid=,ppid=,comm="], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            rows.append((int(parts[0]), int(parts[1]), os.path.basename(parts[2])))
    return rows


def tree(root, procs):
    """root and every process below it."""
    kids, out, todo = {}, [], [root]
    for pid, ppid, _ in procs:
        kids.setdefault(ppid, []).append(pid)
    while todo:
        pid = todo.pop()
        if pid in out:
            continue
        out.append(pid)
        todo += kids.get(pid, [])
    return out


def memory_mib(pids):
    """Working set of these processes together, in MiB (resident memory; the Acer's figures are RSS too)."""
    total = 0
    for pid in pids:
        if IS_WINDOWS:
            total += _win_working_set(pid)
        else:
            try:
                with open(f"/proc/{pid}/status") as f:
                    total += next(int(l.split()[1]) * 1024 for l in f if l.startswith("VmRSS:"))
            except (OSError, StopIteration):
                pass
    return total / 2 ** 20


def idle_seconds():
    """Seconds since the last keyboard or mouse input (Windows); None when it can't tell."""
    if not IS_WINDOWS:
        return None
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000


def gpu():
    """{"temp_c", "mem_used_mib"} from nvidia-smi; None when it can't be read."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu,memory.used",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=20).stdout
        temp, used = (float(x) for x in out.splitlines()[0].split(","))
        return {"temp_c": temp, "mem_used_mib": used}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def kill_tree(proc):
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        for pid in reversed(tree(proc.pid, processes())):
            try:
                os.kill(pid, 9)
            except OSError:
                pass
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()


def _win_processes():
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)          # TH32CS_SNAPPROCESS
    if snap in (None, wintypes.HANDLE(-1).value):
        return []
    rows, entry = [], PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        ok = k32.Process32FirstW(wintypes.HANDLE(snap), ctypes.byref(entry))
        while ok:
            rows.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = k32.Process32NextW(wintypes.HANDLE(snap), ctypes.byref(entry))
    finally:
        k32.CloseHandle(wintypes.HANDLE(snap))
    return rows


def _win_working_set(pid):
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                           "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                           "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)              # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return 0
    try:
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        if k32.K32GetProcessMemoryInfo(wintypes.HANDLE(h), ctypes.byref(pmc), pmc.cb):
            return pmc.WorkingSetSize
        return 0
    finally:
        k32.CloseHandle(wintypes.HANDLE(h))


# ---- may it work now --------------------------------------------------------------------------------------------

class Gates:
    """Why this machine may not work now (a short reason), or None. The probes are passed in so tests can fake them."""

    def __init__(self, conf, now=dt.datetime.now, procs=processes, idle=idle_seconds, gpu_info=gpu):
        self.c, self.now, self.procs, self.idle, self.gpu = conf, now, procs, idle, gpu_info

    def why_not(self, starting):
        c = self.c
        if os.path.exists(c["pause_file"]):
            return "pause file"
        if not in_hours(c["hours"], self.now()):
            return f"outside hours {c['hours']}"
        busy = {n.lower() for n in c["busy_processes"]}
        if busy:
            running = sorted({name for _, _, name in self.procs() if name.lower() in busy})
            if running:
                return "busy: " + ", ".join(running)
        if c["idle_minutes"] is not None:
            idle = self.idle()
            if idle is None:
                return "can't tell whether the user is here"
            need = c["idle_minutes"] * 60 if starting else BACK_S
            if idle < need:
                return f"the user is here (input {idle:.0f} s ago)"
        if c["max_gpu_c"] is not None or (starting and c["max_gpu_mem_used_mib"] is not None):
            g = self.gpu()
            if g is None:
                return "can't read the GPU (nvidia-smi)"
            if c["max_gpu_c"] is not None and g["temp_c"] >= c["max_gpu_c"]:
                return f"GPU at {g['temp_c']:.0f} C"
            if starting and c["max_gpu_mem_used_mib"] is not None and g["mem_used_mib"] > c["max_gpu_mem_used_mib"]:
                return f"GPU in use by another program ({g['mem_used_mib']:.0f} MiB)"
        return None


# ---- the hub ----------------------------------------------------------------------------------------------------

class HubError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"{code} {msg}")
        self.code = code


class Client:
    def __init__(self, url, token, timeout=60):
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout

    def call(self, method, path, body=None, lease=None, raw=False, to_file=None):
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if lease:
            req.add_header("X-Lease", lease)
        if data is not None and not raw:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if to_file:
                    with open(to_file, "wb") as f:
                        while chunk := resp.read(1024 * 1024):
                            f.write(chunk)
                    return resp.headers.get("X-Filename", "")
                text = resp.read()
                return json.loads(text) if text else None
        except urllib.error.HTTPError as e:
            raise HubError(e.code, e.read()[:300].decode("utf-8", "replace")) from None

    def claim(self, worker, kinds, newest_first):
        return self.call("POST", "/claim", {"worker": worker, "kinds": kinds, "newest_first": newest_first})


# ---- one item ---------------------------------------------------------------------------------------------------

def run_item(client, item, conf, gates, clock=time.monotonic):
    """Runs one claimed item to the end; returns "done", "failed", "stopped: <reason>" or "lost"."""
    kind = conf["kinds"][item["kind"]]
    lease, iid, key = item["lease"], item["id"], item["key"]
    os.makedirs(conf["work_dir"], exist_ok=True)
    inp = os.path.join(conf["work_dir"], f"{key}.input")
    out = os.path.join(conf["work_dir"], key + kind.get("output_ext", ".out"))
    logf = os.path.join(conf["work_dir"], f"{key}.log")
    for p in (inp, out):
        if os.path.exists(p):
            os.remove(p)
    try:
        name = client.call("GET", f"/items/{iid}/input", lease=lease, to_file=inp)
        if name and "." in name:                         # keep the extension: ffmpeg and Whisper read it
            renamed = os.path.join(conf["work_dir"], key + "." + name.rsplit(".", 1)[1])
            os.replace(inp, renamed)
            inp = renamed
        cmd = [part.replace("{input}", inp).replace("{output}", out) for part in kind["command"]]
        env = {**os.environ, **{k: str(v) for k, v in kind.get("env", {}).items()}}
        log(f"{item['kind']} {key} (try {item.get('attempt', '?')}): running")
        with open(logf, "wb") as lf:
            proc = subprocess.Popen(cmd, cwd=kind.get("cwd") or None, env=env, stdout=lf, stderr=subprocess.STDOUT)
            last_beat, peak = clock(), 0.0
            while proc.poll() is None:
                time.sleep(conf["check_s"])
                if proc.poll() is not None:
                    break
                reason = gates.why_not(starting=False)
                mem = memory_mib(tree(proc.pid, processes()))
                peak = max(peak, mem)
                if reason is None and mem > conf["memory_cap_mib"]:
                    reason = f"memory {mem:.0f} MiB over the cap {conf['memory_cap_mib']}"
                if reason:
                    kill_tree(proc)
                    log(f"{key}: stopped, {reason}")
                    if reason.startswith("memory"):    # the item itself is too big: counts as a try
                        client.call("POST", f"/items/{iid}/fail", {"error": reason}, lease=lease)
                        return "failed"
                    client.call("POST", f"/items/{iid}/release", {"reason": reason}, lease=lease)
                    return f"stopped: {reason}"
                if clock() - last_beat >= conf["heartbeat_s"]:
                    client.call("POST", f"/items/{iid}/heartbeat", {}, lease=lease)
                    last_beat = clock()
        code = proc.returncode
        if code != 0 or not os.path.exists(out):
            tail = open(logf, "rb").read()[-400:].decode("utf-8", "replace")
            error = f"exit {code}" + ("" if os.path.exists(out) else ", no output") + f": {tail}"
            log(f"{key}: failed, {error[:200]}")
            client.call("POST", f"/items/{iid}/fail", {"error": error}, lease=lease)
            return "failed"
        with open(out, "rb") as f:
            res = client.call("PUT", f"/items/{iid}/result", f.read(), lease=lease, raw=True)
        log(f"{key}: done (peak {peak:.0f} MiB{', the hub already had a result' if res.get('kept_existing') else ''})")
        return "done"
    except HubError as e:
        if e.code == 409:
            log(f"{key}: lease lost (the item went back to the queue)")
            return "lost"
        raise
    finally:
        if "proc" in locals():
            kill_tree(proc)
        for p in (inp, out):
            if os.path.exists(p):
                os.remove(p)


# ---- a run ------------------------------------------------------------------------------------------------------

def load_conf(path):
    conf = {**DEFAULTS, **json.load(open(path, encoding="utf-8"))}
    for k in ("pause_file", "work_dir", "token_file"):
        conf[k] = os.path.expanduser(conf[k])
    if not conf.get("hub") or not conf.get("kinds"):
        sys.exit(f"{path}: hub and kinds are required")
    return conf


def take_lock(work_dir):
    """An open file locked for this process (released by the OS when it ends); None when another run holds it."""
    os.makedirs(work_dir, exist_ok=True)
    f = open(os.path.join(work_dir, "worker.lock"), "a+")
    try:
        if IS_WINDOWS:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def run(conf, client, gates, once=False, clock=time.monotonic):
    """Works items until it may not, nothing waits, or max_minutes are up. Returns {result: count}."""
    lock = take_lock(conf["work_dir"])
    if lock is None:
        log("another run is working: nothing to do")
        return {}
    counts, end = {}, clock() + conf["max_minutes"] * 60
    try:
        while clock() < end:
            reason = gates.why_not(starting=True)
            if reason:
                log(f"not working: {reason}")
                break
            try:
                item = client.claim(conf["worker"], sorted(conf["kinds"]), conf["newest_first"])
                if not item:
                    log("nothing waits in the queue")
                    break
                result = run_item(client, item, conf, gates)
            except (HubError, OSError) as e:          # the hub is down or refused: the lease runs out by itself
                log(f"hub: {e}")
                counts["hub error"] = counts.get("hub error", 0) + 1
                break
            counts[result.split(":")[0]] = counts.get(result.split(":")[0], 0) + 1
            if result.startswith("stopped") or once:
                break
    finally:
        lock.close()
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=os.path.join(HOME, ".gpu-worker.json"))
    ap.add_argument("--once", action="store_true", help="one item at most")
    ap.add_argument("--dry-run", action="store_true", help="say whether it may work now, claim nothing")
    a = ap.parse_args(argv)
    conf = load_conf(a.config)
    gates = Gates(conf)
    if a.dry_run:
        print(gates.why_not(starting=True) or "may work now")
        return
    global LOG_FILE
    os.makedirs(conf["work_dir"], exist_ok=True)
    LOG_FILE = os.path.join(conf["work_dir"], "worker.log")
    token = open(conf["token_file"], encoding="utf-8").read().strip()
    counts = run(conf, Client(conf["hub"], token), gates, once=a.once)
    log(f"run ends: {counts or 'no items'}")


if __name__ == "__main__":
    main()
