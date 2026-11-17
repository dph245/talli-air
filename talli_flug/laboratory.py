"""Bounded, asynchronous SQLite minute measurements."""

from collections import Counter, defaultdict, deque
import logging
import queue
import sqlite3
import threading
import time


LOG = logging.getLogger(__name__)
RETENTION = 60 * 86400
BATCH = 2048
SCHEMA_VERSION = 1


def minute(timestamp):
    return int(timestamp // 60) * 60


class Laboratory:
    def __init__(self, path, receivers, capacity=8192, *, wall=time.time, clock=time.monotonic):
        if capacity < 1:
            raise ValueError("Laboratory queue capacity must be positive")
        self._path = path
        self._receivers = {receiver.receiver_id: receiver for receiver in receivers}
        self._clock = clock
        self._origin = clock()
        self._started = wall()
        self._queue = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._loss = Counter()
        self._lost_before = {}
        self._transitions = deque()
        self._connection_limit = max(256, capacity)
        self._current = {}
        self._resync = set()
        self._inflight = {}
        self._wake = threading.Event()
        self._closing = False
        self._deadline = None
        self._flushed = False
        self._thread = threading.Thread(target=self._run, name="laboratory-writer", daemon=True)
        self._thread.start()

    def _utc(self, monotonic):
        return self._started + (monotonic - self._origin)

    def record(self, frame, outcome):
        timestamp = self._utc(frame.received_monotonic)
        mode_s = bool(frame.raw)
        event = (frame.receiver_id, minute(timestamp), mode_s, outcome)
        with self._lock:
            if self._closing:
                return
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                self._drop(frame.receiver_id, event[1])

    def begin_chunk(self, receiver_id):
        with self._lock:
            self._inflight[receiver_id] = minute(self._utc(self._clock()))

    def end_chunk(self, receiver_id):
        with self._lock:
            self._inflight.pop(receiver_id, None)

    def _drop(self, receiver_id, window):
        if window >= minute(self._utc(self._clock()) - RETENTION):
            self._loss[receiver_id, window] += 1
        else:
            self._lost_before[receiver_id] = self._lost_before.get(receiver_id, 0) + 1

    def connection(self, receiver_id, online):
        with self._lock:
            if self._closing:
                return
            timestamp = self._utc(self._clock())
            self._current[receiver_id] = (timestamp, online)
            if len(self._transitions) < self._connection_limit and receiver_id not in self._resync:
                self._transitions.append((receiver_id, timestamp, online))
            else:
                self._resync.add(receiver_id)
                self._drop(receiver_id, minute(timestamp))

    def close(self, timeout=3):
        with self._lock:
            self._closing = True
            if self._deadline is None:
                self._deadline = time.monotonic() + timeout
        self._wake.set()
        self._thread.join(timeout)
        if self._thread.is_alive():
            LOG.warning("Laboratory shutdown timed out; uncommitted measurements may be missing")
        return self._flushed

    def _open(self):
        db = sqlite3.connect(self._path, timeout=0.25)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise sqlite3.DatabaseError(f"unsupported laboratory schema {version}")
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS receivers (
                    run_id INTEGER NOT NULL, receiver_id TEXT NOT NULL, protocol TEXT NOT NULL,
                    latitude REAL, longitude REAL, terrain_height_m REAL,
                    antenna_height_agl_m REAL, chain_label TEXT,
                    PRIMARY KEY (run_id, receiver_id)
                );
                CREATE TABLE IF NOT EXISTS measurement_runs (
                    run_id INTEGER PRIMARY KEY, observed_through REAL NOT NULL, ended_at REAL
                );
                CREATE TABLE IF NOT EXISTS receiver_sessions (
                    id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, receiver_id TEXT NOT NULL,
                    started_at REAL NOT NULL, ended_at REAL, end_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS sessions_receiver_time
                    ON receiver_sessions(receiver_id, started_at);
                CREATE TABLE IF NOT EXISTS receiver_minutes (
                    run_id INTEGER NOT NULL, receiver_id TEXT NOT NULL, minute_utc INTEGER NOT NULL,
                    observed_seconds REAL NOT NULL, online_seconds REAL,
                    framed INTEGER NOT NULL, accepted INTEGER NOT NULL, cpr INTEGER NOT NULL,
                    no_position INTEGER NOT NULL, dropped INTEGER NOT NULL,
                    complete INTEGER NOT NULL, database_gap INTEGER NOT NULL,
                    PRIMARY KEY (run_id, receiver_id, minute_utc)
                );
                CREATE INDEX IF NOT EXISTS minutes_time ON receiver_minutes(minute_utc);
                CREATE INDEX IF NOT EXISTS minutes_receiver_time
                    ON receiver_minutes(receiver_id, minute_utc);
                CREATE INDEX IF NOT EXISTS minutes_pending
                    ON receiver_minutes(run_id, minute_utc)
                    WHERE complete=0 AND dropped=0 AND database_gap=0 AND online_seconds IS NOT NULL;
                PRAGMA user_version=1;
                COMMIT;
            """)
        except BaseException:
            db.close()
            raise
        return db

    def _start(self, db):
        with db:
            db.execute("""
                UPDATE receiver_sessions SET end_reason='interrupted'
                WHERE ended_at IS NULL AND end_reason IS NULL
            """)
            run = db.execute("INSERT INTO runs(started_at) VALUES (?)", (self._started,)).lastrowid
            db.execute("INSERT INTO measurement_runs VALUES (?, ?, NULL)", (run, self._started))
            db.executemany("INSERT INTO receivers VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                (run, receiver.receiver_id, receiver.protocol, receiver.latitude, receiver.longitude,
                 receiver.terrain_height_m, receiver.antenna_height_agl_m, receiver.chain_label)
                for receiver in self._receivers.values()
            ))
        return run

    def _take(self):
        with self._lock:
            now = self._utc(self._clock())
            transitions = self._transitions
            self._transitions = deque()
            loss = self._loss
            self._loss = Counter()
            resync = {receiver: self._current[receiver] for receiver in self._resync}
            self._resync.clear()
            closing = self._closing
            watermark = min(self._inflight.values(), default=minute(now))
        frames = []
        for _ in range(BATCH):
            try:
                frames.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return now, transitions, loss, resync, frames, watermark, closing and self._queue.empty()

    def _write(self, db, run, cursor, online, batch, failed_since, prune):
        now, transitions, loss, resync, frames, watermark, closing = batch
        cutoff = minute(now - RETENTION)
        rows = defaultdict(lambda: [0., 0., 0, 0, 0, 0, 0, 0])

        def coverage(receiver, start, end, is_online, unknown=False):
            start = max(start, cutoff)
            for window in range(minute(start), minute(end) + 60, 60):
                seconds = min(end, window + 60) - max(start, window)
                if seconds <= 0:
                    continue
                row = rows[receiver, window]
                row[0] += seconds
                if unknown:
                    row[1] = None
                elif is_online and row[1] is not None:
                    row[1] += seconds

        active = online.copy()
        cursors = dict.fromkeys(self._receivers, cursor)
        with db:
            for receiver, timestamp, connected in transitions:
                coverage(receiver, cursors[receiver], timestamp, receiver in active)
                cursors[receiver] = timestamp
                if connected:
                    active[receiver] = db.execute(
                        "INSERT INTO receiver_sessions(run_id, receiver_id, started_at) VALUES (?, ?, ?)",
                        (run, receiver, timestamp)).lastrowid
                elif receiver in active:
                    db.execute("UPDATE receiver_sessions SET ended_at=?, end_reason='disconnect' WHERE id=?",
                               (timestamp, active.pop(receiver)))
            for receiver, (timestamp, connected) in resync.items():
                coverage(receiver, cursors[receiver], timestamp, False, unknown=True)
                cursors[receiver] = timestamp
                if receiver in active:
                    db.execute("UPDATE receiver_sessions SET end_reason='events_lost' WHERE id=?",
                               (active.pop(receiver),))
                if connected:
                    active[receiver] = db.execute(
                        "INSERT INTO receiver_sessions(run_id, receiver_id, started_at) VALUES (?, ?, ?)",
                        (run, receiver, timestamp)).lastrowid
            for receiver, start in cursors.items():
                coverage(receiver, start, now, receiver in active)
            for receiver, window, mode_s, outcome in frames:
                if not mode_s or window < cutoff:
                    continue
                row = rows[receiver, window]
                row[2] += 1
                row[3] += outcome in ("accepted", "position")
                row[4] += outcome == "position"
                row[5] += outcome != "position"
            for (receiver, window), count in loss.items():
                if window >= cutoff:
                    rows[receiver, window][6] += count
            if failed_since is not None:
                gap_end = minute(self._utc(self._clock()))
                for receiver in self._receivers:
                    for window in range(minute(max(cutoff, failed_since)), gap_end + 60, 60):
                        rows[receiver, window][7] = 1
            db.executemany("""
                INSERT INTO receiver_minutes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(run_id, receiver_id, minute_utc) DO UPDATE SET
                    observed_seconds=observed_seconds+excluded.observed_seconds,
                    online_seconds=online_seconds+excluded.online_seconds,
                    framed=framed+excluded.framed, accepted=accepted+excluded.accepted,
                    cpr=cpr+excluded.cpr, no_position=no_position+excluded.no_position,
                    dropped=dropped+excluded.dropped, complete=0,
                    database_gap=max(database_gap, excluded.database_gap)
            """, ((run, receiver, window, *row) for (receiver, window), row in rows.items()))
            if self._queue.empty():
                db.execute("""
                    UPDATE receiver_minutes SET complete=1
                    WHERE run_id=? AND minute_utc<? AND complete=0 AND observed_seconds>=59.999999
                    AND online_seconds IS NOT NULL AND dropped=0 AND database_gap=0
                """, (run, watermark))
            if closing:
                db.execute("UPDATE receiver_sessions SET end_reason='shutdown_unknown' "
                           "WHERE run_id=? AND ended_at IS NULL AND end_reason IS NULL", (run,))
            db.execute("UPDATE measurement_runs SET observed_through=?, ended_at=? WHERE run_id=?",
                       (now, now if closing else None, run))
            if prune:
                db.execute("DELETE FROM receiver_minutes WHERE rowid IN ("
                           "SELECT rowid FROM receiver_minutes WHERE minute_utc<? LIMIT ?)", (cutoff, BATCH))
        return active

    def _run(self):
        db = None
        run = None
        cursor = self._started
        online = {}
        batch = None
        failed_since = None
        next_prune = cursor
        try:
            while True:
                if self._deadline is not None and time.monotonic() >= self._deadline:
                    LOG.warning("Laboratory flush deadline reached; uncommitted measurements may be missing")
                    return
                try:
                    if db is None:
                        db = self._open()
                    if run is None:
                        run = self._start(db)
                    if batch is None:
                        batch = self._take()
                    now, *_, closing = batch
                    online = self._write(db, run, cursor, online, batch, failed_since, now >= next_prune)
                    if now >= next_prune:
                        next_prune = now + 1
                    cursor = now
                    batch = None
                    if failed_since is not None:
                        LOG.info("Laboratory database recovered")
                        failed_since = None
                    if closing:
                        if self._lost_before:
                            LOG.warning("Laboratory losses outside retention: %s", self._lost_before)
                        self._flushed = True
                        return
                except sqlite3.Error as exc:
                    if failed_since is None:
                        failed_since = cursor
                    LOG.warning("Laboratory database write failed; retrying: %s", exc)
                    if self._closing:
                        time.sleep(0.05)
                    else:
                        self._wake.wait(1)
                    continue
                if self._queue.empty() and not self._closing:
                    self._wake.wait(0.2)
        finally:
            if db is not None:
                db.close()
