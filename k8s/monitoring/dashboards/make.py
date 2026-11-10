"""The homelab's own Grafana dashboards, made from short definitions so they stay consistent. Run after a change:

    python k8s/monitoring/dashboards/make.py      (writes home.json, usage.json, jobs.json, pc.json here)

Phone first: Grafana stacks panels in one column below 769 px, in grid order, so each dashboard reads top to bottom
as answers: the one-glance stats first (short stat tiles), then lists (bar gauges and short tables), then charts.
Details are in collapsed rows. Each machine keeps its own colour everywhere (server blue, Acer orange).

  home   "Is anything wrong?"          alerts, machines, the GPU, failed jobs: green or red at a glance
  usage  "What is using the machines?"  memory / CPU / GPU per app and per program, now and over time
  jobs   "Scheduled jobs"               every run of the last week: when, how long, result, memory, OOM kills
  pc     "PC jobs"                       the Windows PC's scheduled jobs, from its reports to the lab
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

if __name__ == "__main__":
    for name, d in (("home", home), ("usage", usage), ("jobs", jobs), ("pc", pc)):
        with open(os.path.join(HERE, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        print(f"{name}.json: {len(d['panels'])} panels")
