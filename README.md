# Infra

Everything that runs at home. "The server" below is the old laptop, 192.168.1.116 (Ubuntu, 8 threads,
7.6 GB): control plane, Traefik, Docker. The Acer laptop, 192.168.1.93, is the worker. The server keeps
a clone of this repo in `~/Infra`; Docker runs from there.

```
internet ─ Cloudflare (DNS, Access login) ─ tunnel ─ cloudflared ─ nginx ─┬─ Docker containers (proxy_net)
                                                                          └─ k3s Traefik :80 ─ pods, by host name
```

- **Docker** (`docker-compose.yml`, `nginx/`): cloudflared, nginx, cAdvisor, node-exporter. The Free
  Motion hub runs in Docker too, from its own folder.
- **Kubernetes** (`k8s/`): k3s, two nodes: `moudimash99` (the server) and `acer-laptop` (worker). The
  desktop (WSL2) joins later.
- `Prometheus/`: the old setup for an EC2 machine, not used.

## Network

The server runs on Wi-Fi (`wlo1`, 192.168.1.116, static on the router). The Ethernet cable (`eno2`,
192.168.1.135) is bad since 2026-10-27 and waits to be replaced. k3s has `--node-ip 192.168.1.116`, so
its database, its API and Traefik's outside address all follow that address. To change it: stop k3s,
change the flag in `k3s.service`, run `k3s server --cluster-reset` with the same flags, start k3s.

Uptime Kuma, Beszel and Portainer still hold `.135` in their own databases. `k8s/cluster/old-address.yaml`
forwards those ports to `.116`, so they keep working without that address on the server.

## The Acer worker

`acer-laptop`, Aspire VX5-591G (Ubuntu 24.04, i5-7300HQ, 4 threads, 7.6 GB), joined on 2026-11-02 as a
k3s agent. It runs on Wi-Fi (`wlp2s0`, MAC `3c:95:09:a0:1e:b5`); Ethernet (`enp3s0`, MAC
`fc:45:96:e7:c8:23`) is not plugged in.

- **Disks:** Ubuntu is on the 1 TB hard drive, and `/` uses all of it (914 GB), including Frigate's
  recordings in `/var/lib/frigate/media`. The 128 GB SSD is mounted at `/srv` (`nofail`, so the machine
  boots without it): k3s data in `/srv/k3s`, app data in `/srv/apps/<app>`.
- **k3s:** `/etc/rancher/k3s/config.yaml` sets the data folder, the label `role=worker` and the taint
  `dedicated=worker:NoSchedule`. Only pods with that toleration run there, plus DaemonSets that
  tolerate everything. The join address and token are in `/etc/systemd/system/k3s-agent.service.env`.
- **Firewall (ufw):** SSH from anywhere; from 192.168.1.0/24: 10250/tcp (kubelet), 8472/udp (flannel),
  9110/tcp (node-exporter), 45876/tcp (Beszel), 5000/tcp, 8554/tcp and 8555 tcp+udp (Frigate); everything
  from 10.42.0.0/16 and 10.43.0.0/16.
- **Laptop settings:** swap off, sleep masked, lid ignored, Wi-Fi power saving off
  (`wifi-powersave-off.service`), screen off (`screen-off.service`).
- **GPUs:** the Intel HD 630 is Frigate's (VA-API decoding, OpenVINO detection) and the only display device in
  `/dev/dri`. The GeForce GTX 1050 (4 GB) is for CUDA only. `nouveau` is blacklisted (2026-11-02), and since
  2026-11-10 NVIDIA's own driver 580 runs without its display modules: `/etc/modprobe.d/nvidia-compute-only.conf`
  blocks `nvidia_drm` and `nvidia_modeset`, so only `nvidia` and `nvidia_uvm` load. Otherwise a second `renderD`
  device appears and the numbering can swap at boot, taking Frigate's GPU. Installed with
  `ubuntu-drivers install --gpgpu nvidia:580` (Ubuntu's signed modules, so Secure Boot stays on;
  `linux-modules-nvidia-580-<kernel>` must exist for each kernel) plus nvidia-container-toolkit, which k3s picked up
  as the `nvidia` runtime. A pod gets the GPU with `runtimeClassName: nvidia` and env `NVIDIA_VISIBLE_DEVICES=all`.
  No device plugin, so Kubernetes doesn't count the GPU: the GPU jobs take turns through a lock file
  (`/srv/apps/gpu-lock`). Not 470, Ubuntu's "recommended": it stops at CUDA 11.4, and Whisper and PyTorch need CUDA 12.
