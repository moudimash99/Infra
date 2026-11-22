#!/usr/bin/env python3
"""Is everything at home up, and is it running the latest code? Read-only.

Reads Infra/services.yaml and checks each service's `probes` against what is really running:
  {k8s: ns/Kind/name}            Deployment / StatefulSet / DaemonSet: all replicas ready
  {k8s: ns/Deployment/name, on_demand: true}   started and stopped by a waker: 0 copies is fine, a start
                                 that takes over 10 minutes is a WARN (no room on the Acer?)
  {k8s: ns/CronJob/name, max_age_h: N}   last good run within N hours, and no run stuck longer than that
  {k8s: ns/CronJob/name, paused_on_purpose: true}   expected to be suspended
  {nodes: all}                   every k3s node Ready
  {docker: name}                 container running in Docker on the server
  {systemd: unit}                unit active on the server
  {pc_task: "\\Task"}            Windows scheduled task enabled, last result 0
  {file_age: path, max_age_min: N}   file on this PC written within N minutes
  {http: url, expect: [200]}     answers with one of those codes (from this PC)
Then lists what runs but is not in the catalog, what the catalog names but is missing, code that is newer than
the image that runs (the image tag's date against the repo's commits, also for code.libs), commits not pushed, and
mistakes in the catalog's own fields (system, lifecycle, depends_on). The full output shows each service's running
images (services.yaml no longer keeps tags). Exit code 1 when anything FAILs.

Run on the PC: python Infra/scripts/status.py   (--json for machine output, --quiet to show only problems)
"""
import datetime as dt
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

import yaml

INFRA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CTX = "homelab"
SERVER = "moudimash99@192.168.1.116"
NOW = dt.datetime.now(dt.timezone.utc)
SEV = {"OK": 0, "WARN": 1, "FAIL": 2}


def run(cmd, timeout=90):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def ago(ts):
    """Hours since an ISO time (UTC 'Z' or with offset)."""
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.astimezone()
    return (NOW - t).total_seconds() / 3600


def human(h):
    return f"{h * 60:.0f} min" if h < 1 else f"{h:.0f} h" if h < 48 else f"{h / 24:.0f} days"


# ---------------------------------------------------------------- gather the live state once
def gather():
    s = {"errors": []}
    rc, out, err = run(["kubectl", "--context", CTX, "get", "deploy,sts,ds,cronjob,job", "-A", "-o", "json"])
    s["k8s"] = {}
    s["jobs"] = []
    if rc == 0:
        for it in json.loads(out)["items"]:
            key = f'{it["metadata"]["namespace"]}/{it["kind"]}/{it["metadata"]["name"]}'
            if it["kind"] == "Job":
                s["jobs"].append(it)
            else:
                s["k8s"][key] = it
    else:
        s["errors"].append("kubectl: " + err.strip()[:200])
    rc, out, err = run(["kubectl", "--context", CTX, "get", "nodes", "-o", "json"])
    s["nodes"] = json.loads(out)["items"] if rc == 0 else None

    marker = "=====SYSTEMD"
    rc, out, err = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", SERVER,
                        f"docker ps -a --format '{{{{.Names}}}}\t{{{{.State}}}}'; echo {marker}; "
                        "for u in nfs-server tailscaled k3s; do echo \"$u $(systemctl is-active $u)\"; done"])
    s["docker"], s["systemd"] = {}, {}
    if rc == 0 and marker in out:
        dpart, spart = out.split(marker, 1)
        for line in dpart.strip().splitlines():
            n, _, st = line.partition("\t")
            s["docker"][n] = st
        for line in spart.strip().splitlines():
            u, _, st = line.partition(" ")
            s["systemd"][u] = st
    else:
        s["docker"] = s["systemd"] = None
        s["errors"].append("ssh to the server: " + (err.strip() or out.strip())[:200])

    ps = ("Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\\Microsoft\\*' } | ForEach-Object { "
          "$i = $_ | Get-ScheduledTaskInfo; [pscustomobject]@{ name = $_.TaskPath + $_.TaskName; "
          "state = [string]$_.State; last = $i.LastRunTime.ToString('o'); result = $i.LastTaskResult } } | ConvertTo-Json")
    s["tasks"] = None
    if os.name == "nt":
        rc, out, err = run(["powershell", "-NoProfile", "-Command", ps])
        if rc == 0 and out.strip():
            t = json.loads(out)
            s["tasks"] = {x["name"]: x for x in (t if isinstance(t, list) else [t])}
    return s


