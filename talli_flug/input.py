"""Streaming Beast/optional AVR framing and independent reconnecting TCP input."""

from dataclasses import dataclass
import logging
import math
import re
import socket
import threading
import time
from collections.abc import Callable

LOG = logging.getLogger(__name__)
MAX_LINE = 4096
FRAME = re.compile(r"\*([0-9a-fA-F]{14}|[0-9a-fA-F]{28});(.*)")


@dataclass(frozen=True)
class Frame:
    receiver_id: str
    raw: str
    metadata: tuple[str, ...]
    received_at: float
    received_monotonic: float
    beast_timestamp: int | None = None
    signal_level: int | None = None
    beast_type: int | None = None


def parse_line(line: bytes, receiver_id: str) -> Frame | None:
    """Preserve metadata fields verbatim; only normalize frame hex to uppercase."""
    if not receiver_id or len(line) > MAX_LINE:
        return None
    try:
        text = line.decode("ascii").rstrip("\r\n")
    except UnicodeDecodeError:
        return None
    if any(ord(c) < 32 or ord(c) > 126 for c in text):
        return None
    match = FRAME.fullmatch(text)
    if not match:
        return None
    raw, tail = match.groups()
    # A nonempty metadata section must also have its final semicolon.
    if tail and not tail.endswith(";"):
        return None
    df = int(raw[:2], 16) >> 3
    if len(raw) != (28 if df >= 16 else 14):
        return None
    metadata = tuple(tail[:-1].split(";")) if tail else ()
    return Frame(receiver_id, raw.upper(), metadata, time.time(), time.monotonic())


class LineBuffer:
    """Bounded LF/CRLF/CR framing across arbitrary TCP chunks.

    An oversized record is discarded through the next line delimiter.
    Incomplete records are never carried across TCP connections.
    """

    def __init__(self):
        self.buffer = bytearray()
        self.discarding = False

    def feed(self, chunk: bytes) -> list[bytes]:
        lines = []
        for byte in chunk:
            if byte in (10, 13):
                if self.buffer and not self.discarding:
                    lines.append(bytes(self.buffer))
                self.buffer.clear()
                self.discarding = False
            elif not self.discarding:
                self.buffer.append(byte)
                if len(self.buffer) > MAX_LINE:
                    self.buffer.clear()
                    self.discarding = True
        return lines


class BeastBuffer:
    """Bounded escaped-binary framer; a new marker resynchronizes damaged input.

    Types 1 (Mode A/C) and 4 (status) are framed but do not enter Mode-S decoding.
    Timestamps remain unsigned receiver-local 48-bit values, not wall-clock time.
    """

    LENGTHS = {0x31: 9, 0x32: 14, 0x33: 21, 0x34: 9}

    def __init__(self, receiver_id: str, laboratory=False):
        self.receiver_id = receiver_id
        self.laboratory = laboratory
        self.buffer = bytearray()
        self.kind = None
        self.escape = False

    def feed(self, chunk: bytes) -> list[Frame]:
        frames = []
        for byte in chunk:
            if self.escape:
                self.escape = False
                if byte != 0x1a:
                    self.buffer.clear()
                    self.kind = byte if byte in self.LENGTHS else None
                    continue
                # A doubled escape is data only inside an active frame.
            elif byte == 0x1a:
                self.escape = True
                continue
            if self.kind is None:
                continue
            self.buffer.append(byte)
            if len(self.buffer) == self.LENGTHS[self.kind]:
                data = bytes(self.buffer)
                if self.laboratory and self.kind in (0x31, 0x34):
                    frames.append(Frame(self.receiver_id, "", (), time.time(), time.monotonic(),
                                        int.from_bytes(data[:6], "big") if self.kind == 0x31 else None,
                                        data[6] if self.kind == 0x31 else None, self.kind))
                if self.kind in (0x32, 0x33) and self.receiver_id:
                    payload = data[7:]
                    if len(payload) == (14 if payload[0] >> 3 >= 16 else 7):
                        frames.append(Frame(
                            self.receiver_id, payload.hex().upper(), (),
                            time.time(), time.monotonic(),
                            int.from_bytes(data[:6], "big"), data[6], self.kind))
                self.buffer.clear()
                self.kind = None
        return frames


