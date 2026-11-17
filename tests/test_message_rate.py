import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlopen

import pytest

from talli_flug.message_rate import MessageRate
from talli_flug.web import make_server
from talli_flug.state import Aircraft, AircraftStore


def test_message_rate_basic_and_window_expiry():
    # deterministic monotonic clock
    t = [0.0]

    def now():
        return t[0]

    mr = MessageRate(window_seconds=10.0, time_fn=now)
    mr.increment("rx1")  # bucket 0
    t[0] = 5.0
    mr.increment("rx1")  # bucket 5
    mr.increment("rx2")  # bucket 5
    # snapshot at t=5 (bucket 5). Window is defined as (now-window, now] →
    # ( -5, 5] which includes the bucket at t=0 and the bucket at t=5.
    snap = mr.snapshot()
    assert snap["rx1"] == 2 / 10.0
    assert snap["rx2"] == 1 / 10.0
    assert mr.total_rate() == 3 / 10.0

    # advance time beyond the lower bound of the window – bucket at t=0 expires
    t[0] = 15.0  # now bucket 15, window (5,15]; bucket at 5 is *excluded* (lower bound exclusive)
    snap2 = mr.snapshot()
    # all previous buckets have fallen outside the window, so the snapshot is empty
    assert snap2 == {}
    assert mr.total_rate() == 0.0


def test_concurrent_increments():
    rates = MessageRate(time_fn=lambda: 42)

    def record(receiver):
        for _ in range(1000):
            rates.increment(receiver)

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(record, ("roof", "roof", "shed", "shed")))
    assert rates.snapshot() == {"roof": 200, "shed": 200}


def test_stats_endpoint_integration():
    store = AircraftStore(ttl=600)
    for icao, latitude, longitude in (("ABC123", 0, 0), ("ABC124", 91, 0), ("ABC125", None, 0)):
        store.aircraft[icao] = Aircraft(icao, latitude=latitude, longitude=longitude, updated=time.monotonic())
    now = [100.0]
    rates = MessageRate(time_fn=lambda: now[0])
    rates.increment("rxA")
    rates.increment("rxA")
    rates.increment("rxB")
    statuses = {"rxA": True, "rxB": False, "silent": False}
    server = make_server(("127.0.0.1", 0), store, "rxA", lambda: False, None,
                         receiver_status=lambda: statuses, message_rate=rates)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(base + "/api/stats") as resp:
            assert resp.headers.get_content_type() == "application/json"
            data = json.load(resp)
        assert data == {
            "aircraft_count": 3, "positioned_aircraft_count": 1, "receiver_count": 3,
            "receivers_online": 1, "message_rate_window_seconds": 10, "messages_per_second": pytest.approx(0.3),
            "receivers": [
                {"receiver_id": "rxA", "connected": True, "messages_per_second": 0.2},
                {"receiver_id": "rxB", "connected": False, "messages_per_second": 0.1},
                {"receiver_id": "silent", "connected": False, "messages_per_second": 0.0},
            ],
        }
        with urlopen(base + "/") as resp:
            assert 'id="kpi-msg">0.3<' in resp.read().decode()
        now[0] += 10
        with urlopen(base + "/api/stats") as resp:
            expired = json.load(resp)
        assert expired["messages_per_second"] == 0
        assert expired["receivers"] == [dict(receiver, messages_per_second=0) for receiver in data["receivers"]]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