# ---------------------------------------------------------------- probes
def probe_k8s(p, s):
    ref = p["k8s"]
    ns, kind, name = ref.split("/", 2)
    keys = [k for k in s["k8s"] if fnmatch.fnmatch(k, ref)]
    if not keys:
        return "FAIL", f"{kind} {name}: not found in the cluster", keys
    it = s["k8s"][keys[0]]
    st, spec = it.get("status", {}), it.get("spec", {})
    if kind in ("Deployment", "StatefulSet"):
        want, ready = spec.get("replicas", 1), st.get("readyReplicas", 0)
        if p.get("on_demand"):
            if want == 0:
                return "OK", f"{name}: stopped (starts on demand)", keys
            if ready >= want:
                return "OK", f"{name}: running (on demand)", keys
            avail = next((c for c in st.get("conditions", []) if c.get("type") == "Available"), {})
            mins = ago(avail["lastTransitionTime"]) * 60 if avail.get("lastTransitionTime") else 0
            if mins > 10:
                return "WARN", f"{name}: not ready for {int(mins)} min after a start (no room on the Acer?)", keys
            return "OK", f"{name}: starting (on demand)", keys
        if want == 0:
            return "WARN", f"{name}: scaled to 0", keys
        return ("OK" if ready >= want else "FAIL"), f"{name}: {ready}/{want} ready", keys
    if kind == "DaemonSet":
        want, ready = st.get("desiredNumberScheduled", 0), st.get("numberReady", 0)
        return ("OK" if ready >= want and want > 0 else "FAIL"), f"{name}: {ready}/{want} ready", keys
    if kind == "CronJob":
        suspended = spec.get("suspend", False)
        if p.get("paused_on_purpose"):
            return ("OK" if suspended else "WARN"), f"{name}: {'paused (on purpose)' if suspended else 'RUNNING, though it is meant to stay paused'}", keys
        if suspended:
            return "FAIL", f"{name}: paused", keys
        msgs, sev = [], "OK"
        limit = p.get("max_age_h", 26)
        last = st.get("lastSuccessfulTime")
        if not last:
            sev, msg = "FAIL", "never ran successfully"
        else:
            h = ago(last)
            msg = f"last good run {human(h)} ago"
            if h > limit:
                sev, msg = "FAIL", msg + f" (expected within {limit} h)"
        msgs.append(msg)
        for j in s["jobs"]:
            own = (j["metadata"].get("ownerReferences") or [{}])[0]
            if own.get("name") == name and j["metadata"]["namespace"] == ns and j.get("status", {}).get("active"):
                h = ago(j["status"]["startTime"]) if j["status"].get("startTime") else 0
                if h > limit:
                    sev = "FAIL"
                    msgs.append(f"a run has been stuck for {human(h)} ({j['metadata']['name']}) and blocks the next ones")
                else:
                    msgs.append(f"running now ({human(h)})")
        return sev, f"{name}: " + "; ".join(msgs), keys
    return "WARN", f"{ref}: kind not handled", keys


def probe(p, s):
    """Returns (severity, message, k8s keys it covers)."""
    if "k8s" in p:
        return probe_k8s(p, s)
    if "nodes" in p:
        if s["nodes"] is None:
            return "FAIL", "nodes: kubectl failed", []
        bad = [n["metadata"]["name"] for n in s["nodes"]
               if not any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"])]
        return ("FAIL" if bad else "OK"), (f"not Ready: {', '.join(bad)}" if bad else f"{len(s['nodes'])} nodes Ready"), []
    if "docker" in p:
        if s["docker"] is None:
            return "FAIL", f"docker {p['docker']}: server not reachable", []
        st = s["docker"].get(p["docker"])
        return ("OK" if st == "running" else "FAIL"), f"docker {p['docker']}: {st or 'not found'}", []
    if "systemd" in p:
        if s["systemd"] is None:
            return "FAIL", f"{p['systemd']}: server not reachable", []
        st = s["systemd"].get(p["systemd"], "not checked")
        return ("OK" if st == "active" else "FAIL"), f"{p['systemd']}: {st}", []
    if "pc_task" in p:
        if s["tasks"] is None:
            return "WARN", f"{p['pc_task']}: not on the PC, not checked", []
        t = s["tasks"].get(p["pc_task"])
        if not t:
            return "FAIL", f"{p['pc_task']}: no such task", []
        if t["state"] == "Disabled":
            return "FAIL", f"{p['pc_task']}: disabled", []
        res = t["result"]
        when = f"last run {human(ago(t['last']))} ago" if t["last"] and not t["last"].startswith("0001") else "never run"
        if res not in (0, 267009, 267011):   # 0 = ok, 0x41301 running, 0x41303 not yet run
            return "WARN", f"{p['pc_task']}: {when}, last result 0x{res & 0xFFFFFFFF:X} (failed)", []
        return "OK", f"{p['pc_task']}: {when}, ok", []
    if "file_age" in p:
        path, limit = p["file_age"], p.get("max_age_min", 60)
        if not os.path.exists(path):
            return ("WARN" if os.name != "nt" else "FAIL"), f"{os.path.basename(path)}: missing", []
        m = (NOW.timestamp() - os.path.getmtime(path)) / 60
        return ("OK" if m <= limit else "FAIL"), f"{os.path.basename(path)} written {m:.0f} min ago (limit {limit})", []
    if "http" in p:
        url, expect = p["http"], p.get("expect", [200])
        try:
            code = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "infra-status"}), timeout=10).status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:   # noqa: BLE001
            return "FAIL", f"{url}: {type(e).__name__}", []
        return ("OK" if code in expect else "FAIL"), f"{url}: HTTP {code}", []
    return "WARN", f"unknown probe {p}", []


