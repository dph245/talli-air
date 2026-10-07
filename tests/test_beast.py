import socket
import threading
from unittest.mock import patch

import pytest
from pyModeS.util import crc

from talli_flug.config import Config
from talli_flug.input import BeastBuffer, ReceiverConfig, receive
from talli_flug.state import AircraftStore

SHORT = bytes.fromhex('5D4840D6' + f"{crc('5D4840D6000000'):06X}")
LONG = bytes.fromhex('8D4840D6202CC371C32CE0576098')


def wire(payload=LONG, kind=0x33, timestamp=0x1a1234561a78, signal=0x1a):
    body = timestamp.to_bytes(6, 'big') + bytes([signal]) + payload
    return bytes([0x1a, kind]) + body.replace(b'\x1a', b'\x1a\x1a')


@pytest.mark.parametrize('payload,kind', [(SHORT, 0x32), (LONG, 0x33),
                                       (bytes.fromhex('8D1A1A1A1A1A1A1A1A1A1A1A1A1A'), 0x33)])
def test_every_split_and_single_bytes(payload, kind):
    data = wire(payload, kind)
    for split in range(len(data) + 1):
        parser = BeastBuffer('roof')
        frames = parser.feed(data[:split]) + parser.feed(data[split:])
        assert len(frames) == 1
        frame = frames[0]
        assert frame.raw == payload.hex().upper()
        assert (frame.receiver_id, frame.beast_timestamp, frame.signal_level, frame.beast_type) == (
            'roof', 0x1a1234561a78, 0x1a, kind)
        assert frame.received_at > 0 and frame.received_monotonic > 0
    parser = BeastBuffer('roof')
    assert sum(len(parser.feed(bytes([byte]))) for byte in data) == 1


def test_coalesced_non_modes_and_resynchronization():
    parser = BeastBuffer('roof')
    data = (b'noise' + wire(b'\x1a\x01', 0x31) + wire(SHORT, 0x32)
            + wire(b'\x1a\x00', 0x34) + wire() + b'\x1a\x99junk' + wire())
    assert [f.raw for f in parser.feed(data)] == [SHORT.hex().upper(), LONG.hex().upper(), LONG.hex().upper()]
    for cut in range(2, len(wire())):
        parser = BeastBuffer('roof')
        # A truncated escape followed by a new marker can be ambiguous on the wire;
        # a subsequent complete marker must always recover.
        frames = parser.feed(wire()[:cut] + wire() + wire())
        assert frames[-1].raw == LONG.hex().upper()
    assert BeastBuffer('roof').feed(wire(SHORT, 0x33)) == []
    assert BeastBuffer('roof').feed(wire(LONG[:7], 0x32)) == []
    parser = BeastBuffer('roof')
    assert parser.feed(b'x' * 100000 + b'\x1a3' + b'x' * 100000) == []
    assert len(parser.buffer) <= 21


def test_truncation_never_emits():
    for cut in range(len(wire())):
        assert BeastBuffer('roof').feed(wire()[:cut]) == []


def test_backoff_capped_and_interruptible():
    class Stop:
        delays = []
        def is_set(self):
            return len(self.delays) == 5
        def wait(self, delay):
            self.delays.append(delay)
    stop = Stop()
    states = []
    with patch('talli_flug.input.socket.create_connection', side_effect=OSError('offline')):
        receive(ReceiverConfig('a', 'offline', reconnect_seconds=1, reconnect_max_seconds=4),
                lambda _: pytest.fail('unexpected frame'), stop, states.append)
    assert stop.delays == [1, 2, 4, 4, 4]
    assert states == [False] * 5


