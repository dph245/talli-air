import json
import socket
import threading
from urllib.request import urlopen

import pytest

from talli_flug.config import Config
from talli_flug.input import ReceiverConfig, receive
from talli_flug.state import Aircraft, AircraftStore
from talli_flug.metadata import AircraftMetadata, LocalAircraftMetadata
from talli_flug.web import make_server, render

SAMPLE = b"*8D440DA5F82300030049B8930905;FE3418B8;06;057A;\r\n"


def test_tcp_reconnect_and_web():
    store = AircraftStore()
    stop = threading.Event()
    enough = threading.Event()
    received = []
    states = []

    def consume(frame):
        received.append(frame)
        store.update(frame)
        if len(received) == 2:
            enough.set()

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(5)

        def send():
            with listener.accept()[0] as connection:
                connection.sendall(SAMPLE[:10])
                connection.sendall(SAMPLE[10:] + b"*incomplete")
            with listener.accept()[0] as connection:
                connection.sendall(SAMPLE)

        sender = threading.Thread(target=send, daemon=True)
        reader = threading.Thread(target=receive, args=(
            ReceiverConfig("test-rx", "127.0.0.1", listener.getsockname()[1], 0.01, protocol="avr"),
            consume, stop, states.append), daemon=True)
        sender.start()
        reader.start()
        try:
            assert enough.wait(5)
        finally:
            stop.set()
            reader.join(2)
            sender.join(2)
        assert not reader.is_alive()
    assert len(received) == 2
    assert states.count(True) >= 2
    assert all(f.receiver_id == "test-rx" for f in received)

    metadata = LocalAircraftMetadata({"440DA5": AircraftMetadata("OE-IDS", "A320")}, {
        "name": "Test metadata", "url": "https://example.com", "license": "Test license",
        "license_url": "https://example.com/license", "revision": "test", "downloaded_at": "2026-10-06",
    })
    server = make_server(("127.0.0.1", 0), store, "test-rx", lambda: False, metadata)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/") as response:
            html = response.read().decode()
            assert "Talli-Flug" in html and "440DA5" in html
            assert "OE-IDS" in html and "Registration</th>" in html
            assert "disconnected" in html and "FE3418B8" in html
            assert 'id="map"' in html and 'Latest DF</th>' in html
            assert 'Latest raw frame</th>' in html
        with urlopen(base + "/static/aircraft.js") as response:
            assert response.headers["Content-Type"].startswith("text/javascript")
            assert "tile.openstreetmap.org" in response.read().decode()
        with urlopen(base + "/api/aircraft") as response:
            row, = json.load(response)
            assert row["receiver_id"] == "test-rx"
            assert row["altitude"] is None
            assert row["receiver_metadata"] == ["FE3418B8", "06", "057A"]
            assert row["external_metadata"]["registration"] == "OE-IDS"
            assert "registration" not in row["field_observations"]
        with urlopen(base + "/healthz") as response:
            assert response.read() == b"ok\n"
        with urlopen(base + "/aircraft/440DA5") as response:
            details = response.read().decode()
            assert "Decoded reports" in details and "Calculated estimates" in details
            assert "Reported temperature" in details and "Derived temperature" in details
            assert "External aircraft metadata" in details and "OE-IDS" in details
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def test_html_escaping():
    html = render([], '<script>alert("receiver")</script>', True)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize("name,value", [
    ("RECEIVER_HOST", ""), ("RECEIVER_ID", ""), ("RECEIVER_PORT", "65536"),
    ("AIRCRAFT_TTL_SECONDS", "0"), ("RECONNECT_SECONDS", "nan"),
    ("RECEIVER_IDLE_SECONDS", "inf"), ("SURFACE_LAT", "42"),
    ("MAP_LAT", "nan"), ("MAP_LAT", "91"), ("MAP_LON", "181"),
])
def test_invalid_config(monkeypatch, name, value):
    monkeypatch.setenv("RECEIVER_HOST", "receiver.test")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Config.from_env()


def test_configurable_map_center(monkeypatch):
    monkeypatch.setenv("RECEIVER_HOST", "receiver.test")
    monkeypatch.setenv("MAP_LAT", "0")
    monkeypatch.setenv("MAP_LON", "-25.5")
    assert Config.from_env().map_center == (0, -25.5)


def test_map_data_cannot_close_script_element():
    import time
    store = AircraftStore()
    store.aircraft["ABC123"] = Aircraft("ABC123", callsign="</script><script>alert(1)</script>",
                                       updated=time.monotonic())
    html = render(store.snapshot(), "test", True, map_center=(0, 0))
    data = html.split('<script id="map-data" type="application/json">', 1)[1].split('</script>', 1)[0]
    assert '<' not in data
    parsed = json.loads(data)
    assert parsed["center"] == [0, 0]
    assert parsed["aircraft"][0]["callsign"] == "</script><script>alert(1)</script>"