# ---------------------------------------------------------------- code against what really runs
def is_repo(path):
    return os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(os.path.join(path, ".git"))


def git(path, *args):
    return run(["git", "-C", path, *args])[1].strip()


def images(keys, s):
    """Image tags of the workloads a service's probes found, as they run now."""
    out = set()
    for k in keys:
        spec = s["k8s"][k]["spec"]
        pod = (spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec")
               or spec.get("template", {}).get("spec", {}))
        out.update(c.get("image", "") for c in pod.get("containers", []))
    return sorted(out)


def code_check(svc, s, keys):
    """How the PC's repo compares with what runs, by the service's code.version_check:
      (default) image: the code is built into the image; compare the image tag (a date or a commit) with the
                repo's current branch (HEAD), never other branches.
      checkout <ssh-host>:<path>: the service runs from a git checkout there; compare its HEAD with the PC's HEAD
                (a path ending in a folder of checkouts is matched per repo by folder name, for repo globs like fm-*).
      none ...: no automatic comparison (e.g. code pushed as files); only uncommitted changes are reported.
    code.libs: library repos built into the image (e.g. bgg-core), always compared the image way.
    Every repo: commits on its branch not pushed to origin (its upstream, or origin/<branch>)."""
    code = svc.get("code") or {}
    if not isinstance(code, dict):
        return None
    glob = __import__("glob").glob
    how = str(code.get("version_check") or "image").strip()
    def repos(entry):
        pat = str(entry or "").split(" (")[0].strip()
        return [p for p in (glob(pat) if "*" in pat else [pat]) if pat and is_repo(p)]

    pattern = str(code.get("repo") or "").split(" (")[0].strip()
    own = repos(code.get("repo"))
    todo = [(p, how) for p in own] + [(p, "image") for lib in code.get("libs") or [] for p in repos(lib)]
    if not todo:
        return None
    notes = []
    for path, check in todo:
        label = os.path.basename(path) + ": " if len(todo) > 1 else ""
        n_dirty = len([x for x in git(path, "status", "--porcelain").splitlines() if x.strip()])
        unpushed = git(path, "rev-list", "--count", "@{upstream}..HEAD")
        if not unpushed.isdigit():   # no upstream set: compare with origin's branch of the same name
            branch = git(path, "branch", "--show-current")
            unpushed = git(path, "rev-list", "--count", f"origin/{branch}..HEAD") if branch else ""
        if unpushed.isdigit() and unpushed != "0":
            notes.append(f"{label}{unpushed} commit(s) not pushed to origin")
        if check.startswith("checkout "):
            host, _, where = check.split(None, 1)[1].partition(":")
            remote = where if len(own) == 1 else f"{where.rstrip('/')}/{os.path.basename(path)}"
            rc, out, _ = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
                              f"git -C '{remote}' rev-parse HEAD 2>/dev/null"])
            there, here = out.strip(), git(path, "rev-parse", "HEAD")
            if rc == 0 and there and there != here:
                ahead = git(path, "rev-list", "--count", f"{there}..HEAD")
                behind = git(path, "rev-list", "--count", f"HEAD..{there}")
                notes.append(f"{label}runs {there[:7]}; the PC is {ahead or '?'} commit(s) ahead"
                             + (f", {behind} behind" if behind not in ("", "0") else ""))
        elif check == "image":
            tags = {i.rsplit(":", 1)[-1] for i in images(keys, s)}
            # Commits that change only README.md (e.g. its "Where it runs" section, scripts/readmes.py) don't count.
            code_only = ["--", ".", ":(exclude)README.md"]
            for t in sorted(tags):
                if re.fullmatch(r"[0-9a-f]{7,40}", t) and git(path, "cat-file", "-t", t) == "commit":
                    newer = git(path, "rev-list", "--count", f"{t}..HEAD", *code_only)
                elif re.match(r"\d{4}-\d{2}-\d{2}", t):
                    newer = git(path, "rev-list", "--count", "HEAD", f"--after={t[:10]} 23:59", *code_only)
                else:
                    continue
                if newer not in ("", "0"):
                    notes.append(f"{label}{newer} commit(s) on {git(path, 'branch', '--show-current') or 'HEAD'} newer than the image ({t})")
        if n_dirty:
            notes.append(f"{label}{n_dirty} uncommitted change(s)")
    return {"repo": pattern, "check": how.split(" (")[0], "notes": notes}