@dataclass(frozen=True)
class ReceiverConfig:
    receiver_id: str
    host: str
    port: int = 30005
    reconnect_seconds: float = 5
    idle_seconds: float = 60
    protocol: str = "beast"
    reconnect_max_seconds: float = 60
    latitude: float | None = None
    longitude: float | None = None
    terrain_height_m: float | None = None
    antenna_height_agl_m: float | None = None
    chain_label: str | None = None

    def __post_init__(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Set both receiver latitude and longitude")
        for value, low, high in ((self.latitude, -90, 90), (self.longitude, -180, 180),
                                 (self.terrain_height_m, -math.inf, math.inf),
                                 (self.antenna_height_agl_m, 0, math.inf)):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)
                                      or not low <= value <= high):
                raise ValueError("Invalid receiver location")
        if self.chain_label is not None and not isinstance(self.chain_label, str):
            raise ValueError("Invalid receiver chain label")
        if not isinstance(self.receiver_id, str) or not self.receiver_id.strip():
            raise ValueError("receiver_id must be nonempty")
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("receiver host must be nonempty")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("receiver port must be between 1 and 65535")
        if self.protocol not in ("beast", "avr"):
            raise ValueError("receiver protocol must be beast or avr")
        for value in (self.reconnect_seconds, self.idle_seconds, self.reconnect_max_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("receiver timeouts must be finite positive numbers")
        if self.reconnect_max_seconds < self.reconnect_seconds:
            raise ValueError("maximum reconnect delay must be at least the initial delay")


def receive(config: ReceiverConfig, consume: Callable[[Frame], str | None],
            stop: threading.Event, status: Callable[[bool], None],
            message_rate=None, lab=None) -> None:
    delay = config.reconnect_seconds
    while not stop.is_set():
        online = False
        try:
            with socket.create_connection((config.host, config.port), timeout=5) as sock:
                sock.settimeout(min(1, config.idle_seconds))
                buffer = BeastBuffer(config.receiver_id, lab is not None) if config.protocol == "beast" else LineBuffer()
                connected_at = time.monotonic()
                last_data = time.monotonic()
                status(True)
                online = True
                if lab is not None:
                    lab.connection(config.receiver_id, True)
                LOG.info("Receiver %s connected", config.receiver_id)
                while not stop.is_set():
                    try:
                        chunk = sock.recv(16384)
                    except socket.timeout:
                        if time.monotonic() - last_data >= config.idle_seconds:
                            raise TimeoutError("receiver idle timeout")
                        continue
                    if not chunk:
                        raise ConnectionError("receiver closed connection")
                    last_data = time.monotonic()
                    # Reset after a stable connection, not a connect/close loop.
                    if last_data - connected_at >= config.idle_seconds:
                        delay = config.reconnect_seconds
                    if lab is not None:
                        lab.begin_chunk(config.receiver_id)
                    try:
                        for item in buffer.feed(chunk):
                            frame = item if config.protocol == "beast" else parse_line(item, config.receiver_id)
                            if frame is None or not frame.raw:
                                continue
                            if message_rate is not None:
                                message_rate.increment(config.receiver_id)
                            result = "processing_error"
                            try:
                                result = consume(frame)
                            finally:
                                if lab is not None:
                                    lab.record(frame, result or "not_accepted")
                    finally:
                        if lab is not None:
                            lab.end_chunk(config.receiver_id)
        except OSError as exc:
            LOG.warning("Receiver %s disconnected: %s", config.receiver_id, exc)
        finally:
            status(False)
            if online and lab is not None:
                lab.connection(config.receiver_id, False)
        stop.wait(delay)
        delay = min(config.reconnect_max_seconds, delay * 2)
