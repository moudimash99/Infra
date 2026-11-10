"""Kubernetes, read only: CronJobs, Jobs, pods, nodes and pod logs.

In the pod: the API with the ServiceAccount token. In development: K8S_API=http://127.0.0.1:8001 (kubectl proxy).
"""
import os

import httpx

from . import source
from ..config import K8S_API, K8S_CA_FILE, K8S_TOKEN_FILE
from ..fmt import to_ts

_client = None


def client():
    global _client
    if _client is None:
        headers, verify = {}, True
        if os.path.exists(K8S_TOKEN_FILE) and K8S_API.startswith("https"):
            headers["Authorization"] = "Bearer " + open(K8S_TOKEN_FILE).read().strip()
            verify = K8S_CA_FILE
        _client = httpx.Client(base_url=K8S_API, headers=headers, verify=verify, timeout=10)
    return _client


def get(path, **params):
    r = client().get(path, params=params)
    r.raise_for_status()
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text


def _job_state(job):
    st = job.get("status", {})
    for c in st.get("conditions") or []:
        if c.get("status") == "True" and c["type"] in ("Complete", "SuccessCriteriaMet"):
            return "ok"
        if c.get("status") == "True" and c["type"] in ("Failed", "FailureTarget"):
            return "failed"
    if st.get("active"):
        return "running"
    return "pending"


def _job(job):
    meta, st = job["metadata"], job.get("status", {})
    owner = next((o["name"] for o in meta.get("ownerReferences") or [] if o["kind"] == "CronJob"), None)
    reason = next((c.get("reason") or c.get("message") for c in st.get("conditions") or []
                   if c["type"] == "Failed" and c.get("status") == "True"), "")
    return {"name": meta["name"], "namespace": meta["namespace"], "cronjob": owner, "state": _job_state(job),
            "start": to_ts(st.get("startTime")), "end": to_ts(st.get("completionTime")),
            "failed_at": next((to_ts(c.get("lastTransitionTime")) for c in st.get("conditions") or []
                               if c["type"] == "Failed" and c.get("status") == "True"), None),
            "reason": reason, "uid": meta["uid"]}


@source("k8s.jobs", ttl=20)
def jobs():
    """Every Job in apps, with its CronJob, state and times; and every CronJob with schedule and pause."""
    cj = get("/apis/batch/v1/namespaces/apps/cronjobs")["items"]
    jb = get("/apis/batch/v1/namespaces/apps/jobs")["items"]
    cronjobs = {c["metadata"]["name"]: {
        "name": c["metadata"]["name"], "schedule": c["spec"]["schedule"], "tz": c["spec"].get("timeZone"),
        "suspended": bool(c["spec"].get("suspend")),
        "last_schedule": to_ts(c.get("status", {}).get("lastScheduleTime")),
        "last_success": to_ts(c.get("status", {}).get("lastSuccessfulTime")),
        "env": {e["name"]: e.get("value", "") for ct in c["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
                for e in ct.get("env") or [] if "value" in e},
    } for c in cj}
    return {"cronjobs": cronjobs, "jobs": sorted((_job(j) for j in jb), key=lambda j: j["start"] or 0, reverse=True)}


def _pod(p):
    meta, spec, st = p["metadata"], p["spec"], p.get("status", {})
    cs = (st.get("containerStatuses") or [{}])[0]
    term = (cs.get("state") or {}).get("terminated") or (cs.get("lastState") or {}).get("terminated") or {}
    return {"name": meta["name"], "namespace": meta["namespace"], "node": spec.get("nodeName"),
            "phase": st.get("phase"), "job": (meta.get("labels") or {}).get("job-name"),
            "app": (meta.get("labels") or {}).get("app"), "runtime": spec.get("runtimeClassName"),
            "start": to_ts(st.get("startTime")), "reason": term.get("reason"),
            "restarts": cs.get("restartCount", 0), "ready": cs.get("ready", False)}


@source("k8s.pods", ttl=20)
def pods():
    """Pods in apps (the jobs and apps the lab shows; its RBAC covers only this namespace)."""
    return [_pod(p) for p in get("/api/v1/namespaces/apps/pods")["items"]]


@source("k8s.nodes", ttl=60)
def nodes():
    out = []
    for n in get("/api/v1/nodes")["items"]:
        ready = next((c["status"] == "True" for c in n["status"]["conditions"] if c["type"] == "Ready"), False)
        addr = next((a["address"] for a in n["status"]["addresses"] if a["type"] == "InternalIP"), "")
        out.append({"name": n["metadata"]["name"], "ready": ready, "ip": addr,
                    "unschedulable": bool(n["spec"].get("unschedulable"))})
    return out


@source("k8s.log", ttl=15)
def log_tail(namespace, pod, lines=200):
    """The last lines of a pod's log ('' when it has none or is gone)."""
    try:
        return get(f"/api/v1/namespaces/{namespace}/pods/{pod}/log", tailLines=lines)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (400, 404):
            return ""
        raise


def pods_of_job(job_name):
    """A Job's pods, newest first (a retried Job has more than one)."""
    r = pods.get()
    return sorted((p for p in (r.data or []) if p["job"] == job_name), key=lambda p: p["start"] or 0, reverse=True)


def latest_pod(job_name):
    found = pods_of_job(job_name)
    return found[0] if found else None


def runs_of(cronjob):
    """Jobs Kubernetes still has for a CronJob (it keeps the last few), newest first."""
    r = jobs.get()
    return [j for j in (r.data or {}).get("jobs", []) if j["cronjob"] == cronjob]