LIFECYCLES = {"production", "experimental", "deprecated"}


def catalog_problems(services):
    """The catalog's own fields: every entry has a system and a known lifecycle, and depends_on names entries."""
    names, out = {svc["name"] for svc in services}, []
    for svc in services:
        n = svc["name"]
        if not svc.get("system"):
            out.append(f"{n}: no system")
        if svc.get("lifecycle") not in LIFECYCLES:
            out.append(f"{n}: lifecycle {svc.get('lifecycle')!r} is not one of {', '.join(sorted(LIFECYCLES))}")
        out += [f"{n}: depends_on {d!r} is not an entry" for d in svc.get("depends_on") or [] if d not in names]
    return out


# ---------------------------------------------------------------- main
def main():
    as_json, quiet = "--json" in sys.argv, "--quiet" in sys.argv
    services = yaml.safe_load(open(os.path.join(INFRA, "services.yaml"), encoding="utf-8"))
    s = gather()
    covered, results = set(), []
    for svc in services:
        rows, keys = [], []
        for p in svc.get("probes") or []:
            sev, msg, ks = probe(p, s)
            rows.append((sev, msg))
            keys += ks
        covered.update(keys)
        worst = max((r[0] for r in rows), key=SEV.get, default="WARN")
        results.append({"name": svc["name"], "status": worst if rows else "WARN",
                        "checks": [{"status": a, "detail": b} for a, b in rows] or [{"status": "WARN", "detail": "no probes"}],
                        "images": images(keys, s),
                        "code": code_check(svc, s, keys)})
    named_docker = {p["docker"] for svc in services for p in (svc.get("probes") or []) if "docker" in p}
    unknown = sorted(k for k in s["k8s"] if k not in covered)
    stopped_docker = sorted(n for n, st in (s["docker"] or {}).items() if n not in named_docker and st == "running")
    report = {"time": NOW.astimezone().strftime("%Y-%m-%d %H:%M"), "services": results,
              "not_in_catalog": unknown + [f"docker/{n}" for n in stopped_docker],
              "catalog_problems": catalog_problems(services), "errors": s["errors"]}
    if as_json:
        print(json.dumps(report, indent=1))
    else:
        counts = {k: sum(r["status"] == k for r in results) for k in SEV}
        print(f"Home services, {report['time']}: {counts['OK']} OK, {counts['WARN']} WARN, {counts['FAIL']} FAIL\n")
        for r in sorted(results, key=lambda r: -SEV[r["status"]]):
            if quiet and r["status"] == "OK":
                continue
            print(f"{r['status']:<4}  {r['name']}")
            for c in r["checks"]:
                if not quiet or c["status"] != "OK":
                    print(f"        {'' if c['status'] == 'OK' else c['status'] + ': '}{c['detail']}")
            if r["images"] and not quiet:
                print(f"        runs {', '.join(r['images'])}")
        stale = [(r["name"], r["code"]) for r in results if r["code"] and r["code"]["notes"]]
        if stale:
            print("\nCode on the PC that is not what runs (newer commits, or changes never committed):")
            for n, c in stale:
                print(f"  {n:<18} {'; '.join(c['notes'])}   [{c['repo']}]")
        if report["not_in_catalog"]:
            print("\nRunning but not in services.yaml (add them):")
            for k in report["not_in_catalog"]:
                print("  " + k)
        if report["catalog_problems"]:
            print("\nMistakes in services.yaml:")
            for k in report["catalog_problems"]:
                print("  " + k)
        for e in report["errors"]:
            print("\nCould not check: " + e)
    sys.exit(1 if any(r["status"] == "FAIL" for r in results) or s["errors"] else 0)


if __name__ == "__main__":
    main()
