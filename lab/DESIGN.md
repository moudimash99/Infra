# Lab: the homelab's own status site (lab.machaka.net)

> **2026-11-10, after the owner's first look:** the single site was too much for a first view. Now
> lab.machaka.net is Homepage again (simple tiles, with a Pipelines group fed by `/api/summary/*`), and each job
> with a real use case has its own address: tastematch., deals. and catcam.machaka.net. GPU, machines, jobs and
> the PC's jobs moved to Grafana (`k8s/monitoring/dashboards/make.py`). Each page checks the Cloudflare Access token
> itself and fails closed. Until the three names are in the Access application, the same pages also answer at
> lab.machaka.net/tastematch/, /deals/ and /catcam/ (that name already has the login; Traefik sends only these paths
> to the lab). The rest of this document is the first design, kept for its data notes.

One private web app that answers, for each thing the homelab runs, the question its owner actually has:
"is it working, what did it find, and what needs me?". Phone first: most visits are from a phone.

It replaces Homepage on lab.machaka.net (same Cloudflare Access protection, same hostname). Homepage keeps running
untouched; going back is switching the Ingress.

## Who reads it and for what

One person, the owner, who runs these jobs and fixes them. Typical visits:

- "Anything wrong?" (10 seconds, phone): top of the front page.
- "Where is bg-taste-match at, and when will this round be done?"
- "Did the cats eat today? Show me."
- "Any good board game deals today? What's new?"
- "Which job is using the GPU / why is the Acer busy?"
- "Did my PC jobs (Gmail archiver, daily review, Upwork scan, FreeMotion) run?"

## Pages

### `/` front page
1. **Needs you** strip, only when something is wrong: firing alerts (Alertmanager), failed or late jobs, stalled
   sources (YouTube blocking downloads, BGF scrape aborted, a PC job not reporting). One line each: what, since when,
   link to the page that explains it. When nothing is wrong: one green line "All good, checked HH:MM".
2. **Jobs** group, one card per job: name, state chip (running / ok / failed / paused / late), "last run 2 h ago,
   took 58 min", "next 07:00", and 2 to 3 numbers from its last run that mean something:
   - BG Taste Match: "downloading Chairman of the Board 7/41", round progress bar, audio left in hours, ETA.
   - BoardGameFinder: new deals today, live listings, steps failing (LLM steps until fm-llm exists).
   - Cat monitor: cat visits at the feeder today (Frigate), last monitor run, "feeding measurement off: no tag".
   - Mail classifier: emails classified today by decision.
   - PC jobs (reported from the PC): Gmail archiver archived today, daily review sent, Upwork new jobs / stopped.
3. **Sites** group: machaka.net, cat weight (latest weigh-in per cat), Unmatched, BoardXplorer, FreeMotion hub:
   up/down plus one number where there is one.
4. **Machines** group: server and Acer: CPU, memory, disk, hottest temperature; the Acer's GPU: use, memory,
   temperature and **who is using it** (running pods with the nvidia runtime, minus exporters).
5. **Tools** row: links to Grafana, Uptime Kuma, Beszel, Portainer, Frigate (LAN), Homepage (port-forward note).

### `/bg-taste-match`
- **Now**: running jobs (fetch / gpu / llm) with the latest progress line from each pod's log ("[7/41] ok ..."),
  whether the GPU schedule is paused, whether YouTube is blocking (last fetch exit code 3 / 403 lines).
- **Round progress**: per channel, one row (phone: one card) with a bar per stage over the channel's videos in scope
  (category in FETCH_CATEGORIES): audio (file in audio dir), transcribed (captions/<id>.json3), speakers
  (diar/<id>.json), rated (any row in ratings). Plus audio hours left and the ETA from the last 24 h rate.
- **Progress over time**: daily snapshot of the stage totals (stored by the lab), as a line per stage.
- **Recent activity**: last 30 events across stages (downloads from the fetch log, Whisper lines, ratings by
  rated_at) with titles.
- **Problems**: fetch_errors (latest 20), whisper FAILED lines, game names without a BGG match (games.bgg_id NULL),
  ratings waiting for fm-llm.