- **Restarts:** on 2026-11-02 a reboot stuck in the BIOS until it was fixed by hand. After that fix, a
  restart with nobody there took 49 s, and the three apps were back within a minute.

## nginx

`nginx/conf.d/site.conf`. A name moved into the cluster has `proxy_pass http://192.168.1.116:80`
(Traefik). The `*.machaka.net` catch-all also goes to Traefik, so a new app in the cluster needs only
its Ingress and a DNS record. After a change: `docker exec nginx nginx -s reload`.

## Kubernetes (`k8s/`)

The kubeconfig context on the PC is `homelab`.

```
kubectl apply -f k8s/cluster/
kubectl apply -R -f k8s/apps/
kubectl apply -f k8s/tools/
kubectl apply -k k8s/monitoring/ --server-side   # --server-side: the dashboards are too big otherwise
```

`k8s/cluster/traefik-config.yaml` makes Traefik keep the X-Forwarded-For / X-Real-IP /
X-Forwarded-Proto that nginx sets; without it every visitor looks like 10.42.0.1 to the apps.

### Apps (namespace `apps`)

| App | Host | Runs on | Data (hostPath) |
|---|---|---|---|
| cat-weight | cat.machaka.net | Acer | `/srv/apps/cat-weight` |
| boardxplorer | boardxplorer.machaka.net | Acer | `/srv/apps/boardxplorer` |
| unmatched | unmatched.machaka.net | Acer | `/srv/apps/unmatched` |
| website | machaka.net, mohammad.machaka.net | server | `~/Personal-Website/tracking-data` |
| frigate | http://192.168.1.93:5000 (home network only) | Acer | `/srv/apps/frigate/config`, `/var/lib/frigate/media` |
| freemotion-hub (trial) | none yet: `kubectl -n apps port-forward svc/freemotion-hub 8790` | server | `/srv/apps/freemotion-hub`: a copy of the Docker hub's `hub.db` from 2026-11-10 |

The Free Motion hub is still live in Docker. The pod `freemotion-hub` is a trial on a copy of its data, with no
Ingress, no agent port and no Secret yet; the steps to move it are at the top of `k8s/apps/freemotion-hub/freemotion-hub.yaml`.

Scheduled jobs (CronJobs, no web page), moved from the Windows PC on 2026-11-10. The PC stays as a backup: its copies
of the code and data stopped updating that day.

| Job | Schedule | Runs on | Data (hostPath) |
|---|---|---|---|
| boardgamefinder | daily 07:00 Paris | Acer | `/srv/apps/boardgamefinder/{data,Log}` |
| cat-monitor | 02:16, 10:16, 18:16 Paris, GPU | Acer | `/srv/apps/cat-monitor`; reads `/var/lib/frigate/media/recordings` |
| bg-taste-match-fetch / -gpu / -llm | every 30 / 60 / 60 min; gpu uses the GPU | Acer | `/srv/apps/bg-taste-match` (code + data), audio in `/var/lib/bg-taste-match/audio` (1 TB disk) |

bg-taste-match also has an idle `bg-taste-match-shell` pod with the same mounts, for stages run by hand
(`kubectl -n apps exec deploy/bg-taste-match-shell -- python score.py <user>`). Its image holds only the environment:
the code is sent with `deploy/push_code.sh` in that repo, which never touches the live `bgtaste.db`.

