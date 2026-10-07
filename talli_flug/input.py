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

    def __init__(self, receiver_id: str):
        self.receiver_id = receiver_id
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

    def __post_init__(self):
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


def receive(config: ReceiverConfig, consume: Callable[[Frame], None],
            stop: threading.Event, status: Callable[[bool], None]) -> None:
    delay = config.reconnect_seconds
    while not stop.is_set():
        try:
            with socket.create_connection((config.host, config.port), timeout=5) as sock:
                sock.settimeout(min(1, config.idle_seconds))
                buffer = BeastBuffer(config.receiver_id) if config.protocol == "beast" else LineBuffer()
                connected_at = time.monotonic()
                last_data = time.monotonic()
                status(True)
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
                    for item in buffer.feed(chunk):
                        frame = item if config.protocol == "beast" else parse_line(item, config.receiver_id)
                        if frame is not None:
                            consume(frame)
        except OSError as exc:
            LOG.warning("Receiver %s disconnected: %s", config.receiver_id, exc)
        finally:
            status(False)
        stop.wait(delay)
        delay = min(config.reconnect_max_seconds, delay * 2)
