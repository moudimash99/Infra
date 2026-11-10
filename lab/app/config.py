"""Where the lab finds things. Every value can be overridden by an environment variable, so the same code runs in
the pod (defaults) and on a PC for development (kubectl proxy, port-forwards, local copies)."""
import os
from zoneinfo import ZoneInfo


def env(name, default):
    return os.environ.get(name, default)


TZ = ZoneInfo(env("LAB_TZ", "Europe/Paris"))

# Kubernetes API: in the pod, the ServiceAccount token; in development, `kubectl proxy` (no token).
K8S_API = env("K8S_API", "https://kubernetes.default.svc")
K8S_TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
K8S_CA_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

PROMETHEUS = env("PROMETHEUS_URL", "http://prometheus.monitoring.svc")
ALERTMANAGER = env("ALERTMANAGER_URL", "http://alertmanager.tools.svc:9093")
FRIGATE = env("FRIGATE_URL", "http://192.168.1.93:5000")
FRIGATE_CAMERA = env("FRIGATE_CAMERA", "living_room")
MAIL_CLASSIFIER = env("MAIL_CLASSIFIER_URL", "http://mail-classifier.apps.svc")

BGT_DIR = env("BGT_DIR", "/data/bg-taste-match")              # bgtaste.db, captions/, diar/, results.html
BGT_AUDIO = env("BGT_AUDIO", "/data/bg-taste-match-audio")
BGF_DIR = env("BGF_DIR", "/data/boardgamefinder")              # Games.db, Output/, pipeline_state.json
CATS_DIR = env("CATS_DIR", "/data/cat-monitor")                # rolling_runs/
CAT_WEIGHT_DB = env("CAT_WEIGHT_DB", "/data/cat-weight/cat_data.db")
LAB_DB = env("LAB_DB", "/srv/lab/lab.db")

# Sites shown on the front page: name, public address, in-cluster address to check.
SITES = [
    ("machaka.net", "https://machaka.net", env("SITE_WEBSITE", "http://website.apps.svc")),
    ("Cat weight", "https://cat.machaka.net", env("SITE_CATWEIGHT", "http://cat-weight.apps.svc")),
    ("Unmatched", "https://unmatched.machaka.net", env("SITE_UNMATCHED", "http://unmatched.apps.svc")),
    ("BoardXplorer", "https://boardxplorer.machaka.net", env("SITE_BOARDXPLORER", "http://boardxplorer.apps.svc")),
    ("FreeMotion", "https://freemotion.machaka.net", env("SITE_FREEMOTION", "")),
]

TOOLS = [
    ("Grafana", "https://grafana.machaka.net/d/home-overview", "Charts over Prometheus"),
    ("Uptime Kuma", "https://status.machaka.net", "Checks every site each minute"),
    ("Beszel", "https://beszel.machaka.net", "Machines with history"),
    ("Portainer", "https://portainer.machaka.net", "Containers and the cluster"),
    ("Frigate", "http://192.168.1.93:5000", "Living-room camera (home network only)"),
]

# A report from the PC older than this means the PC is off or asleep, not that its jobs failed.
PC_STALE_MINUTES = int(env("PC_STALE_MINUTES", "20"))
