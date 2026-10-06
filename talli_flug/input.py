"""Line framing and reconnecting TCP input, independent of the decoder."""

from dataclasses import dataclass
import logging
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


@dataclass(frozen=True)
class ReceiverConfig:
    receiver_id: str
    host: str
    port: int
    reconnect_seconds: float = 5
    idle_seconds: float = 60


def receive(config: ReceiverConfig, consume: Callable[[Frame], None],
            stop: threading.Event, status: Callable[[bool], None]) -> None:
    while not stop.is_set():
        try:
            with socket.create_connection((config.host, config.port), timeout=5) as sock:
                sock.settimeout(min(1, config.idle_seconds))
                buffer = LineBuffer()
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
                    for line in buffer.feed(chunk):
                        frame = parse_line(line, config.receiver_id)
                        if frame is not None:
                            consume(frame)
        except OSError as exc:
            LOG.warning("Receiver %s disconnected: %s", config.receiver_id, exc)
        finally:
            status(False)
        stop.wait(config.reconnect_seconds)
