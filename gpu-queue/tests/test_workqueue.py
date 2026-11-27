"""The queue's rules: one lease per item, a lease that runs out puts the item back, three tries then failed, a stop
(release) not counted, and nobody but the lease holder finishes an item. Run: python -m pytest -q gpu-queue/tests"""
import pytest

from workqueue import LeaseLost, Queue


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def q(tmp_path):
    clock = Clock()
    queue = Queue(str(tmp_path / "q.db"), max_attempts=3, lease_seconds=600, clock=clock)
    queue.clock = clock
    yield queue
    queue.close()


def test_add_is_once_per_key(q):
    assert q.add("whisper", "a") is True
    assert q.add("whisper", "a") is False
    assert q.add("other", "a") is True
    assert q.status()["counts"]["whisper"]["waiting"] == 1


def test_claim_order_and_one_lease_per_item(q):
    for k in "abc":
        q.add("whisper", k)
    first = q.claim("acer")
    last = q.claim("pc", newest_first=True)
    assert (first["key"], last["key"]) == ("a", "c")
    assert q.claim("pc")["key"] == "b"
    assert q.claim("pc") is None
    assert first["lease"] != last["lease"]


def test_claim_by_kind(q):
    q.add("whisper", "a")
    q.add("speakers", "b")
    assert q.claim("pc", kinds=["speakers"])["key"] == "b"
    assert q.claim("pc", kinds=["speakers"]) is None


def test_done_needs_the_lease(q):
    q.add("whisper", "a")
    item = q.claim("pc")
    with pytest.raises(LeaseLost):
        q.done(item["id"], "someone else's")
    q.done(item["id"], item["lease"])
    assert q.get(item["id"])["state"] == "done"
    with pytest.raises(LeaseLost):            # finished: a second done is refused
        q.done(item["id"], item["lease"])
    assert q.status()["last_done"] == q.clock.t


def test_lease_runs_out_and_the_item_goes_back(q):
    """The PC went to sleep halfway: after the lease the item waits again, and the old lease can't finish it."""
    q.add("whisper", "a")
    item = q.claim("pc")
    q.clock.t += 601
    again = q.claim("acer")
    assert again["key"] == "a" and again["attempt"] == 2
    with pytest.raises(LeaseLost):
        q.done(item["id"], item["lease"])
    q.done(again["id"], again["lease"])


def test_heartbeat_keeps_the_lease(q):
    q.add("whisper", "a")
    item = q.claim("pc")
    q.clock.t += 500
    q.heartbeat(item["id"], item["lease"])
    q.clock.t += 500
    assert q.claim("acer") is None
    q.done(item["id"], item["lease"])


def test_three_tries_then_failed(q):
    q.add("whisper", "a")
    for n in (1, 2):
        item = q.claim("pc")
        assert q.fail(item["id"], item["lease"], "exit 1") == "waiting"
    item = q.claim("pc")
    q.clock.t += 601                          # the third try dies without a word
    assert q.claim("pc") is None
    row = q.get(item["id"])
    assert row["state"] == "failed" and "lease ran out" in row["error"]
    assert q.retry("whisper", "a") is True
    assert q.claim("pc")["attempt"] == 1


def test_release_is_not_a_try(q):
    q.add("whisper", "a")
    for _ in range(5):
        item = q.claim("pc")
        q.release(item["id"], item["lease"], "a game started")
    assert q.get(item["id"])["attempts"] == 0
    assert q.get(item["id"])["error"] == "released: a game started"


def test_fail_final(q):
    q.add("whisper", "a")
    item = q.claim("pc")
    assert q.fail(item["id"], item["lease"], "no input file", final=True) == "failed"


def test_status_for_the_probe(q):
    q.add("whisper", "a")
    q.clock.t += 3600
    q.add("whisper", "b")
    item = q.claim("pc")
    st = q.status()
    assert st["oldest_waiting_s"] == 0         # a is leased, b was just added
    assert st["leases"][0]["worker"] == "pc" and st["leases"][0]["key"] == item["key"]
    assert st["counts"]["whisper"] == {"waiting": 1, "leased": 1, "done": 0, "failed": 0}