- **Results**: the latest results.html (served as is under `/bg-taste-match/results`) and the census page.
- **Video page** `/bg-taste-match/v/<id>`: thumbnail (YouTube's), title, channel, length, stage flags, ratings with
  host, score (shown out of 5), confidence and quote; transcript first lines.
- **Search**: by title or game across videos.

### `/cats`
- Banner while the ArUco tag is missing: feeding time can't be measured; visits below come from Frigate.
- **Today at the feeder**: Frigate cat events in feeder_zone today, newest first, each with its snapshot
  (proxied through the lab, Frigate is LAN only), time, length, and a link to the clip (proxied).
- **Visits per day** (last 14 days, Frigate keeps 14) and **time of day** (hour histogram, last 14 days).
- **Monitor runs**: from rolling_runs/feeding_history_summary.json: per run, window, presence and feeding minutes
  per cat (Simba, Nala).
- **Weight**: latest weigh-in per cat and the trend (cat-weight's SQLite, table weights).

### `/deals` (BoardGameFinder)
- **Last run**: each step's state (data/pipeline_state.json consecutive_failures, data/Output/log.txt tail), and a
  warning when price_finder aborted (then every Post is Is_Online=0 and "no live listings" is false).
- **Today's deals**: data/Output/deals.csv as cards: box image (BGG_Games.Image_URL), name, price, market value
  (Baseline), Edge, condition, language, ships, link to the listing.
- **New since yesterday**: Posts with First_Seen today; **price drops**: Price_History today vs previous price.
- **Market**: live listings, tracked games, listings seen per day (Price_History rows per Date, 30 days).
- **Game page** `/deals/game/<bg_id>`: live listings and price history chart.
- The digest HTML (deals.html) linked as is.

### `/gpu`
Use, memory, temperature (Prometheus nvidia_smi_*, 24 h), who is using it now, GPU jobs (schedules, paused or not,
last runs), the GPU lock holder.

### `/pc`
PC-side jobs from the PC's reports: each scheduled task's last run, result and next run, plus job numbers read from
their own files: Gmail archiver (archiver.log), daily review (review.log), Upwork round (upwork-run.log,
upwork-stopped.txt), FreeMotion tasks (scheduled task info, scan-runs.tsv last row, agent-inbox.md open items).
Stale when the last report is older than 20 minutes (PC off or asleep): shown as "PC offline since", not as failures.

## How it gets its data

| Source | How | Notes |
|---|---|---|
| Kubernetes | API with the pod's ServiceAccount (read-only: pods, pods/log, jobs, cronjobs, nodes) | dev: `kubectl proxy` |
| Prometheus | `http://prometheus.monitoring.svc/api/v1/query` | machines, GPU, Frigate fps |
| Alertmanager | `http://alertmanager.tools.svc:9093/api/v2/alerts` | firing alerts |
| bg-taste-match | `/srv/apps/bg-taste-match` read-only: bgtaste.db (opened `mode=ro`), captions/, diar/, results.html; audio dir listing | journal mode is delete, no WAL |
| BoardGameFinder | `/srv/apps/boardgamefinder/data` read-only: Games.db, Output/, pipeline_state.json | never the repo root (secrets) |
| Cat monitor | `/srv/apps/cat-monitor/rolling_runs` read-only | |
| Frigate | `http://192.168.1.93:5000/api/events?camera=living_room&label=cat...`, snapshot.jpg, clip.mp4 | proxied, cached |
| Cat weight | `/srv/apps/cat-weight/cat_data.db` read-only | |
| Mail classifier | its Service: `/metrics`, `/health` | |
| Sites | HTTP GET of each Service | |
| PC | the PC POSTs a JSON report every 10 minutes to the lab's report port (LAN NodePort, not in the Ingress) | a small PowerShell script + one scheduled task; reads files, changes nothing |

Each source is its own module returning `{ok, error, data, read_at}`, cached for its own TTL (30 s to 10 min), so a
broken source shows as one grey card with its error and never breaks the page.

The lab keeps its own small SQLite (`/srv/apps/lab/lab.db`):
- job runs (every finished Job seen: cronjob, start, end, outcome), because Kubernetes keeps only the last 3;
- a daily snapshot of bg-taste-match's stage totals (progress over time, ETA);
- the PC's latest report and a 7-day history.
A background loop polls every 2 minutes.

## Build

- Python 3.13, FastAPI + Jinja2, server-rendered HTML. No front-end framework; a few lines of plain JS for the
  front page's auto refresh (60 s) and chart tooltips. Charts are inline SVG made on the server.
- One stylesheet, phone first (single column; 2 to 3 columns from 900 px), light and dark from the system setting,
  big tap targets, numbers in tabular figures, times in Europe/Paris and relative ("2 h ago").
- Code in Infra/lab/, manifest Infra/k8s/tools/lab.yaml. Image built on the server, imported on the Acer (no
  registry), like the other apps. Runs on the Acer (needs its hostPaths), requests 96Mi, limit 384Mi.
- Two ports: 8080 pages (Ingress lab.machaka.net), 8081 report intake only (NodePort, LAN).
- Tests: each source module has a test against fixture files; one smoke test renders every page with fixtures.