**LLM calls** from the cluster go to fm-llm on the control node, which doesn't exist yet: the jobs assume
`FM_LLM_URL=http://192.168.1.116:8792` and this contract. `GET /healthz` answers 200 when it can take work.
`POST /v1/ask` takes JSON `{"model", "prompt", "system"?, "project", "purpose"}` and answers `{"text", "model"}`.
503 means no model is free or signed in now (callers treat it as "out of usage" and try later). Models asked for:
`gemini-pro` (bg-taste-match), `claude-haiku` and `claude-sonnet` (boardgamefinder). Until fm-llm answers, the LLM steps
fail or skip; nothing else depends on them.

All moved from Docker on 2026-10-24; the first three moved to the Acer on 2026-11-02. Their old data
folders on the server (`~/Cat-Weight/data`, `~/BoardXplorer/data`, `~/unmatched_matcher/data`) stopped
updating that day: they are a fallback only.

Frigate moved from Docker to the Acer on 2026-11-02. Its container on the server (`~/frigate`) is stopped:
never start it while the pod runs. Its old recordings copy slowly to the Acer through
`frigate-copy.service` there, which pulls from the temporary `frigate-rsyncd` on the server (read-only,
stops by itself after 48 hours). `/var/lib/frigate/copy-done` on the Acer means it finished. After that,
`~/frigate/storage` on the server can go.

Images are built on the server and loaded by hand until there is a registry. For the website:
`docker save <image> | sudo k3s ctr images import -`. For an app on the Acer:

```
docker save <image> -o /tmp/image.tar
scp /tmp/image.tar 192.168.1.93:/tmp/
ssh -t 192.168.1.93 sudo k3s ctr images import /tmp/image.tar
```

unmatched-tournament is retired (the tournaments live in unmatched); nginx answers
unmatchedtournament.machaka.net with a 301 to https://unmatched.machaka.net/tournaments.

Never `docker compose up` an app that runs in the cluster: two copies would write the same SQLite file.

**Undo a move** (cat-weight as the example):
1. In `nginx/conf.d/site.conf`, put back `proxy_pass http://cat_weight:8002;`, then reload nginx.
2. `kubectl -n apps scale deploy/cat-weight --replicas=0` (SQLite: never two copies at once).
3. `cd ~/Cat-Weight && docker compose start cat_weight`.

### Tools (namespace `tools`)

| Tool | Address | Login | What it does |
|---|---|---|---|
| Portainer | portainer.machaka.net | `admin` | Containers and pods: the cluster (`homelab`) and Docker on the server (`server-docker`, through `~/portainer-agent` on the server) |
| Beszel | beszel.machaka.net | owner's email | CPU, memory, disk, network and Docker containers per machine, with history. One agent per node |
| Uptime Kuma | status.machaka.net | `admin` | Checks each site and tool every minute; its status page `homelab` feeds the home page |
| Home page | lab.machaka.net | (Cloudflare only) | Homepage (`k8s/tools/homepage.yaml`): every site and tool with live numbers; its Pipelines tiles read the lab's `/api/summary/*` (each stage's progress and state: running, paused, last run failed) |
| Taste Match, Deals, Cat cam | tastematch., deals., catcam.machaka.net | (Cloudflare only) | One page per job, built for what it's for (`lab/`, `k8s/tools/lab.yaml`): the review pipeline by channel; the best Okkazeo deals now, new cheap listings and bundles to judge; the cats' flash of the day (CronJob `cat-flash`), camera and weights. Each checks the Access token itself; a name not yet in the Access application signs in through lab.machaka.net's login (the bridge at lab.machaka.net/lab-sso, `lab/app/access.py`) |
| Alertmanager | `kubectl -n tools port-forward svc/alertmanager 9093:9093` | none | Emails Prometheus's alerts (`k8s/tools/alertmanager.yaml`); silence an alert in its page |

**Alerts** go by email to the owner's Gmail (the `to` key of the Secret below), sent through Gmail SMTP with the same Gmail sender
as the Free Motion night emails (Secret `tools/alert-smtp`). Their settings live in each tool's own
database, not in this repo:

