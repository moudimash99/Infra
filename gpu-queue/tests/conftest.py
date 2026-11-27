import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import Hub, make_server  # noqa: E402
from worker import Client  # noqa: E402
from workqueue import Queue  # noqa: E402

TOKEN = "t0ken"


@pytest.fixture
def env(tmp_path):
    """A hub on a free local port, with bg-taste-match's layout: audio/<id>.m4a in, captions/<id>.json3 out."""
    audio, captions = tmp_path / "audio", tmp_path / "captions"
    audio.mkdir()
    captions.mkdir()
    kinds = {"whisper": {"input_dir": str(audio), "result_dir": str(captions), "result_ext": ".json3",
                         "check": "json", "skip_ext": [".tries"]}}
    hub = Hub(Queue(str(tmp_path / "q.db")), kinds)
    server = make_server(hub, TOKEN, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    yield {"hub": hub, "url": url, "client": Client(url, TOKEN, timeout=10), "audio": audio, "captions": captions,
           "tmp": tmp_path}
    server.shutdown()
    server.server_close()
    hub.q.close()
