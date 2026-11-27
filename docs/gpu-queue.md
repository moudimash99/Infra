# GPU work on the PC from a queue (Infra#14): what is built, what waits for a decision

Status 2026-11-27: **code and tests only. Nothing runs, nothing is deployed, nothing is in `k8s/` or `services.yaml`.**

## What is built

`gpu-queue/` holds three files, all standard-library Python, so nothing needs installing:

- `workqueue.py`: the queue. It keeps the work items in one SQLite file and lends each one out on a lease, a random
  token that lasts 15 minutes and is renewed while the work runs.
  - A lease that runs out puts the item back in the queue. That happens when the PC sleeps or the worker dies.
  - Each claim counts as a try. After 3 tries the item is marked failed and waits for a person, as
    `captions/<id>.tries` does on the Acer. `hub.py retry` puts it back.
  - When the worker is told to stop (the pause file, a game, the end of its hours), it releases the item. That puts
    it back without counting a try.
  - Only the holder of the current lease can finish or fail an item.
- `hub.py`: the queue over HTTP.
  - It needs a token. The calls about one item also need that item's lease.
  - A worker claims an item, downloads its input (the audio file), and uploads the result.
  - The hub writes the result where bg-taste-match keeps it today, `captions/<id>.json3`, then marks the item done.
  - If captions already exist (the Acer's own Whisper job got there first), the hub keeps them and marks the item
    done.
  - A result that isn't JSON is refused and counts as a try.
  - Video ids are checked, so a key can never be a file path.
  - `/metrics` gives Prometheus the counts per state, the age of the oldest waiting item, and when an item last
    finished.
  - `hub.py fill whisper` adds every audio file that has no captions and no `.tries` file.
- `worker.py`: the PC worker, run as a plain scheduled task. It does one item at a time, with one run at a time
  (a lock file).
  - It starts only when the rules allow. While the command runs, it checks again every 15 seconds.
  - It stops the whole process tree at once for any of these:
    - the pause file (`~/.gpu-worker-pause`);
    - the end of the allowed hours;
    - a listed game running;
    - the user coming back (when `idle_minutes` is set);
    - the GPU too hot, or its temperature unreadable;
    - the command using more than the memory cap (2,560 MiB by default, counted over all its processes).
  - It renews the lease every 2 minutes.
  - When a run ends, it deletes the downloaded input and its output.
  - `worker.py --dry-run` says whether it may work now, and why not.
- The examples: `kinds.example.json` (where the hub finds inputs and writes results) and `gpu-worker.example.json`
  (the PC's settings; the Whisper command in it is a placeholder, see below).
- The proposed manifest is `docs/gpu-queue.proposed.yaml`: the hub on the Acer, a NodePort for the home network, and
  a fill CronJob.

Tests: `python -m pytest -q gpu-queue/tests` (35 tests, about 12 s).
- The worker tests run a real hub on a local port, with a small Python script standing in for Whisper.
- They cover:
  - an item done end to end;
  - the pause file stopping the command halfway, with the item going back untried;
  - the memory cap;
  - a failing command, reported with its last lines;
  - the hub being down;
  - one run at a time;
  - the time windows;
  - each stop rule;
  - the real Windows process and memory readings.

## What is still missing, outside this repo or needing a deploy

1. **bg-taste-match needs a "transcribe this one file" entry point** (`<audio> <out.json3>`), using
   `whisper_captions.py`'s model and piece settings. That is a change in bg-taste-match, not in this repo, and I
   couldn't read that repo from here. The worker runs whatever `command` says, with `{input}` and `{output}` filled
   in. The PC already has a working Whisper setup: it transcribed 231 videos on 2026-11-18.
2. **"Results arrive in bg-taste-match's DB":** today's captions are files (`captions/<id>.json3`). The lab counts
   a video as transcribed from those files. I couldn't check here whether `whisper_captions.py` also writes a DB
   row. If it does, the new entry point or the hub has to write that row too.
3. **The deploy:**
   - copy the code to the Acer;
   - create the Secret;
   - move the proposed manifest into `k8s/`;
   - add the services.yaml entries below (then menu.py and readmes.py);
   - on the PC, save `~/.gpu-worker.json` and `~/.gpu-worker-token` and register the scheduled task.
4. **The status check:** status.py has no probe that reads a queue. The entries below use the existing kinds (the
   pod, an HTTP check, the PC task). The queue's numbers go to Prometheus through `/metrics`. A status.py probe for
   "last done" is a small change once the hub runs.

## Decisions for you

**1. Where the hub runs.**
- A. **Its own small pod on the Acer** (the draft). It sits next to the audio and the captions and writes them
  directly. It costs about 32 MiB of booked memory on the Acer, which is booked full.
- B. **Inside the lab.** The lab already has a home-network port with a token (30881). But its mounts of
  bg-taste-match are read-only on purpose, and the hub would turn it into a writer. It would also need porting to
  FastAPI.
- C. **On the server** (the issue's first idea: more memory). The audio and captions are on the Acer, so the server
  would need them over NFS from the Acer, or would have to send results on in a second step.

**2. When the PC may work.** Every option below is a setting in `~/.gpu-worker.json`; no code changes.
- A. **Nights only**, for example `"hours": "01:00-07:30"`, with games in `busy_processes`.
- B. **Any time the PC is idle**: `"idle_minutes": 20`. It stops when there was input in the last 2 minutes.
- C. **Both** (the example file): nights, and only when idle.
- Heat: `max_gpu_c`. I didn't choose a level for the GTX 1660. `nvidia-smi -q -d TEMPERATURE` shows the card's own
  slowdown level. The example's 83 is a placeholder.
- Noise is not measured.

**3. How the Acer and the PC share the Whisper backlog** (the Acer first, the PC for overflow, as you set on
11-23). The Acer's `bg-taste-match-gpu` runs hourly since 2026-11-23 with the heat guard. The issue's "paused
because it overheats" is out of date.
- A. **The fill CronJob plus newest-first** (the draft). The Acer goes through its channel list from the front, and
  the PC takes from the end of the queue. They can still meet on one video: the first result is kept and the other
  run's work is wasted. Nothing changes in bg-taste-match except the entry point.
- B. **The Acer's job claims from the queue too** (a bg-taste-match change to `run_gpu.sh`). They never overlap, and
  the queue becomes the one list.
- C. **The Acer stops transcribing and the PC does it all.** This goes against "the Acer first", and the PC works
  only some hours.

**4. The alert.** With 1,517 hours of audio waiting, "items wait for days" would fire from day one and stay on.
- A. **"Nothing finished for 2 days while items wait"** (below).
- B. **"The oldest item has waited N days"**, with N above the backlog's expected length.
- C. **Both.**

**5. Volunteers (bg-taste-match#6).** The token and the per-item lease are there. Sign-in per volunteer and checks
beyond "is it JSON" are not.

The cat monitor is out of scope: it was removed on 2026-11-24 (services.yaml, cat-camera-jobs), and the 11-23
comment keeps it off the PC anyway.

## Proposed services.yaml entries (not added: nothing runs yet)

```yaml
- name: gpu-queue
  what: "The GPU work queue (Infra#14): work items with leases over HTTP; workers (the PC first) claim, download the input, upload the result. Whisper first: results written to bg-taste-match's captions/."
  kind: web-app
  system: board-games
  lifecycle: experimental
  depends_on: [bg-taste-match]
  runs: k8s apps/Deployment gpu-queue and apps/CronJob gpu-queue-fill on acer-laptop
  manifest: Infra/k8s/apps/gpu-queue/gpu-queue.yaml
  image: "the lab's image (any Python 3.11+; stdlib only), pull Never"
  schedule:
    fill: "40 */6 * * * (UTC)"
  url: "NodePort 30895 on the home network (token in Secret apps/gpu-queue)"
  data:
    - "hostPath /srv/apps/gpu-queue on the Acer (queue.db; code/ copied by hand)"
    - "hostPath /srv/apps/bg-taste-match/captions (written) and /var/lib/bg-taste-match/audio (read-only)"
  code:
    repo: C:\Users\Moudimash99\Documents\Coding\Infra\gpu-queue
    branch: main
    deployed: "<commit>, copied by hand to /srv/apps/gpu-queue/code"
  deploy: "Copy gpu-queue/hub.py, workqueue.py and kinds.json to /srv/apps/gpu-queue/code on the Acer, kubectl -n apps rollout restart deploy/gpu-queue."
  check: "GET /healthz; /metrics (gpu_queue_items, gpu_queue_last_done_timestamp_seconds); hub.py status"
  probes:
    - {k8s: apps/Deployment/gpu-queue}
    - {k8s: apps/CronJob/gpu-queue-fill, max_age_h: 7}
    - {http: "http://192.168.1.93:30895/healthz", expect: [200]}
  drift: []
  notes: []

- name: gpu-worker-pc
  what: "The PC's GPU worker (Infra#14): takes Whisper items from gpu-queue when the PC may work (hours, idle, no game, GPU cool), one at a time, memory cap 2.5 GiB; a pause file stops it at once."
  kind: pc-task
  system: board-games
  lifecycle: experimental
  depends_on: [gpu-queue]
  runs: "PC scheduled task \\gpu-worker (every 30 min; python gpu-queue/worker.py)"
  schedule: "every 30 minutes; works only inside its hours (~/.gpu-worker.json)"
  data:
    - "~/.gpu-worker.json, ~/.gpu-worker-token; ~/.gpu-worker (one input at a time, logs)"
  code:
    repo: C:\Users\Moudimash99\Documents\Coding\Infra\gpu-queue
    branch: main
  probes:
    - {pc_task: "\\gpu-worker"}
```

## Proposed alert (option 4A), for `k8s/monitoring/alert-rules.yml`

```yaml
- alert: GpuQueueNothingDone
  expr: (time() - gpu_queue_last_done_timestamp_seconds > 2 * 86400) and on() sum(gpu_queue_items{state="waiting"}) > 0
  for: 1h
  annotations:
    summary: "GPU queue: items wait and none finished in 2 days (PC worker off, paused, or failing)"
```

## The PC's scheduled task (not registered; check before use)

Every 30 minutes, at most one at a time, and only while the PC is on. The worker's own rules decide whether it
works.

```powershell
$py = (Get-Command python).Source
$act = New-ScheduledTaskAction -Execute $py -Argument "`"$PWD\gpu-queue\worker.py`"" -WorkingDirectory "$PWD\gpu-queue"
$trg = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 30)
$set = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 8) -StartWhenAvailable
Register-ScheduledTask -TaskName gpu-worker -Action $act -Trigger $trg -Settings $set
```