- Uptime Kuma: notification "Email (Gmail)", on every check (and the default for new ones): a
  site down after 3 failed checks, and back up.
- Beszel, for every machine: down for 2 min; CPU or memory above 90% for 10 min; disk above 85%.
  Settings → Notifications in Beszel to change them. On the Acer its agent is the NVIDIA variant, so Beszel
  also shows the GTX 1050's load, memory, temperature and power.
- Prometheus (since 2026-11-10): 38 rules in `k8s/monitoring/alert-rules.yml`, sent by Alertmanager. Its config
  is in the repo, but the addresses aren't: an init container copies `user` and `to` from the Secret into it.
  Critical alerts repeat every 3 hours while they last, warnings every 12, and each one mails again once resolved.
  The rules cover what Beszel can't see:
  - machines: down, NotReady, memory 90/97%, any kernel out-of-memory kill, CPU busy or load high for 30 min,
    disk under 10%/4% free or full within a day, a disk gone read-only, sensors above 90/97 °C;
  - the GPU: no metrics (exporter down, or the driver didn't load after a kernel update), memory above 92%,
    above 85/92 °C, flat out for two days;
  - Kubernetes: a container killed for memory or near its limit, crash loops, a pod that can't start (missing
    image, bad config) or stays Pending, a Deployment or DaemonSet short of pods, a scrape target down;
  - scheduled jobs: any Job that fails (or runs past its time limit), BoardGameFinder with no good run in 26 h,
    no cat feeding report in 17 h, bg-taste-match downloads stalled 12 h, any other Job running over 6 h;
  - Frigate: camera below 1 fps, no metrics, detector slower than 100 ms, storage under 10%;
  - itself: emails failing, broken rules, Prometheus unable to reach Alertmanager.

  Change a threshold in `alert-rules.yml`, check it with
  `kubectl -n monitoring exec -i deploy/prometheus -- sh -c 'cat > /tmp/r.yml && promtool check rules /tmp/r.yml' < k8s/monitoring/alert-rules.yml`,
  then `kubectl apply -k k8s/monitoring/ --server-side`.

### Monitoring (namespace `monitoring`)

Prometheus collects every 30 s and keeps 15 days (at most 4 GB); Grafana (grafana.machaka.net, `admin`)
draws it. What is collected is all in `k8s/monitoring/prometheus.yml`:

| Job | What | From |
|---|---|---|
| node-exporter | each machine: CPU, memory, disks, network, temperatures | DaemonSet, port 9110 on every node |
| process-exporter | each machine: memory per program, outside containers | DaemonSet, port 9256 on each pod |
| kubelet-cadvisor | each pod's containers | the kubelets, through the API server |
| kube-state-metrics | pods up, restarts, requests and limits | Deployment |
| cadvisor-docker | the Docker containers on the server | cAdvisor in `docker-compose.yml`, port 8081 |
| frigate | camera fps, detector speed | Frigate's `/api/metrics` |
| pods | any pod annotated `prometheus.io/scrape: "true"` (Traefik, the GPU exporter) | the pods |
| (pods) nvidia-gpu-exporter | the Acer's GTX 1050: load, memory, temperature, clocks (`nvidia_smi_*`) | DaemonSet on the Acer, port 9835, `gpu-exporter.yaml` |
| alertmanager | whether alert emails go out | Alertmanager in `tools` |

**Dashboards made here** (folder Homelab), generated by `k8s/monitoring/dashboards/make.py` (edit it, run it, apply):
phone first, so each reads top to bottom as answers, details in collapsed rows, each machine in its own colour.

- **Homelab** (Grafana opens on it): is anything wrong? Alerts firing (and which), both machines up, failed job runs and
  out-of-memory kills in 24 h, then each machine's CPU, memory, fullest disk and hottest sensor, the GPU and what's on it.
- **What is using the machines** (pick a machine): memory and CPU per app or job, the GPU per program (from the
  `gpu-procs` collector), programs outside containers, Docker on the server; then the same over time.