def test_simultaneous_receivers_reconnect_and_provenance():
    store = AircraftStore()
    stop = threading.Event()
    first = threading.Event()
    enough = threading.Event()
    frames = []
    lock = threading.Lock()
    states = {'a': [], 'b': []}
    listeners = []
    workers = []

    def consume(frame):
        store.update(frame)
        with lock:
            frames.append(frame)
            if frame.receiver_id == 'a':
                first.set()
            if len(frames) >= 4:
                enough.set()

    try:
        for _ in range(2):
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.settimeout(5)
            listeners.append(listener)

        def sender_a():
            with listeners[0].accept()[0] as conn:
                conn.sendall(wire(SHORT, 0x32) + wire()[:12])
            with listeners[0].accept()[0] as conn:
                conn.sendall(wire(SHORT, 0x32, signal=42))
                stop.wait(5)

        def sender_b():
            with listeners[1].accept()[0] as conn:
                assert first.wait(5)
                data = wire(SHORT, 0x32, signal=99)
                conn.sendall(data[:3])
                conn.sendall(data[3:] + wire(signal=99))
                stop.wait(5)

        for target in (sender_a, sender_b):
            workers.append(threading.Thread(target=target, daemon=True))
        for index, name in enumerate(('a', 'b')):
            config = ReceiverConfig(name, '127.0.0.1', listeners[index].getsockname()[1], 0.01)
            workers.append(threading.Thread(target=receive, args=(config, consume, stop, states[name].append), daemon=True))
        for worker in workers:
            worker.start()
        assert enough.wait(5)
        rows = store.snapshot()
        assert len(rows) == 1
        row = rows[0]
        assert set(row['receiver_observations']) == {'a', 'b'}
        assert row['receiver_observations']['a']['signal_level'] == 42
        assert row['receiver_observations']['b']['signal_level'] == 99
        assert row['field_observations']['on_ground']['receiver_id'] in ('a', 'b')
        assert row['field_observations']['on_ground']['beast_timestamp'] == 0x1a1234561a78
        assert row['callsign'] == 'KLM1023'
        assert row['field_observations']['callsign']['receiver_id'] == 'b'
        assert row['field_observations']['callsign']['signal_level'] == 99
        assert states['a'].count(True) == 2
        assert states['b'] == [True]
        assert len(frames) == 4
    finally:
        stop.set()
        for worker in workers:
            worker.join(6)
        for listener in listeners:
            listener.close()
    assert all(not worker.is_alive() for worker in workers)


@pytest.mark.parametrize('value', ['[]', '{}', '[{}]', '[{"receiver_id":"a","host":"x"},{"receiver_id":"a","host":"y"}]',
    '[{"receiver_id":"a","host":"x","protocol":"auto"}]', '[{"receiver_id":"a","host":"x","port":true}]'])
def test_invalid_receivers(monkeypatch, value):
    monkeypatch.setenv('RECEIVERS', value)
    with pytest.raises(ValueError):
        Config.from_env()


def test_external_receivers(monkeypatch):
    monkeypatch.setenv('RECEIVERS', '[{"receiver_id":"roof","host":"one"},{"receiver_id":"shed","host":"two","protocol":"avr","port":47806}]')
    a, b = Config.from_env().receivers
    assert (a.receiver_id, a.port, a.protocol) == ('roof', 30005, 'beast')
    assert (b.receiver_id, b.port, b.protocol) == ('shed', 47806, 'avr')


def test_idle_connection_reconnects():
    from unittest.mock import MagicMock
    stop = threading.Event()
    sock = MagicMock()
    sock.__enter__.return_value = sock
    sock.recv.side_effect = socket.timeout
    states = []

    def status(value):
        states.append(value)
        if states.count(True) == 2:
            stop.set()

    with patch('talli_flug.input.socket.create_connection', return_value=sock), \
         patch('talli_flug.input.time.monotonic', side_effect=range(100)):
        receive(ReceiverConfig('idle', 'localhost', reconnect_seconds=0.001, idle_seconds=0.1),
                lambda _: pytest.fail('unexpected frame'), stop, status)
    assert states == [True, False, True, False]


def test_multiple_receiver_status_html():
    from talli_flug.web import render
    html = render([], '', False, receiver_status={'<roof>': True, 'shed': False})
    assert '&lt;roof&gt;: connected; shed: disconnected; retrying' in html
    assert '<roof>' not in html
