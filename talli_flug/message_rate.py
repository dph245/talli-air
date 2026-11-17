"""Thread-safe per-receiver frame rates using monotonic one-second buckets."""

import threading
import time


class MessageRate:
    """Average over the current bucket and the preceding window minus one buckets.

    The window is measured in whole seconds, with up to one second of
    quantization. Bucket counts, not individual frames, are retained.
    """

    def __init__(self, window_seconds: float = 10.0, time_fn=time.monotonic):
        self.window = max(1, int(window_seconds))
        self._now = time_fn
        self._lock = threading.Lock()
        self._counts: dict[str, dict[int, int]] = {}

    def _prune(self, buckets: dict[int, int], now: int) -> None:
        cutoff = now - self.window
        while buckets:
            oldest = next(iter(buckets))
            if oldest > cutoff:
                break
            del buckets[oldest]

    def increment(self, receiver_id: str) -> None:
        with self._lock:
            now = int(self._now())
            buckets = self._counts.setdefault(receiver_id, {})
            self._prune(buckets, now)
            buckets[now] = buckets.get(now, 0) + 1

    def _snapshot_counts(self) -> dict[str, int]:
        with self._lock:
            now = int(self._now())
            counts = {}
            for receiver_id, buckets in list(self._counts.items()):
                self._prune(buckets, now)
                if buckets:
                    counts[receiver_id] = sum(buckets.values())
                else:
                    del self._counts[receiver_id]
            return counts

    def snapshot(self) -> dict[str, float]:
        return {name: count / self.window for name, count in self._snapshot_counts().items()}

    def total_rate(self) -> float:
        return sum(self._snapshot_counts().values()) / self.window