- **Scheduled jobs**: time since each job's last good run, what's paused or running, every run of the last 7 days
  (when, how long, result) read back from Prometheus, run times, memory against the limit, out-of-memory kills.
- **PC jobs**: the Windows PC's scheduled tasks and its jobs' numbers (archived today, FreeMotion, Upwork), from the
  reports `lab/pc-report/report.ps1` sends to the lab every 10 minutes (set up with `lab/pc-report/install.ps1`).

`gpu-procs.yaml` is the per-process GPU collector: `nvidia-smi` per process plus each process's cgroup, which names
its pod (`gpu_process_memory_bytes`, `gpu_process_sm_percent`; join `pod_uid` to `kube_pod_info`'s `uid`).

The dashboards from grafana.com sit in the folder **More detail** (Node Exporter Full, Kubernetes / Views / Global and
Pods, Traefik, Docker containers), with Node temperatures (made here: every sensor). The Docker one had its Compose
filter removed, because cAdvisor here does not keep container labels. Each folder is its own mounted volume:
Grafana doesn't follow the directory links of one projected volume with subfolders.

Without Cloudflare: `kubectl -n monitoring port-forward svc/grafana 3000:80` (http://localhost:3000)
and `kubectl -n monitoring port-forward svc/prometheus 9090:80` (no login, so no Ingress).

The Docker node-exporter (port 9100) is no longer read: it only sees its own container's network.

Memory (2026-10-25): tools ~250 MB, Prometheus ~120 MB, Grafana ~300-450 MB.

### Publishing a name

1. An Ingress in the cluster (host name, class `traefik`).
2. Cloudflare DNS: a CNAME to the tunnel, proxied (or one wildcard `*` record for all).
3. Cloudflare Zero Trust → Access: add the name to the self-hosted app, so it asks for the login first.
4. Optional: a site in `lab/app/config.py` (SITES), a card or page in the lab for a job, and a check in Uptime Kuma.

### Secrets

Not in this repo. Made on the server with `sudo k3s kubectl create secret ...`:

| Secret | From |
|---|---|
| apps/boardxplorer-env | `~/BoardXplorer/.env` (quotes stripped) |
| apps/unmatched-env | `~/unmatched_matcher/.env` |
| apps/website-env | `~/Personal-Website/.env` |
| tools/portainer-agent | AGENT_SECRET, same as `~/portainer-agent/.env` |
| tools/beszel-agent | KEY: the Beszel hub's public key (from `id_ed25519` in its volume) |
| tools/tool-logins | passwords of Portainer, Beszel, Uptime Kuma |
| monitoring/grafana-admin | admin-user, admin-password |
| tools/alert-smtp | user, password (Gmail app password), to |
| tools/homepage-env | HOMEPAGE_VAR_PORTAINER_KEY: a Portainer API key named "homepage" |
| tools/lab-intake | token: the PC's reports to the lab (the PC keeps it in %USERPROFILE%\.lab-token; `lab/pc-report/install.ps1` makes both) |

Read a password in your own terminal:

```
kubectl -n tools get secret tool-logins -o go-template='{{range $k,$v := .data}}{{$k}}: {{$v | base64decode}}{{"\n"}}{{end}}'
kubectl -n monitoring get secret grafana-admin -o go-template='{{index .data "admin-password" | base64decode}}{{"\n"}}'
```

## Backups

Before the move to k3s: `~/backups/pre-k3s-20261024-1641/` on the server, and a copy on the PC.
Tools and monitoring keep their data on local-path volumes (`/var/lib/rancher/k3s/storage/`).
k3s takes its own database snapshot every 12 hours and keeps 5, in `/var/lib/rancher/k3s/server/db/snapshots/`.
A restore also needs `/var/lib/rancher/k3s/server/token`. Nothing is copied off the server or the Acer
on a schedule. The data in `/srv/apps` on the Acer has only two copies from 2026-11-02: its old folders
on the server, and `~/homelab-backup/2026-11-02/` on the PC, which also holds the `.env` files and tool logins.
