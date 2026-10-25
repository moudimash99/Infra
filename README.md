# Infra

Everything that runs on the home server, 192.168.1.135 (Ubuntu, 8 cores, 7.6 GB). The server keeps a
clone of this repo in `~/Infra`; Docker runs from there.

```
internet ─ Cloudflare (DNS, Access login) ─ tunnel ─ cloudflared ─ nginx ─┬─ Docker containers (proxy_net)
                                                                          └─ k3s Traefik :80 ─ pods, by host name
```

- **Docker** (`docker-compose.yml`, `nginx/`): cloudflared, nginx, cAdvisor, node-exporter. Frigate and
  the Free Motion hub run in Docker too, from their own folders.
- **Kubernetes** (`k8s/`): k3s, one node today. The desktop (WSL2) and a second machine join later as
  servers (k3s was installed with `--cluster-init`).
- `Prometheus/`: the old setup for an EC2 machine, not used.

## nginx

`nginx/conf.d/site.conf`. A name moved into the cluster has `proxy_pass http://192.168.1.135:80`
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

| App | Host | Data (hostPath on .135) |
|---|---|---|
| cat-weight | cat.machaka.net | `~/Cat-Weight/data` |
| boardxplorer | boardxplorer.machaka.net | `~/BoardXplorer/data` |
| unmatched | unmatched.machaka.net | `~/unmatched_matcher/data` |
| website | machaka.net, mohammad.machaka.net | `~/Personal-Website/tracking-data` |

All moved from Docker on 2026-10-24. Images are built on the server and loaded by hand
(`docker save <image> | sudo k3s ctr images import -`) until there is a registry.

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
| Portainer | portainer.machaka.net | `admin` | Containers and pods: the cluster (`homelab`) and Docker on .135 (`server-docker`, through `~/portainer-agent` on the server) |
| Beszel | beszel.machaka.net | owner's email | CPU, memory, disk, network and Docker containers per machine, with history. One agent per node |
| Uptime Kuma | status.machaka.net | `admin` | Checks each site and tool every minute; its status page `homelab` feeds the home page |
| Home page | lab.machaka.net | (Cloudflare only) | One page with every site and tool and their live numbers (Homepage, `k8s/tools/homepage.yaml`) |

**Alerts** go by email to the owner's Gmail (the `to` key of the Secret below), sent through Gmail SMTP with the same Gmail sender
as the Free Motion night emails (Secret `tools/alert-smtp`). Their settings live in each tool's own
database, not in this repo:

- Uptime Kuma: notification "Email (Gmail)", on every check (and the default for new ones): a
  site down after 3 failed checks, and back up.
- Beszel, for every machine: down for 2 min; CPU or memory above 90% for 10 min; disk above 85%.
  Settings → Notifications in Beszel to change them.

### Monitoring (namespace `monitoring`)

Prometheus collects every 30 s and keeps 15 days (at most 4 GB); Grafana (grafana.machaka.net, `admin`)
draws it. What is collected is all in `k8s/monitoring/prometheus.yml`:

| Job | What | From |
|---|---|---|
| node-exporter | each machine: CPU, memory, disks, network | DaemonSet, port 9110 on every node |
| kubelet-cadvisor | each pod's containers | the kubelets, through the API server |
| kube-state-metrics | pods up, restarts, requests and limits | Deployment |
| cadvisor-docker | the Docker containers on .135 | cAdvisor in `docker-compose.yml`, port 8081 |
| frigate | camera fps, detector speed | Frigate's `/api/metrics` |
| pods | any pod annotated `prometheus.io/scrape: "true"` (Traefik today) | the pods |

Dashboards (folder Homelab, from grafana.com, in `k8s/monitoring/dashboards/`): Node Exporter Full,
Kubernetes / Views / Global and Pods, Traefik, Docker containers. The Docker one had its Compose
filter removed, because cAdvisor here does not keep container labels.

Without Cloudflare: `kubectl -n monitoring port-forward svc/grafana 3000:80` (http://localhost:3000)
and `kubectl -n monitoring port-forward svc/prometheus 9090:80` (no login, so no Ingress).

The Docker node-exporter (port 9100) is no longer read: it only sees its own container's network.

Memory (2026-10-25): tools ~250 MB, Prometheus ~120 MB, Grafana ~300-450 MB.

### Publishing a name

1. An Ingress in the cluster (host name, class `traefik`).
2. Cloudflare DNS: a CNAME to the tunnel, proxied (or one wildcard `*` record for all).
3. Cloudflare Zero Trust → Access: add the name to the self-hosted app, so it asks for the login first.
4. Optional: a tile in `k8s/tools/homepage.yaml`, and a check in Uptime Kuma.

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

Read a password in your own terminal:

```
kubectl -n tools get secret tool-logins -o go-template='{{range $k,$v := .data}}{{$k}}: {{$v | base64decode}}{{"\n"}}{{end}}'
kubectl -n monitoring get secret grafana-admin -o go-template='{{index .data "admin-password" | base64decode}}{{"\n"}}'
```

## Backups

Before the move to k3s: `~/backups/pre-k3s-20261024-1641/` on the server, and a copy on the PC.
Tools and monitoring keep their data on local-path volumes (`/var/lib/rancher/k3s/storage/`).
No regular backup yet.
