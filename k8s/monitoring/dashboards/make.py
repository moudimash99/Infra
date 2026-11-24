"""The homelab's own Grafana dashboards, made from short definitions so they stay consistent. Run after a change:

    python k8s/monitoring/dashboards/make.py      (writes home.json, usage.json, jobs.json, pc.json, scheduling.json,
                                                   ranks.json here)

Phone first: Grafana stacks panels in one column below 769 px, in grid order, so each dashboard reads top to bottom
as answers: the one-glance stats first (short stat tiles), then lists (bar gauges and short tables), then charts.
Details are in collapsed rows. Each machine keeps its own colour everywhere (server blue, Acer orange).

  home   "Is anything wrong?"          alerts, machines, the GPU, failed jobs: green or red at a glance
  usage  "What is using the machines?"  memory / CPU / GPU per app and per program, now and over time
  jobs   "Scheduled jobs"               every run of the last week: when, how long, result, memory, OOM kills
  pc     "PC jobs"                       the Windows PC's scheduled jobs, from its reports to the lab
  scheduling "Who gets in"           each app on a machine: running, waiting for room, failed; booked vs used memory
  ranks  "Ranks and memory"           every app: rank, what the rank may do, what it is, booked, real memory over 24 h
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DS = {"type": "prometheus", "uid": "prometheus"}
MACHINE_COLORS = {"moudimash99": "#2a78d6", "acer-laptop": "#eb6834"}
GREEN, ORANGE, RED, BLUE, GREY = "#0ca30c", "#fab219", "#d03b3b", "#2a78d6", "#898781"
APP = r'"pod_app", "$1", "pod", "^(.+?)(-[0-9]{6,}|-[a-f0-9]{8,10})?-[a-z0-9]{5}$"'   # pod -> its app / job
CRON = r'"cronjob", "$1", "pod", "^(.+)-[0-9]{6,}-[a-z0-9]{5}$"'                       # a CronJob's pod -> the CronJob


def steps(*pairs):
    """Threshold steps: steps(GREEN, 80, ORANGE, 95, RED)."""
    out = [{"color": pairs[0], "value": None}]
    for i in range(1, len(pairs), 2):
        out.append({"color": pairs[i + 1], "value": pairs[i]})
    return {"mode": "absolute", "steps": out}


def target(expr, legend="", instant=False, fmt=None, ref="A"):
    t = {"refId": ref, "datasource": DS, "expr": expr, "legendFormat": legend or "__auto", "range": not instant,
         "instant": instant}
    if fmt:
        t["format"] = fmt
    return t


def machine_overrides():
    return [{"matcher": {"id": "byName", "options": m},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]} for m, c in MACHINE_COLORS.items()]


def stat(title, exprs, w=6, h=4, unit="none", thr=None, mappings=None, color_mode="background", graph=False,
         decimals=None, desc="", text="value_and_name", no_value=None, overrides=None, link=None):
    p = {"type": "stat", "title": title, "description": desc, "datasource": DS, "w": w, "h": h,
         "targets": [target(e, l, instant=not graph, ref=chr(65 + i)) for i, (e, l) in enumerate(exprs)],
         "fieldConfig": {"defaults": {"unit": unit, "thresholds": thr or steps(BLUE), "color": {"mode": "thresholds"},
                                      "mappings": mappings or []}, "overrides": overrides or []},
         "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                     "orientation": "auto", "textMode": text, "colorMode": color_mode,
                     "graphMode": "area" if graph else "none", "justifyMode": "auto", "wideLayout": True,
                     "showPercentChange": False, "text": {"titleSize": 14, "valueSize": 30}}}
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    if no_value:
        p["fieldConfig"]["defaults"]["noValue"] = no_value
    if link:
        p["links"] = [{"title": link[0], "url": link[1]}]
    return p


def series(title, exprs, w=12, h=8, unit="none", stack=False, max_=None, points=False, thr_line=None, desc="",
           overrides=None, legend="list", fill=10):
    custom = {"lineWidth": 2, "fillOpacity": 70 if stack else fill, "showPoints": "always" if points else "never",
              "pointSize": 6, "drawStyle": "points" if points else "line", "spanNulls": False,
              "stacking": {"mode": "normal" if stack else "none", "group": "A"}, "axisSoftMin": 0,
              "gradientMode": "none", "lineInterpolation": "smooth"}
    defaults = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"}, "min": 0}
    if max_ is not None:
        defaults["max"] = max_
    if thr_line is not None:
        defaults["thresholds"] = steps("transparent", thr_line, RED)
        custom["thresholdsStyle"] = {"mode": "line"}
    return {"type": "timeseries", "title": title, "description": desc, "datasource": DS, "w": w, "h": h,
            "targets": [target(e, l, ref=chr(65 + i)) for i, (e, l) in enumerate(exprs)],
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
            "options": {"legend": {"displayMode": legend, "placement": "bottom", "showLegend": legend != "hidden"},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def bars(title, expr, legend, w=12, h=9, unit="bytes", color=BLUE, desc="", max_=None, thr=None):
    """A ranked list, biggest first: a table with the name and an inline bar plus value per row (reads well on a
    phone, where bar gauges squeeze their bars to a hairline). `legend` names the label column(s) to show."""
    labels = re.findall(r"\{\{(\w+)\}\}", legend)
    cell = {"type": "gauge", "mode": "basic", "valueDisplayMode": "text"}
    value_props = [{"id": "unit", "value": unit}, {"id": "custom.cellOptions", "value": cell},
                   {"id": "color", "value": {"mode": "fixed", "fixedColor": color}}, {"id": "min", "value": 0},
                   {"id": "custom.width", "value": 170}]
    if max_ is not None:
        value_props.append({"id": "max", "value": max_})
    rename = {l: l.replace("pod_app", "App").replace("program", "Program").replace("pod", "Pod")
                    .replace("command", "Program").replace("name", "Container") for l in labels}
    keep = set(labels) | {"Value"}
    return {"type": "table", "title": title, "description": desc, "datasource": DS, "w": w, "h": h,
            "targets": [target(expr, legend, instant=True, fmt="table")],
            "transformations": [{"id": "filterFieldsByName", "options": {"include": {"names": sorted(keep)}}},
                                {"id": "organize", "options": {"renameByName": rename,
                                                               "indexByName": {**{l: i for i, l in enumerate(labels)},
                                                                               "Value": len(labels)}}},
                                {"id": "sortBy", "options": {"sort": [{"field": "Value", "desc": True}]}}],
            "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False},
                                         "noValue": "Nothing"},
                            "overrides": [{"matcher": {"id": "byName", "options": "Value"}, "properties": value_props}]},
            "options": {"showHeader": False, "cellHeight": "sm", "footer": {"show": False}}}


def table(title, targets, transformations, overrides, w=24, h=9, sort=None, desc="", no_value=None):
    p = {"type": "table", "title": title, "description": desc, "datasource": DS, "w": w, "h": h, "targets": targets,
         "transformations": transformations,
         "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False}},
                         "overrides": overrides},
         "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}, "sortBy": sort or []}}
    if no_value:
        p["fieldConfig"]["defaults"]["noValue"] = no_value
    return p


def text(content, w=24, h=3, title=""):
    return {"type": "text", "title": title, "w": w, "h": h,
            "options": {"mode": "markdown", "content": content, "code": {"showLineNumbers": False}}}


def row(title, collapsed=False, panels=None):
    return {"type": "row", "title": title, "collapsed": collapsed, "panels": panels or [], "w": 24, "h": 1}


def field(name, props):
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


def place(items):
    """gridPos in reading order: left to right, wrapping at 24. A collapsed row carries its panels inside it and
    takes no height; an open row is followed by its panels at the top level."""
    out, ids = [], iter(range(1, 1000))

    def layout(panels, y):
        x = line = 0
        for p in panels:
            if p.pop("smalltext", False):
                p["options"]["text"] = {"titleSize": 14, "valueSize": 18}
            w, h = p.pop("w"), p.pop("h")
            if x + w > 24:
                x, y, line = 0, y + line, 0
            p["gridPos"], p["id"] = {"x": x, "y": y, "w": w, "h": h}, next(ids)
            x, line = x + w, max(line, h)
        return y + line

    y, loose = 0, []
    for p in items + [None]:
        if p is not None and p["type"] != "row":
            loose.append(p)
            continue
        y = layout(loose, y)
        out += loose
        loose = []
        if p is None:
            break
        inner = p["panels"]
        p.pop("w"), p.pop("h")
        p["gridPos"], p["id"] = {"x": 0, "y": y, "w": 24, "h": 1}, next(ids)
        y += 1
        if p["collapsed"]:
            layout(inner, y)          # positions for when it opens; takes no room while closed
            out.append(p)
        else:
            p["panels"] = []
            out.append(p)
            y = layout(inner, y)
            out += inner
    return out


def dashboard(uid, title, panels, time="now-24h", refresh="1m", variables=None, desc=""):
    return {"uid": uid, "title": title, "description": desc, "tags": ["homelab"], "timezone": "browser",
            "editable": True, "graphTooltip": 1, "schemaVersion": 39, "version": 1, "refresh": refresh,
            "time": {"from": time, "to": "now"}, "fiscalYearStartMonth": 0, "liveNow": False,
            "links": [{"title": "Homelab", "type": "dashboards", "tags": ["homelab"], "asDropdown": False,
                       "includeVars": False, "keepTime": False, "targetBlank": False}],
            "templating": {"list": variables or []}, "annotations": {"list": []}, "panels": place(panels)}


def machine_var():
    return {"name": "machine", "label": "Machine", "type": "custom", "query": "acer-laptop,moudimash99",
            "current": {"text": "acer-laptop", "value": "acer-laptop"}, "options": [], "multi": False,
            "includeAll": False, "hide": 0}


# ---------------------------------------------------------------------------------------------------------------
# home: is anything wrong?

UP = steps(RED, 1, GREEN)
CPU = '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])))'
MEM = '100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)'
DISK = ('100 * max by (instance) (1 - node_filesystem_avail_bytes{fstype=~"ext4|xfs|btrfs"} '
        '/ node_filesystem_size_bytes{fstype=~"ext4|xfs|btrfs"})')
TEMP = 'max by (instance) (node_hwmon_temp_celsius)'
FAILED_24H = ('count((max by (job_name) (kube_job_status_failed{namespace="apps"}) > 0) '
              'and on (job_name) (time() - max by (job_name) (kube_job_status_start_time{namespace="apps"}) < 86400)) or vector(0)')
# A Job's pod that was killed stays "terminated"; a restarted container keeps it as "last terminated".
OOM = ('(max by (namespace, pod, container) (max_over_time(kube_pod_container_status_terminated_reason{{reason="OOMKilled"}}[{w}]))'
       ' or max by (namespace, pod, container) (max_over_time(kube_pod_container_status_last_terminated_reason{{reason="OOMKilled"}}[{w}]))) > 0')
OOM_24H = f'count({OOM.format(w="24h")}) or vector(0)'
GPU_POD = ('sum by (pod) (gpu_process_sm_percent * on (pod_uid) group_left (pod) '
           'label_replace(kube_pod_info, "pod_uid", "$1", "uid", "(.+)"))')
GPU_POD_MEM = ('sum by (pod) (gpu_process_memory_bytes * on (pod_uid) group_left (pod) '
               'label_replace(kube_pod_info, "pod_uid", "$1", "uid", "(.+)"))')

home = dashboard("home-overview", "Homelab", desc="Is anything wrong? Green means no.", panels=[
    stat("Alerts firing", [('count(ALERTS{alertstate="firing"}) or vector(0)', "")], w=6, h=3,
         thr=steps(GREEN, 1, RED), text="value", mappings=[{"type": "value", "options": {"0": {"text": "None"}}}],
         desc="Prometheus alerts firing now (the same ones that email you). Listed below."),
    stat("Machines up", [('sum(up{job="node-exporter"})', "")], w=6, h=3, text="value",
         thr=steps(RED, 2, GREEN),
         mappings=[{"type": "value", "options": {"2": {"text": "Both"}, "1": {"text": "One down"}, "0": {"text": "None"}}}]),
    stat("Failed job runs, 24 h", [(FAILED_24H, "")], w=6, h=3, text="value", thr=steps(GREEN, 1, RED),
         mappings=[{"type": "value", "options": {"0": {"text": "None"}}}],
         link=("Scheduled jobs", "/d/jobs")),
    stat("Out-of-memory kills, 24 h", [(OOM_24H, "")], w=6, h=3, text="value", thr=steps(GREEN, 1, ORANGE),
         mappings=[{"type": "value", "options": {"0": {"text": "None"}}}], link=("What uses memory", "/d/usage")),
    table("What's firing", [target('ALERTS{alertstate="firing"}', instant=True, fmt="table")],
          [{"id": "organize", "options": {"indexByName": {"alertname": 0, "severity": 1},
                                          "renameByName": {"alertname": "Alert", "severity": "Severity"},
                                          "excludeByName": {"Time": True, "Value": True, "__name__": True,
                                                            "alertstate": True, "cluster": True, "job": True,
                                                            "instance": True, "endpoint": True, "service": True,
                                                            "uid": True, "container": True, "namespace": False}}}],
          [field("Severity", [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                              {"id": "mappings", "value": [{"type": "value", "options": {
                                  "critical": {"color": RED, "index": 0}, "warning": {"color": ORANGE, "index": 1}}}]}])],
          w=24, h=4, no_value="Nothing firing"),
    row("Machines", panels=[
        stat("CPU busy", [(CPU, "{{instance}}")], w=6, h=5, unit="percent", graph=True, color_mode="value",
             thr=steps(GREEN, 80, ORANGE, 95, RED), decimals=0),
        stat("Memory used", [(MEM, "{{instance}}")], w=6, h=5, unit="percent", graph=True, color_mode="value",
             thr=steps(GREEN, 85, ORANGE, 95, RED), decimals=0, link=("What uses memory", "/d/usage")),
        stat("Fullest disk", [(DISK, "{{instance}}")], w=6, h=5, unit="percent", graph=True, color_mode="value",
             thr=steps(GREEN, 85, ORANGE, 95, RED), decimals=0),
        stat("Hottest sensor", [(TEMP, "{{instance}}")], w=6, h=5, unit="celsius", graph=True, color_mode="value",
             thr=steps(GREEN, 80, ORANGE, 90, RED), decimals=0),
    ]),
    row("The Acer's GPU", panels=[
        stat("GPU busy", [("nvidia_smi_utilization_gpu_ratio * 100", "")], w=6, h=4, unit="percent", graph=True,
             color_mode="value", thr=steps(GREEN, 95, ORANGE), decimals=0, text="value"),
        stat("GPU temperature", [("nvidia_smi_temperature_gpu", "")], w=6, h=4, unit="celsius", graph=True,
             color_mode="value", thr=steps(GREEN, 85, ORANGE, 92, RED), decimals=0, text="value"),
        stat("GPU memory", [("nvidia_smi_memory_used_bytes / nvidia_smi_memory_total_bytes * 100", "")], w=6, h=4,
             unit="percent", graph=True, color_mode="value", thr=steps(GREEN, 92, ORANGE), decimals=0, text="value"),
        stat("On the GPU now", [(GPU_POD, "{{pod}}")], w=6, h=4, unit="percent", color_mode="none", decimals=0,
             no_value="Nothing", desc="Each pod using the GPU and how busy it keeps it.", link=("Details", "/d/usage")),
    ]),
    row("Last 24 hours", panels=[
        series("Memory used", [(MEM, "{{instance}}")], w=12, h=8, unit="percent", max_=100,
               overrides=machine_overrides(), thr_line=90),
        series("Hottest sensor", [(TEMP, "{{instance}}")], w=12, h=8, unit="celsius", overrides=machine_overrides(),
               thr_line=90),
    ]),
])

# ---------------------------------------------------------------------------------------------------------------
# usage: what is using the machines?

POD_MEM = (f'sort_desc(topk(12, sum by (pod_app) (label_replace(container_memory_working_set_bytes'
           f'{{container!="", node="$machine"}}, {APP}))))')
POD_CPU = (f'sort_desc(topk(12, sum by (pod_app) (label_replace(rate(container_cpu_usage_seconds_total'
           f'{{container!="", node="$machine"}}[5m]), {APP}))))')
PROC_MEM = ('sort_desc(topk(10, sum by (program) (namedprocess_namegroup_memory_bytes'
            '{memtype="resident", node="$machine"})))')
DOCKER_MEM = 'sort_desc(topk(10, container_memory_working_set_bytes{job="cadvisor-docker", name!=""}))'
GPU_PROC = ('sort_desc(sum by (pod, command) (gpu_process_sm_percent * on (pod_uid) group_left (pod) '
            'label_replace(kube_pod_info, "pod_uid", "$1", "uid", "(.+)")))')

usage = dashboard("usage", "What is using the machines", time="now-6h",
                  desc="Memory, CPU and the GPU, per app and per program.", variables=[machine_var()], panels=[
    stat("Memory used", [(MEM.replace("node_memory_MemAvailable_bytes", 'node_memory_MemAvailable_bytes{instance="$machine"}')
                          .replace("node_memory_MemTotal_bytes", 'node_memory_MemTotal_bytes{instance="$machine"}'), "")],
         w=8, h=4, unit="percent", graph=True, color_mode="value", thr=steps(GREEN, 85, ORANGE, 95, RED), decimals=0,
         text="value"),
    stat("CPU busy", [(CPU.replace('mode="idle"', 'mode="idle", instance="$machine"'), "")], w=8, h=4, unit="percent",
         graph=True, color_mode="value", thr=steps(GREEN, 80, ORANGE, 95, RED), decimals=0, text="value"),
    stat("Memory free", [('node_memory_MemAvailable_bytes{instance="$machine"}', "")], w=8, h=4, unit="bytes",
         color_mode="value", thr=steps(RED, 512 * 2**20, ORANGE, 1024 * 2**20, GREEN), decimals=1, text="value"),
    bars("Memory by app", POD_MEM, "{{pod_app}}", w=12, h=11,
         desc="Pods on this machine, grouped by app or job (working set: what can't be freed)."),
    bars("CPU by app", POD_CPU, "{{pod_app}}", w=12, h=11, unit="percentunit", color="#eb6834",
         desc="Share of one CPU core in use, averaged over 5 minutes (100% = one whole core; the server has 8 "
              "threads, the Acer 4)."),
    bars("On the GPU (Acer)", GPU_PROC, "{{pod}}: {{command}}", w=12, h=6, unit="percent", color="#1baf7a", max_=100,
         desc="Each GPU program, its pod and how much of the GPU's compute it used in the last second."),
    bars("Programs outside containers", PROC_MEM, "{{program}}", w=12, h=9,
         desc="Memory of programs that aren't in a pod (k3s itself, Docker, the desktop...). Shared memory counts "
              "in each, so these can add up to more than the machine has."),
    row("Over time", panels=[
        series("Memory by app", [(f'topk(8, sum by (pod_app) (label_replace(container_memory_working_set_bytes'
                                  f'{{container!="", node="$machine"}}, {APP})))', "{{pod_app}}")],
               w=12, h=9, unit="bytes", stack=True),
        series("CPU by app", [(f'topk(8, sum by (pod_app) (label_replace(rate(container_cpu_usage_seconds_total'
                               f'{{container!="", node="$machine"}}[5m]), {APP})))', "{{pod_app}}")],
               w=12, h=9, unit="none", stack=True),
        series("GPU by pod", [(GPU_POD, "{{pod}}")], w=12, h=8, unit="percent", max_=100),
        series("GPU memory by pod", [(GPU_POD_MEM, "{{pod}}")], w=12, h=8, unit="bytes"),
    ]),
    row("Docker on the server", collapsed=True, panels=[
        bars("Docker containers' memory (server)", DOCKER_MEM, "{{name}}", w=24, h=8),
    ]),
])

# ---------------------------------------------------------------------------------------------------------------
# jobs: every scheduled run

OWNER = 'max by (job_name, owner_name) (max_over_time(kube_job_owner{namespace="apps", owner_kind="CronJob"}[7d]))'
START = 'max by (job_name) (max_over_time(kube_job_status_start_time{namespace="apps"}[7d])) * 1000'
TOOK = ('max by (job_name) (max_over_time(kube_job_status_completion_time{namespace="apps"}[7d]))'
        ' - max by (job_name) (max_over_time(kube_job_status_start_time{namespace="apps"}[7d]))')
# 2 failed, 1 done, 0 still running (kube-state-metrics only has a failed series for Jobs that have the condition)
RESULT = ('(2 * (max by (job_name) (max_over_time(kube_job_failed{namespace="apps", condition="true"}[7d])) > 0))'
          ' or max by (job_name) (max_over_time(kube_job_complete{namespace="apps", condition="true"}[7d]))')
BY_CRON = ' * on (job_name) group_left (owner_name) max by (job_name, owner_name) (kube_job_owner{namespace="apps", owner_kind="CronJob"})'

jobs = dashboard("jobs", "Scheduled jobs", time="now-7d", refresh="5m",
                 desc="Every scheduled job's runs over the last week: when, how long, how it ended, memory.", panels=[
    stat("Since the last good run", [('time() - kube_cronjob_status_last_successful_time{namespace="apps"}', "{{cronjob}}")],
         w=24, h=6, unit="s", color_mode="value", thr=steps(GREEN, 26 * 3600, ORANGE, 48 * 3600, RED), decimals=0,
         desc="Per scheduled job. Daily jobs turn orange after 26 h without a good run."),
    dict(stat("Paused", [('kube_cronjob_spec_suspend{namespace="apps"} == 1', "{{cronjob}}")], w=12, h=3,
              color_mode="none", text="name", no_value="Nothing paused",
              desc="Suspended CronJobs: they don't run until resumed."), smalltext=True),
    dict(stat("Running now", [('max by (owner_name) ((kube_job_status_active{namespace="apps"} > 0)' + BY_CRON + ')',
                               "{{owner_name}}")], w=12, h=3, color_mode="none", text="name", no_value="Nothing running"),
         smalltext=True),
    table("Runs, last 7 days",
          [target(OWNER, instant=True, fmt="table", ref="A"), target(START, instant=True, fmt="table", ref="B"),
           target(TOOK, instant=True, fmt="table", ref="C"), target(RESULT, instant=True, fmt="table", ref="D")],
          [{"id": "joinByField", "options": {"byField": "job_name", "mode": "outer"}},
           {"id": "organize", "options": {
               "excludeByName": {"Time": True, "Time 1": True, "Time 2": True, "Time 3": True, "Time 4": True,
                                 "Value #A": True, "job_name": False},
               "indexByName": {"owner_name": 0, "Value #B": 1, "Value #C": 2, "Value #D": 3, "job_name": 4},
               "renameByName": {"owner_name": "Job", "Value #B": "Started", "Value #C": "Took",
                                "Value #D": "Result", "job_name": "Run"}}}],
          [field("Started", [{"id": "unit", "value": "dateTimeFromNow"}]),
           field("Took", [{"id": "unit", "value": "dtdurations"}, {"id": "noValue", "value": "-"}]),
           field("Result", [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                            {"id": "mappings", "value": [
                                {"type": "value", "options": {"0": {"text": "running", "color": BLUE},
                                                              "1": {"text": "OK", "color": GREEN}}},
                                {"type": "range", "options": {"from": 2, "to": 3, "result": {"text": "Failed", "color": RED}}},
                                {"type": "special", "options": {"match": "null", "result": {"text": "running", "color": BLUE}}}]}]),
           field("Run", [{"id": "custom.hidden", "value": True}]),
           field("Started", [{"id": "custom.width", "value": 104}]), field("Took", [{"id": "custom.width", "value": 74}]),
           field("Result", [{"id": "custom.width", "value": 64}])],
          w=24, h=12, sort=[{"displayName": "Started", "desc": True}],
          desc="Kubernetes keeps only the last few runs; this table reads them back from Prometheus (15 days)."),
    series("How long each run took", [('max by (owner_name) (((kube_job_status_completion_time{namespace="apps"}'
                                        ' - kube_job_status_start_time{namespace="apps"})'
                                        ' and on (job_name) (time() - kube_job_status_completion_time{namespace="apps"} < 1800))'
                                        + BY_CRON + ')', "{{owner_name}}")],
           w=24, h=9, unit="s", points=True, legend="list",
           desc="One mark per finished run, placed just after it ended (scheduled jobs only)."),
    series("Memory against the limit", [
        (f'max by (cronjob) (label_replace(container_memory_working_set_bytes{{namespace="apps", container!="", pod=~".+-[0-9]{{6,}}-[a-z0-9]{{5}}"}}'
         f' / on (namespace, pod, container) kube_pod_container_resource_limits{{resource="memory"}}, {CRON})) * 100',
         "{{cronjob}}")], w=24, h=8, unit="percent", max_=110, thr_line=90,
        desc="Each job's memory as a share of its limit. Past 100% the kernel kills it (OOMKilled)."),
    table("Killed for memory, last 7 days",
          [target(OOM.format(w="7d"), instant=True, fmt="table")],
          [{"id": "organize", "options": {"excludeByName": {"Time": True, "Value": True, "namespace": True},
                                          "renameByName": {"pod": "Pod", "container": "Container"}}}],
          [], w=24, h=5, no_value="None"),
])

# ---------------------------------------------------------------------------------------------------------------
# pc: the Windows PC's jobs (reported by lab/pc-report/report.ps1 every 10 minutes)

pc = dashboard("pc-jobs", "PC jobs", time="now-24h", refresh="5m",
               desc="The Windows PC's scheduled jobs, from its reports (every 10 minutes while it is on).", panels=[
    stat("Last report", [("time() - pc_report_timestamp_seconds", "")], w=8, h=4, unit="s", color_mode="value",
         thr=steps(GREEN, 1200, GREY), decimals=0, text="value", no_value="Never: run lab/pc-report/install.ps1",
         desc="Over 20 minutes: the PC is off or asleep, so the rest is its last known state."),
    stat("Tasks failing", [('count(pc_task_ok == 0) or vector(0)', "")], w=8, h=4, text="value",
         thr=steps(GREEN, 1, RED), mappings=[{"type": "value", "options": {"0": {"text": "None"}}}]),
    stat("Archived today", [('pc_job_value{pc_job="archiver", name="Archived today"}', "")], w=8, h=4, text="value",
         color_mode="value", decimals=0),
    table("Scheduled tasks",
          [target("pc_task_ok", instant=True, fmt="table", ref="A"),
           target("pc_task_last_run_timestamp_seconds * 1000", instant=True, fmt="table", ref="B")],
          [{"id": "joinByField", "options": {"byField": "task", "mode": "outer"}},
           {"id": "organize", "options": {
               "excludeByName": {"Time": True, "Time 1": True, "Time 2": True, "__name__": True, "cluster": True,
                                 "instance": True, "job": True, "namespace": True, "pod": True, "app": True,
                                 "status": True, "instance 1": True, "instance 2": True, "job 1": True, "job 2": True,
                                 "namespace 1": True, "namespace 2": True, "pod 1": True, "pod 2": True,
                                 "cluster 1": True, "cluster 2": True, "__name__ 1": True, "__name__ 2": True},
               "indexByName": {"task": 0, "Value #A": 1, "Value #B": 2, "note": 3},
               "renameByName": {"task": "Task", "Value #A": "State", "Value #B": "Last run", "note": "Note"}}}],
          [field("State", [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                           {"id": "mappings", "value": [{"type": "value", "options": {
                               "1": {"text": "OK", "color": GREEN}, "0": {"text": "Failed", "color": RED}}}]}]),
           field("Last run", [{"id": "unit", "value": "dateTimeFromNow"}])],
          w=24, h=10, no_value="No report yet"),
    stat("Gmail archiver", [('pc_job_value{pc_job="archiver"}', "{{name}}")], w=12, h=6, color_mode="none", decimals=0),
    stat("FreeMotion", [('pc_job_value{pc_job="freemotion"}', "{{name}}")], w=12, h=6, color_mode="none", decimals=0),
    stat("Upwork scan", [('pc_job_ok{pc_job="upwork"}', "")], w=12, h=4, text="value",
         thr=steps(RED, 1, GREEN), mappings=[{"type": "value", "options": {"1": {"text": "Running"},
                                                                          "0": {"text": "Stopped (login page)"}}}]),
    stat("Daily review", [('pc_job_value{pc_job="review"}', "{{name}}")], w=12, h=4, color_mode="none", decimals=0),
    series("Archived emails", [('pc_job_value{pc_job="archiver", name="Archived today"}', "archived today")],
           w=24, h=7, unit="none"),
])

# ---------------------------------------------------------------------------------------------------------------
# scheduling: who gets in on a machine, and who waits for room (made 2026-11-21 from the "Acer Scheduling Night" page)
#
# One row per app: blue while it runs, orange while it waits for room (Pending), red for 10 minutes after it fails.
# Short runs are the hard part: kube-state-metrics is scraped every 30 s and a fetch run takes 20 s. Pod phases alone
# miss such runs, so a Job's run is drawn from its start and completion times instead, read 1 minute ahead
# (offset -1m) so that a run that began and ended between two scrapes is still placed where it was. Each point is the
# worst state seen in its interval at 10 s steps (failed over waiting over running), so nothing shorter than a pixel
# disappears. The last minute of Job runs lags by up to a minute because of the look-ahead.


def named(x):
    """pod -> pod_app: the app or job behind a pod (a pod with no hash suffix, a StatefulSet's, keeps its name)."""
    return f'label_replace(label_replace({x}, "pod_app", "$1", "pod", "(.+)"), {APP})'


NS = 'namespace=~"apps|tools"'
ON_MACHINE = f'and on (namespace, pod) kube_pod_info{{{NS}, node=~"$machine|"}}'   # "" = not placed yet (waiting)
IN_RUNNING = 'and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)'
IN_PENDING = 'and on (namespace, pod) (kube_pod_status_phase{phase="Pending"} == 1)'


def worst(x):
    return f'max_over_time(({x})[$__interval:10s])'


S_RUNNING = 'max by (pod_app) (' + named('(kube_pod_status_phase{phase="Running"} == 1 ' + ON_MACHINE + ')') + ')'
S_WAITING = 'max by (pod_app) (' + named('(kube_pod_status_phase{phase="Pending"} == 1 ' + ON_MACHINE + ')') + ')'
S_FAILED = ('max by (pod_app) (' + named('((kube_pod_status_phase{phase="Failed"} == 1 unless on (namespace, pod) '
                                         '(kube_pod_status_phase{phase="Failed"} offset 10m == 1)) ' + ON_MACHINE + ')')
            + ')')
JOB_ON_MACHINE = ('label_replace((kube_pod_owner{' + NS + ', owner_kind="Job"} offset -1m) and on (namespace, pod) '
                  '(kube_pod_info{node="$machine"} offset -1m), "job_name", "$1", "owner_name", "(.+)")')
S_JOB = ('max by (pod_app) (label_replace(label_replace('
         '((kube_job_status_start_time{' + NS + '} offset -1m) <= time()'
         ' unless on (namespace, job_name) ((kube_job_status_completion_time offset -1m) <= time())'
         ' unless on (namespace, job_name) (kube_job_failed{condition="true"} == 1))'
         ' and on (namespace, job_name) ' + JOB_ON_MACHINE + ','
         ' "pod_app", "$1", "job_name", "(.+)"), "pod_app", "$1", "job_name", "^(.+)-[0-9]{6,}$")) * 0 + 1')
STATE = (f'(3 * {worst(S_FAILED)}) or (2 * {worst(S_WAITING)}) or {worst(S_JOB)} or {worst(S_RUNNING)}')

ALLOC = 'sum(kube_node_status_allocatable{resource="memory", node="$machine"})'
BOOKED = ('sum(kube_pod_container_resource_requests{resource="memory", node="$machine"} ' + IN_RUNNING + ')')
WANTED = 'sum(kube_pod_container_resource_requests{resource="memory"} ' + IN_PENDING + ') or vector(0)'
USED = 'sum(container_memory_working_set_bytes{node="$machine", container!=""})'
STATES = [{"type": "value", "options": {"1": {"text": "running", "color": BLUE, "index": 0},
                                        "2": {"text": "waiting for room", "color": ORANGE, "index": 1},
                                        "3": {"text": "failed", "color": RED, "index": 2}}}]


def timeline(title, expr, legend, w=24, h=14, desc=""):
    # Colour "fixed", not "thresholds": with thresholds a state timeline names its bars by range ("2+", "-∞+") and
    # ignores the value mappings. Fixed lets the mappings give each state its name and colour.
    return {"type": "state-timeline", "title": title, "description": desc, "datasource": DS, "w": w, "h": h,
            "interval": "30s", "maxDataPoints": 1500, "targets": [target(expr, legend)],
            "fieldConfig": {"defaults": {"color": {"mode": "fixed", "fixedColor": GREY}, "thresholds": steps(BLUE),
                                         "mappings": STATES,
                                         "custom": {"lineWidth": 0, "fillOpacity": 90, "spanNulls": False}},
                            "overrides": []},
            "options": {"mergeValues": True, "showValue": "never", "alignValue": "left", "rowHeight": 0.85,
                        "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                        "tooltip": {"mode": "single", "sort": "none"}}}


def event_annotation(name, expr, title, color):
    return {"name": name, "datasource": DS, "enable": True, "iconColor": color, "expr": expr, "step": "1m",
            "titleFormat": title, "textFormat": "", "tagKeys": "", "useValueForTime": False}


scheduling = dashboard("scheduling", "Who gets in", time="now-12h", refresh="1m", variables=[machine_var()],
                       desc="Each app on a machine: when it ran, when it waited for room, when it failed; and the "
                            "memory booked against what is really used.", panels=[
    stat("Running now", [('count(kube_pod_status_phase{phase="Running"} == 1 and on (namespace, pod) '
                          'kube_pod_info{node="$machine"})', "")], w=6, h=3, text="value", color_mode="none",
         desc="Pods running on this machine now, every namespace."),
    stat("Waiting for room now", [('count(kube_pod_status_phase{phase="Pending"} == 1) or vector(0)', "")], w=6, h=3,
         text="value", thr=steps(GREEN, 1, ORANGE), mappings=[{"type": "value", "options": {"0": {"text": "None"}}}],
         desc="Pods that exist but have no machine yet, usually because the memory they book doesn't fit."),
    stat("Booked", [(f'{BOOKED} / {ALLOC} * 100', "")], w=6, h=3, unit="percent", text="value", color_mode="value",
         thr=steps(GREEN, 90, ORANGE, 97, RED), decimals=0,
         desc="Memory the running pods have booked (requests), as a share of what the machine gives pods. A new pod "
              "gets in only if its own booking fits in the rest, whatever is really used."),
    stat("Really used", [(f'{USED} / {ALLOC} * 100', "")], w=6, h=3, unit="percent", text="value",
         color_mode="value", thr=steps(GREEN, 85, ORANGE, 95, RED), decimals=0),
    timeline("Every app: running, waiting, failed", STATE, "{{pod_app}}", h=16,
             desc="Blue: running. Orange: waiting for room (the booking didn't fit). Red: failed (for 10 minutes "
                  "after). Empty: not there. Point at a bar for its exact start and length. Markers on top: purple, "
                  "a pod pushed out to make room for a higher rank (the scheduler) or by a short machine (eviction); "
                  "red, a container killed for memory. Apps in apps and tools; the monitoring pods are left out."),
    series("Memory: booked against really used", [
        (ALLOC, "room for pods"), (BOOKED, "booked by running pods"), (USED, "really used"),
        (WANTED, "wanted by pods waiting")], w=24, h=9, unit="bytes",
        overrides=[field("room for pods", [{"id": "color", "value": {"mode": "fixed", "fixedColor": RED}},
                                           {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [8, 6]}},
                                           {"id": "custom.fillOpacity", "value": 0}]),
                   field("booked by running pods", [{"id": "color", "value": {"mode": "fixed", "fixedColor": ORANGE}}]),
                   field("really used", [{"id": "color", "value": {"mode": "fixed", "fixedColor": BLUE}}]),
                   field("wanted by pods waiting", [{"id": "color", "value": {"mode": "fixed", "fixedColor": GREY}},
                                                    {"id": "custom.drawStyle", "value": "bars"},
                                                    {"id": "custom.fillOpacity", "value": 40}])],
        desc="When the orange line reaches the red one, the next pod waits even if the blue line is far below."),
    bars("Waiting now, and for how long",
         'sort_desc(max by (pod) ((time() - kube_pod_created) ' + IN_PENDING + '))', "{{pod}}", w=12, h=7,
         unit="s", color=ORANGE),
    table("On this machine now: rank, booked, used",
          [target('max by (pod, priority_class) (kube_pod_info{node="$machine"} ' + IN_RUNNING + ')', instant=True,
                  fmt="table", ref="A"),
           target('sum by (pod) (kube_pod_container_resource_requests{resource="memory", node="$machine"} '
                  + IN_RUNNING + ')', instant=True, fmt="table", ref="B"),
           target('sum by (pod) (kube_pod_container_resource_limits{resource="memory", node="$machine"} '
                  + IN_RUNNING + ')', instant=True, fmt="table", ref="C"),
           target('sum by (pod) (container_memory_working_set_bytes{node="$machine", container!=""})',
                  instant=True, fmt="table", ref="D")],
          [{"id": "joinByField", "options": {"byField": "pod", "mode": "inner"}},
           {"id": "organize", "options": {
               "excludeByName": {"Time": True, "Time 1": True, "Time 2": True, "Time 3": True, "Time 4": True,
                                 "Value #A": True},
               "indexByName": {"pod": 0, "priority_class": 1, "Value #B": 2, "Value #D": 3, "Value #C": 4},
               "renameByName": {"pod": "Pod", "priority_class": "Rank", "Value #B": "Booked", "Value #D": "Used",
                                "Value #C": "Limit"}}}],
          [field(n, [{"id": "unit", "value": "bytes"}, {"id": "decimals", "value": 0}, {"id": "custom.width", "value": 84}])
           for n in ("Booked", "Used", "Limit")],
          w=12, h=7, sort=[{"displayName": "Booked", "desc": True}], no_value="Nothing running",
          desc="Rank decides who gives way: a waiting pod of a higher rank pushes out lower ones (see "
               "k8s/cluster/priority.yaml)."),
])
scheduling["annotations"]["list"] = [
    event_annotation("Pushed out", '(kube_pod_status_reason{reason=~"PreemptionByScheduler|Evicted"} == 1 unless '
                     'kube_pod_status_reason{reason=~"PreemptionByScheduler|Evicted"} offset 1m == 1) '
                     'and on (namespace, pod) (kube_pod_info{node="$machine"} offset 2m)',
                     "{{pod}} pushed out ({{reason}})", "purple"),
    event_annotation("Killed for memory", '(kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} == 1 '
                     'unless kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} offset 1m == 1) '
                     'and on (namespace, pod) kube_pod_info{node="$machine"}',
                     "{{pod}} / {{container}} killed for memory", "red"),
]

# ---------------------------------------------------------------------------------------------------------------
# ranks: every app's rank, what that rank may do, what the app is, its booking and its real memory over 24 hours
# (made 2026-11-24, user: "this is the rank that we give it, this is the memory that it books, and this is its actual
# memory usage over the last 24 hours in a graph").
#
# The ranks and their numbers come from k8s/cluster/priority.yaml (kube-state-metrics has no PriorityClass metrics),
# the k3s ones are fixed. "What it is" comes from APP_NOTES below, else from services.yaml: the first sentence of
# the `what` of the entry whose k8s probe names the app. One row per app and machine: a DaemonSet has one per machine.

import yaml  # noqa: E402  (only this section needs it)

INFRA = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
SYSTEM_RANKS = [("system-node-critical", 2000001000, True, "k3s's own plumbing."),
                ("system-cluster-critical", 2000000000, True, "Cluster essentials: names, the web front door, GitOps.")]


def load_ranks():
    with open(os.path.join(INFRA, "k8s", "cluster", "priority.yaml"), encoding="utf-8") as f:
        docs = [d for d in yaml.safe_load_all(f) if d and d.get("kind") == "PriorityClass"]
    ours = [(d["metadata"]["name"], int(d["value"]), d.get("preemptionPolicy", "PreemptLowerPriority") != "Never",
             d.get("description", "")) for d in docs]
    return sorted(SYSTEM_RANKS + ours + [("(none)", 0, False, "Pods made before the default rank existed; they get "
                                          "homelab-normal at their next restart.")], key=lambda r: -r[1])


RANKS = load_ranks()

# Short words for apps whose services.yaml entry covers several pods, or that have no entry (k3s, monitoring agents).
APP_NOTES = {
    "local-path-provisioner": "k3s: makes the disk folders pods keep data in",
    "metrics-server": "k3s: live CPU and memory for kubectl top",
    "svclb-traefik": "k3s: passes ports 80/443 on each machine to Traefik",
    "coredns": "k3s: name lookups inside the cluster",
    "traefik": "Web front door: sends each site's traffic to its pod",
    "source-controller": "Flux: pulls the Infra repo from GitHub",
    "kustomize-controller": "Flux: applies k8s/ from main every 10 min",
    "frigate": "Cat camera recorder: records and detects",
    "lab": "Taste Match, Deals and Cat cam sites, and the login bridge",
    "homepage": "The lab.machaka.net menu",
    "prometheus": "Collects every metric every 30 s, keeps 15 days",
    "grafana": "These dashboards",
    "alertmanager": "Emails Prometheus's alerts",
    "kube-state-metrics": "Numbers about pods, jobs and restarts for Prometheus",
    "beszel": "Simple per-machine monitor (beszel.machaka.net)",
    "beszel-agent": "Beszel's collector on the server",
    "beszel-agent-worker": "Beszel's collector on the Acer",
    "node-exporter": "Machine CPU, memory, disks and temperatures",
    "process-exporter": "Memory per program outside containers",
    "nvidia-gpu-exporter": "The GTX 1050's load, memory and heat",
    "gpu-procs": "Which program is on the GPU",
    "fm-controller": "Free Motion's brain: daemon, scan, score loops, hub agent",
    "fm-browser-acer-0": "Free Motion browser, lane 1",
    "fm-browser-acer-1": "Free Motion browser, lane 2",
    "fm-webllm": "Free Motion's ChatGPT/Gemini website writer, started on demand",
    "fm-webllm-waker": "Starts and stops fm-webllm",
    "freemotion-hub": "Free Motion's dashboard",
    "llm-gateway": "fm-llm: one door to the AI models for every project",
    "mail-classifier": "Trained model that decides what the Gmail archiver archives",
    "mail-classifier-waker": "Starts and stops the mail classifier",
    "email-remove": "Gmail archiver, every 3 minutes",
    "bg-taste-match-gpu": "Whisper captions on the GPU, hourly, with a heat guard",
    "bg-taste-match-llm": "Rates the reviewers with AI, hourly at night",
    "bg-taste-match-fetch": "Downloads the reviewers' videos' audio, every 2 hours",
    "bg-taste-match-shell": "Idle helper for running bg-taste-match stages by hand",
    "backup-acer": "Nightly backup of the Acer",
    "backup-server": "Nightly backup of the server",
    "unmatched-restart": "Restarts the Unmatched site on Mondays",
    "cat-flash": "The cat's flash-of-the-day video, 00:20 and 19:20",
    "cat-monitor": "Nightly GPU cat detection",
    "boardgamefinder": "Nightly board game deals",
    "boardgamefinder-fetch": "Loads Okkazeo pages through the day",
    "cat-weight": "Cat weight site",
    "unmatched": "Unmatched matcher site",
    "boardxplorer": "Board game explorer site",
    "website": "Your personal website",
    "spirit-island": "Spirit Island site",
    "registry": "Local image store (not used yet)",
    "portainer": "Web page for Docker and Kubernetes",
    "uptime-kuma": "Up/down checks of the sites",
}


def app_notes():
    """APP_NOTES, plus the first sentence of each services.yaml `what` for the apps its k8s probes name."""
    notes = {}
    with open(os.path.join(INFRA, "services.yaml"), encoding="utf-8") as f:
        catalog = yaml.safe_load(f)
    for e in catalog if isinstance(catalog, list) else catalog.get("services", []):
        what = re.split(r"(?<=[.!?])\s", str(e.get("what", "")).strip())[0].rstrip(".")
        for p in e.get("probes") or []:
            if isinstance(p, dict) and "k8s" in p and what:
                notes.setdefault(p["k8s"].split("/")[-1], what)
    notes.update(APP_NOTES)
    return notes


NOTES = app_notes()
MACHINE_NAMES = {"moudimash99": "Server", "acer-laptop": "Acer"}


def keyed(x):
    """pod -> pod_app (as `named`), plus `key` = app and machine, the row of the table."""
    return f'label_join({named(x)}, "key", " on ", "pod_app", "node")'


def mem_q(node):
    return f'sum by (pod, node) (container_memory_working_set_bytes{{container!="", node="{node}"}})'


def info_q(node):
    return f'max by (pod, node, priority_class) (max_over_time(kube_pod_info{{node="{node}"}}[24h]))'


RANK_VEC = " or ".join(f'label_replace(vector({v}), "priority_class", "{n}", "", "")'
                       for n, v, _, _ in RANKS if n != "(none)")


def copy_label(x, to, frm):
    return f'label_replace({x}, "{to}", "$1", "{frm}", "(.*)")'


def text_map(field_name, mapping, colors=None):
    """Value mappings that turn a label's value into words (and an optional colour)."""
    opts = {k: {"text": v, "index": i, **({"color": colors[k]} if colors and k in colors else {})}
            for i, (k, v) in enumerate(mapping.items())}
    return field(field_name, [{"id": "mappings", "value": [{"type": "value", "options": opts}]}])


def ladder_md():
    lines = ["| Rank | Number | Can push out lower ranks to start? | Can be pushed out by | What it is for |",
             "|---|---:|---|---|---|"]
    for i, (n, v, pushes, desc) in enumerate(RANKS):
        above = [m for m, w, p, _ in RANKS[:i] if p and w > v and not m.startswith("system")]
        by = "nothing" if n.startswith("system") else (", ".join(f"`{m}`" for m in above) or "nothing")
        if not n.startswith("system") and any(p for m, w, p, _ in RANKS[:i] if m.startswith("system")):
            by = (by + ", k3s's own") if by != "nothing" else "k3s's own"
        lines.append(f"| `{n}` | {v:,} | {'yes' if pushes else 'no, waits for room'} | {by} | {desc} |")
    return "\n".join(lines) + (
        "\n\n**Two separate rules.** *Getting in:* a pod starts only where its **booking** fits; a rank marked yes "
        "may stop pods of a strictly lower number on that machine to make room, the others wait. *Running out of "
        "real memory:* ranks don't count; the kernel kills the biggest containers first (a small booking makes it "
        "likelier), and a container above its own limit is killed whatever its rank.")


RANK_TABLE_COLS = {"pod_app": "App", "priority_class": "Rank", "Value #G": "Rank no.",
                   "pushes": "Pushes out lower?", "what": "What it is", "Value #C": "Booked", "Value #F": "Limit",
                   "Value #D": "Used now", "Value #E": "Peak 24 h", "Trend #A": "Memory, last 24 h"}


def rank_table(node):
    mem, info = mem_q(node), info_q(node)
    req = ('sum by (pod, node) (max_over_time(kube_pod_container_resource_requests'
           '{resource="memory", node="' + node + '"}[24h]))')
    lim = ('sum by (pod, node) (max_over_time(kube_pod_container_resource_limits'
           '{resource="memory", node="' + node + '"}[24h]))')
    return table(f"{MACHINE_NAMES[node]}: every app that ran in the last 24 hours, highest rank first",
          [target('max by (key) (' + keyed(mem) + ')', ref="A"),
           target('max by (key, pod_app, priority_class, what, pushes) (' +
                  copy_label(copy_label(keyed(info), "what", "pod_app"), "pushes", "priority_class") + ')',
                  instant=True, fmt="table", ref="B"),
           target('max by (key) (' + keyed(req) + ')', instant=True, fmt="table", ref="C"),
           target('max by (key) (' + keyed(mem) + ')', instant=True, fmt="table", ref="D"),
           target('max by (key) (max_over_time((max by (key) (' + keyed(mem) + '))[24h:1m]))',
                  instant=True, fmt="table", ref="E"),
           target('max by (key) (' + keyed(lim) + ')', instant=True, fmt="table", ref="F"),
           target('max by (key) ((' + keyed(info) + ' * 0) + on (priority_class) group_left () (' + RANK_VEC + '))'
                  ' or max by (key) (' + keyed(info) + ' * 0)', instant=True, fmt="table", ref="G")],
          [{"id": "timeSeriesTable", "options": {}},
           {"id": "joinByField", "options": {"byField": "key", "mode": "outer"}},
           {"id": "organize", "options": {
               "excludeByName": {k: True for k in ("key", "node", "Time", "Time 1", "Time 2", "Time 3", "Time 4",
                                                   "Time 5", "Time 6", "Value #B")},
               "indexByName": {k: i for i, k in enumerate(RANK_TABLE_COLS)},
               "renameByName": RANK_TABLE_COLS}}],
          [text_map("What it is", NOTES),
           field("What it is", [{"id": "custom.width", "value": 330}]),
           text_map("Pushes out lower?", {n: ("yes" if p else "no, waits") for n, _, p, _ in RANKS if n != "(none)"},
                    {n: (GREEN if p else GREY) for n, _, p, _ in RANKS if n != "(none)"}),
           field("Pushes out lower?", [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                       {"id": "custom.width", "value": 120}]),
           field("Rank", [{"id": "mappings", "value": [{"type": "special", "options": {
               "match": "empty", "result": {"text": "(none)", "index": 0}}}]}, {"id": "custom.width", "value": 170}]),
           field("Rank no.", [{"id": "custom.width", "value": 100}, {"id": "decimals", "value": 0}]),
           field("Memory, last 24 h", [{"id": "custom.cellOptions",
                                        "value": {"type": "sparkline", "hideValue": True,
                                                  "lineWidth": 1, "fillOpacity": 20}},
                                       {"id": "color", "value": {"mode": "fixed",
                                                                 "fixedColor": MACHINE_COLORS[node]}},
                                       {"id": "unit", "value": "bytes"}, {"id": "custom.width", "value": 220}])]
          + [field(n, [{"id": "unit", "value": "bytes"}, {"id": "decimals", "value": 0},
                       {"id": "custom.width", "value": 84}])
             for n in ("Booked", "Limit", "Used now", "Peak 24 h")],
          w=24, h=20 if node == "acer-laptop" else 18, sort=[{"displayName": "Rank no.", "desc": True}],
          no_value="-",
          desc="Booked: the memory the scheduler counts (the request). Limit: above it the container is killed. Used "
               "now and Peak 24 h: real memory (working set). A job that ran in the last 24 h shows its last run's "
               "booking; its graph has a bump per run. Names come from the pods: a Deployment's or job's pod is shown "
               "by its app, a StatefulSet's by its own name.")


def machine_section(node):
    alloc = 'sum(kube_node_status_allocatable{resource="memory", node="' + node + '"})'
    booked = ('sum(kube_pod_container_resource_requests{resource="memory", node="' + node + '"} '
              'and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1))')
    used = 'sum(container_memory_working_set_bytes{node="' + node + '", container!=""})'
    return row(f"{MACHINE_NAMES[node]} ({node})", panels=[
        stat("Room for pods", [(alloc, "")], w=6, h=3, unit="bytes", text="value", color_mode="none", decimals=1,
             desc="What the machine gives pods: its memory minus what is kept for the system and k3s."),
        stat("Booked now", [(f"{booked} / {alloc} * 100", "")], w=6, h=3, unit="percent", text="value",
             color_mode="value", thr=steps(GREEN, 90, ORANGE, 97, RED), decimals=0,
             desc="The running pods' bookings as a share of the room. A new pod gets in only if its booking fits."),
        stat("Really used now", [(f"{used} / {alloc} * 100", "")], w=6, h=3, unit="percent", text="value",
             color_mode="value", thr=steps(GREEN, 85, ORANGE, 95, RED), decimals=0),
        stat("Really used, peak 24 h", [(f"max_over_time(({used})[24h:1m]) / {alloc} * 100", "")], w=6, h=3,
             unit="percent", text="value", color_mode="value", thr=steps(GREEN, 85, ORANGE, 95, RED), decimals=0),
        rank_table(node),
        series(f"{MACHINE_NAMES[node]}: real memory per app, last 24 hours", [
            ('sort_desc(max by (pod_app) (' + keyed(mem_q(node)) + '))', "{{pod_app}}"),
            (alloc, "room for pods")], w=24, h=11, unit="bytes", legend="table", stack=True,
            overrides=[field("room for pods", [{"id": "color", "value": {"mode": "fixed", "fixedColor": RED}},
                                               {"id": "custom.stacking", "value": {"mode": "none", "group": "B"}},
                                               {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [8, 6]}},
                                               {"id": "custom.fillOpacity", "value": 0}])],
            desc="The apps stacked, so the top is the machine's total; the dashed red line is the room for pods. "
                 "Click a name in the list to see it alone."),
    ])


ranks = dashboard("ranks", "Ranks and memory", time="now-24h", refresh="5m",
                  desc="Each machine: every app's rank, what that rank may do, what the app is, the memory it books "
                       "and its real memory over the last 24 hours.", panels=[
    text(ladder_md(), w=24, h=12, title="The ranks, highest first"),
    machine_section("acer-laptop"),
    machine_section("moudimash99"),
    row("Paused jobs (not running on purpose)", panels=[
        table("Paused jobs",
              [target(copy_label('kube_cronjob_spec_suspend{namespace=~"apps|tools"} == 1', "what", "cronjob"),
                      instant=True, fmt="table")],
              [{"id": "organize", "options": {"excludeByName": {"Time": True, "Value": True, "__name__": True,
                                                                "job": True, "instance": True, "namespace": True},
                                              "indexByName": {"cronjob": 0, "what": 1},
                                              "renameByName": {"cronjob": "Job", "what": "What it is"}}},
               {"id": "filterFieldsByName", "options": {"include": {"names": ["Job", "What it is"]}}}],
              [text_map("What it is", NOTES)], w=24, h=5, no_value="None")]),
])

if __name__ == "__main__":
    for name, d in (("home", home), ("usage", usage), ("jobs", jobs), ("pc", pc), ("scheduling", scheduling),
                    ("ranks", ranks)):
        with open(os.path.join(HERE, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        print(f"{name}.json: {len(d['panels'])} panels")
