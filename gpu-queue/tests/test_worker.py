"""The PC worker against a real hub, with a stand-in for Whisper (a small Python script): an item done end to end,
a pause file that stops the command halfway and puts the item back untouched, the memory cap, a failing command,
and the rules for when it may work. Run: python -m pytest -q gpu-queue/tests"""
import datetime as dt
import json
import os
import subprocess
import sys
import time

import pytest

import worker
from worker import Gates, in_hours, run

FAKE = """
import json, sys, time
inp, out, wait = sys.argv[1], sys.argv[2], float(sys.argv[3])
time.sleep(wait)
if sys.argv[4:] == ["fail"]:
    print("CUDA out of memory"); sys.exit(1)
open(out, "w").write(json.dumps({"from": open(inp, "rb").read().decode(), "name": inp.rsplit(".", 1)[1]}))
"""


def conf_for(env, wait=0.0, extra=(), **over):
    fake = env["tmp"] / "fake_whisper.py"
    fake.write_text(FAKE)
    conf = {**worker.DEFAULTS, "hub": env["url"], "worker": "pc-test", "check_s": 0.05, "heartbeat_s": 0.1,
            "pause_file": str(env["tmp"] / "pause"), "work_dir": str(env["tmp"] / "work"),
            "kinds": {"whisper": {"command": [sys.executable, str(fake), "{input}", "{output}", str(wait), *extra],
                                  "output_ext": ".json3"}}}
    conf.update(over)
    return conf


def add(env, *keys):
    for k in keys:
        (env["audio"] / f"{k}.m4a").write_bytes(f"audio of {k}".encode())
        env["hub"].q.add("whisper", k)


def test_items_done_end_to_end(env):
    add(env, "a", "b")
    conf = conf_for(env)
    assert run(conf, env["client"], Gates(conf)) == {"done": 2}
    got = json.loads((env["captions"] / "a.json3").read_text())
    assert got == {"from": "audio of a", "name": "m4a"}         # the input kept its extension
    assert os.listdir(conf["work_dir"]) == ["a.log", "b.log", "worker.lock"]   # inputs and outputs cleaned up


def test_newest_first_by_default(env):
    add(env, "a", "b")
    conf = conf_for(env)
    run(conf, env["client"], Gates(conf), once=True)
    assert [p.name for p in env["captions"].iterdir()] == ["b.json3"]


def test_pause_file_stops_halfway_and_the_item_goes_back(env):
    add(env, "a")
    conf = conf_for(env, wait=30)
    pause = env["tmp"] / "pause"
    calls = {"n": 0}

    class PauseLater(Gates):
        def why_not(self, starting):
            calls["n"] += 1
            if calls["n"] == 3:
                pause.write_text("")
            return super().why_not(starting)
    t0 = time.monotonic()
    assert run(conf, env["client"], PauseLater(conf)) == {"stopped": 1}
    assert time.monotonic() - t0 < 15                            # killed, not waited for
    row = env["hub"].q.get(1)
    assert row["state"] == "waiting" and row["attempts"] == 0 and row["error"] == "released: pause file"
    assert not (env["captions"] / "a.json3").exists()
    assert run(conf, env["client"], Gates(conf)) == {}            # paused: claims nothing
    assert env["hub"].q.get(1)["state"] == "waiting"


def test_memory_cap_stops_the_command_and_counts_a_try(env):
    add(env, "a")
    conf = conf_for(env, wait=30, memory_cap_mib=1)             # any Python is over 1 MiB
    assert run(conf, env["client"], Gates(conf), once=True) == {"failed": 1}
    row = env["hub"].q.get(1)
    assert row["state"] == "waiting" and row["attempts"] == 1 and "over the cap" in row["error"]


def test_a_failing_command_is_reported_with_its_last_lines(env):
    add(env, "a")
    conf = conf_for(env, extra=["fail"])
    assert run(conf, env["client"], Gates(conf), once=True) == {"failed": 1}
    row = env["hub"].q.get(1)
    assert row["attempts"] == 1 and "exit 1, no output" in row["error"] and "CUDA out of memory" in row["error"]


def test_hub_down_ends_the_run(env, tmp_path):
    conf = conf_for(env)
    dead = worker.Client("http://127.0.0.1:9", "x", timeout=2)
    assert run(conf, dead, Gates(conf)) == {"hub error": 1}


def test_one_run_at_a_time(env):
    conf = conf_for(env)
    held = worker.take_lock(conf["work_dir"])
    assert held is not None
    assert worker.take_lock(conf["work_dir"]) is None
    assert run(conf, env["client"], Gates(conf)) == {}
    held.close()


# ---- when it may work --------------------------------------------------------------------------------------------

def at(h, m=0):
    return dt.datetime(2026, 11, 27, h, m)


@pytest.mark.parametrize("spec,time_,ok", [
    (None, at(15), True), ("01:00-07:30", at(1), True), ("01:00-07:30", at(7, 29), True),
    ("01:00-07:30", at(7, 30), False), ("01:00-07:30", at(0, 59), False),
    ("23:00-07:00", at(23, 30), True), ("23:00-07:00", at(3), True), ("23:00-07:00", at(12), False)])
def test_hours(spec, time_, ok):
    assert in_hours(spec, time_) is ok


def gates(tmp_path, procs=(), idle=10_000, gpu=None, now=at(3), **conf):
    c = {**worker.DEFAULTS, "pause_file": str(tmp_path / "pause"), **conf}
    return Gates(c, now=lambda: now, procs=lambda: list(procs), idle=lambda: idle, gpu_info=lambda: gpu)


def test_gates(tmp_path):
    assert gates(tmp_path).why_not(True) is None
    assert gates(tmp_path, hours="01:00-07:00", now=at(9)).why_not(True) == "outside hours 01:00-07:00"
    game = [(1, 0, "EldenRing.exe")]
    assert gates(tmp_path, procs=game, busy_processes=["eldenring.exe"]).why_not(False) == "busy: EldenRing.exe"
    # idle: 15 minutes to start; once running, only input in the last 2 minutes stops it
    assert gates(tmp_path, idle=600, idle_minutes=15).why_not(True).startswith("the user is here")
    assert gates(tmp_path, idle=600, idle_minutes=15).why_not(False) is None
    assert gates(tmp_path, idle=30, idle_minutes=15).why_not(False).startswith("the user is here")
    assert gates(tmp_path, idle=None, idle_minutes=15).why_not(True) == "can't tell whether the user is here"
    # GPU: heat stops it; memory used by others only blocks a start; unreadable fails closed
    hot = {"temp_c": 84.0, "mem_used_mib": 300.0}
    assert gates(tmp_path, gpu=hot, max_gpu_c=83).why_not(False) == "GPU at 84 C"
    busy = {"temp_c": 50.0, "mem_used_mib": 3000.0}
    assert gates(tmp_path, gpu=busy, max_gpu_mem_used_mib=800).why_not(True).startswith("GPU in use")
    assert gates(tmp_path, gpu=busy, max_gpu_mem_used_mib=800).why_not(False) is None
    assert gates(tmp_path, gpu=None, max_gpu_c=83).why_not(False) == "can't read the GPU (nvidia-smi)"
    (tmp_path / "pause").write_text("")
    assert gates(tmp_path).why_not(True) == "pause file"


def test_process_tree_and_memory_are_read():
    """The real probes on this machine: a child shows up under us, with some memory."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        time.sleep(0.5)
        procs = worker.processes()
        pids = worker.tree(os.getpid(), procs)
        assert child.pid in pids
        assert worker.memory_mib([child.pid]) > 1
    finally:
        worker.kill_tree(child)
    assert child.poll() is not None
