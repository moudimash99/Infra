"""The hub over real HTTP on a free port: the token, a claim, the input download, the result written where the
project keeps it, an existing result kept, bad results refused, fill. Run: python -m pytest -q gpu-queue/tests"""
import json
import urllib.request

import pytest

from worker import Client, HubError


def test_token_needed_but_not_for_health_and_metrics(env):
    with pytest.raises(HubError) as e:
        Client(env["url"], "wrong").call("GET", "/status")
    assert e.value.code == 401
    bare = Client(env["url"], "")
    assert bare.call("GET", "/healthz") == {"ok": True}


def test_claim_download_upload(env):
    (env["audio"] / "vid1.m4a").write_bytes(b"AUDIO")
    c = env["client"]
    assert c.call("POST", "/items", {"kind": "whisper", "keys": ["vid1"]}) == {"added": 1}
    item = c.claim("pc", ["whisper"], True)
    assert item["key"] == "vid1"
    dest = env["tmp"] / "got"
    assert c.call("GET", f"/items/{item['id']}/input", lease=item["lease"], to_file=str(dest)) == "vid1.m4a"
    assert dest.read_bytes() == b"AUDIO"
    c.call("POST", f"/items/{item['id']}/heartbeat", {}, lease=item["lease"])
    res = c.call("PUT", f"/items/{item['id']}/result", b'{"events": []}', lease=item["lease"], raw=True)
    assert res == {"ok": True, "kept_existing": False}
    assert json.loads((env["captions"] / "vid1.json3").read_text()) == {"events": []}
    assert c.call("GET", "/status")["counts"]["whisper"]["done"] == 1
    metrics = urllib.request.urlopen(env["url"] + "/metrics").read().decode()
    assert 'gpu_queue_items{kind="whisper",state="done"} 1' in metrics
    assert "gpu_queue_last_done_timestamp_seconds" in metrics


def test_wrong_or_old_lease_is_refused(env):
    (env["audio"] / "vid1.m4a").write_bytes(b"AUDIO")
    env["hub"].q.add("whisper", "vid1")
    item = env["client"].claim("pc", None, False)
    for call in ("heartbeat", "release"):
        with pytest.raises(HubError) as e:
            env["client"].call("POST", f"/items/{item['id']}/{call}", {}, lease="not-it")
        assert e.value.code == 409


def test_a_result_already_there_is_kept_and_skipped_at_claim(env):
    """The Acer's own Whisper job did the video meanwhile: its captions stay and the item is done."""
    for k in ("a", "b"):
        (env["audio"] / f"{k}.m4a").write_bytes(b"AUDIO")
        env["hub"].q.add("whisper", k)
    item = env["client"].claim("pc", None, False)
    assert item["key"] == "a"
    (env["captions"] / "a.json3").write_text('{"by": "acer"}')
    (env["captions"] / "b.json3").write_text('{"by": "acer"}')
    res = env["client"].call("PUT", f"/items/{item['id']}/result", b'{"by": "pc"}', lease=item["lease"], raw=True)
    assert res["kept_existing"] is True
    assert json.loads((env["captions"] / "a.json3").read_text()) == {"by": "acer"}
    assert env["client"].claim("pc", None, False) is None          # b is done, not handed out
    assert env["hub"].q.status()["counts"]["whisper"]["done"] == 2


def test_bad_result_is_refused_and_counts_as_a_try(env):
    (env["audio"] / "a.m4a").write_bytes(b"AUDIO")
    env["hub"].q.add("whisper", "a")
    item = env["client"].claim("pc", None, False)
    with pytest.raises(HubError) as e:
        env["client"].call("PUT", f"/items/{item['id']}/result", b"not json", lease=item["lease"], raw=True)
    assert e.value.code == 422
    assert not (env["captions"] / "a.json3").exists()
    assert env["hub"].q.get(item["id"])["state"] == "waiting"


def test_missing_input_fails_the_item_at_once(env):
    env["hub"].q.add("whisper", "gone")
    assert env["client"].claim("pc", None, False) is None
    assert env["hub"].q.status()["counts"]["whisper"]["failed"] == 1


def test_bad_keys_and_kinds_are_refused(env):
    for body in ({"kind": "whisper", "keys": ["../etc"]}, {"kind": "nope", "keys": ["a"]}):
        with pytest.raises(HubError) as e:
            env["client"].call("POST", "/items", body)
        assert e.value.code == 400


def test_fill_adds_inputs_without_results(env):
    for name in ("done.m4a", "todo.m4a", "acer.m4a", "half.m4a.part", "x.webm.ytdl"):
        (env["audio"] / name).write_bytes(b"A")
    (env["captions"] / "done.json3").write_text("{}")
    (env["captions"] / "acer.tries").write_text("1")
    assert env["hub"].fill("whisper") == 1
    assert env["hub"].fill("whisper") == 0
    assert env["hub"].q.claim("pc")["key"] == "todo"
