import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import MagicMock

import pytest

from talli_flug.input import Frame, ReceiverConfig, receive
from talli_flug.laboratory import Laboratory, RETENTION
from talli_flug.message_rate import MessageRate
from talli_flug.state import AircraftStore


RAW = "8D40621D58C382D690C8AC2863A7"
ODD = "8D40621D58C386435CC412692AD6"
RECEIVERS = (ReceiverConfig("east", "localhost"), ReceiverConfig("north", "localhost"))


def rows(path, query):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(query).fetchall()


def test_parallel_producers_never_open_sqlite(tmp_path, monkeypatch):
    path = tmp_path / "lab.db"
    original = sqlite3.connect
    owners = set()

    def connect(*args, **kwargs):
        owners.add(threading.current_thread().name)
        db = original(*args, **kwargs)
        db.set_trace_callback(lambda _: owners.add(threading.current_thread().name))
        return db

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        lab = Laboratory(path, RECEIVERS)

        def send(receiver):
            lab.connection(receiver.receiver_id, True)
            for _ in range(3000):
                lab.record(Frame(receiver.receiver_id, RAW, (), time.time(), time.monotonic()), "accepted")
            lab.connection(receiver.receiver_id, False)

        with ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(send, RECEIVERS))
        assert lab.close()
    assert owners == {"laboratory-writer"}
    assert rows(path, "SELECT receiver_id, sum(framed), sum(accepted), sum(dropped) "
                      "FROM receiver_minutes GROUP BY receiver_id ORDER BY receiver_id") == [
        ("east", 3000, 3000, 0), ("north", 3000, 3000, 0)]


def test_overflow_and_database_recovery(tmp_path, monkeypatch):
    path = tmp_path / "lab.db"
    blocked = threading.Event()
    attempted = threading.Event()
    original = Laboratory._open
    now = [0]

    def unavailable(self):
        if not blocked.is_set():
            attempted.set()
            raise sqlite3.OperationalError("injected unavailable database")
        return original(self)

    monkeypatch.setattr(Laboratory, "_open", unavailable)
    lab = Laboratory(path, RECEIVERS, 1, wall=lambda: 120, clock=lambda: now[0])
    assert attempted.wait(2)
    lab.connection("east", True)
    for offset in (1, 2, 61, 62):
        now[0] = offset
        lab.record(Frame("east", RAW, (), 0, offset), "accepted")
    now[0] = 125
    lab.connection("east", False)
    blocked.set()
    assert lab.close()
    assert rows(path, "SELECT minute_utc, framed, dropped, complete, database_gap FROM receiver_minutes "
                      "WHERE receiver_id='east' ORDER BY minute_utc") == [
        (120, 1, 1, 0, 1), (180, 0, 2, 0, 1), (240, 0, 0, 0, 1)]
    assert rows(path, "SELECT DISTINCT database_gap FROM receiver_minutes WHERE receiver_id='north'") == [(1,)]


def test_minutes_split_sessions_and_keep_offline_distinct(tmp_path):
    path = tmp_path / "lab.db"
    now = [0]
    lab = Laboratory(path, RECEIVERS, wall=lambda: 120, clock=lambda: now[0])
    for offset, online in ((10, True), (25, False), (50, True), (80, False)):
        now[0] = offset
        lab.connection("east", online)
    for offset, outcome in ((59, "accepted"), (60, "position"), (60, "not_accepted")):
        lab.record(Frame("east", RAW, (), 9999, offset), outcome)
    now[0] = 125
    assert lab.close()
    assert rows(path, "SELECT minute_utc, online_seconds, framed, accepted, cpr, no_position, complete "
                      "FROM receiver_minutes WHERE receiver_id='east' ORDER BY minute_utc") == [
        (120, 25, 1, 1, 0, 1, 1), (180, 20, 2, 1, 1, 1, 1), (240, 0, 0, 0, 0, 0, 0)]
    assert rows(path, "SELECT started_at, ended_at FROM receiver_sessions ORDER BY id") == [(130, 145), (170, 200)]
    assert rows(path, "SELECT online_seconds, framed, complete FROM receiver_minutes "
                      "WHERE receiver_id='north' ORDER BY minute_utc") == [(0, 0, 1), (0, 0, 1), (0, 0, 0)]


@pytest.mark.parametrize("enabled", [True, False])
def test_receive_preserves_live_rate_and_cpr(tmp_path, monkeypatch, enabled):
    path = tmp_path / "lab.db"
    receiver = RECEIVERS[0]
    lab = Laboratory(path, [receiver]) if enabled else None
    stop = threading.Event()
    statuses, outcomes = [], []
    sock = MagicMock()
    sock.__enter__.return_value = sock
    invalid = RAW[:-1] + "0"
    sock.recv.return_value = b"\x1a1" + bytes(9) + b"\x1a4" + bytes(9) + b"".join(
        b"\x1a3" + bytes(7) + bytes.fromhex(raw) for raw in (RAW, RAW, ODD, ODD, invalid))
    monkeypatch.setattr("talli_flug.input.socket.create_connection", lambda *args, **kwargs: sock)
    store = AircraftStore()
    rate = MessageRate(time_fn=lambda: 0)

    def consume(frame):
        result = store.update(frame)
        outcomes.append(result)
        if len(outcomes) == 5:
            stop.set()
        return result

    try:
        receive(receiver, consume, stop, statuses.append, rate, lab)
    finally:
        if lab:
            assert lab.close()
    assert statuses == [True, False]
    assert outcomes == ["accepted", "accepted", "position", "accepted", None]
    assert rate.total_rate() == 0.5
    assert store.snapshot()[0]["latitude"] is not None
    if lab:
        assert rows(path, "SELECT sum(framed), sum(accepted), sum(cpr), sum(no_position) "
                          "FROM receiver_minutes") == [(5, 4, 1, 4)]
    else:
        assert not path.exists()


def test_migration_restart_and_retention(tmp_path):
    path = tmp_path / "lab.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at REAL NOT NULL);
            INSERT INTO runs VALUES (1, 0);
            CREATE TABLE observations (id INTEGER PRIMARY KEY, raw TEXT NOT NULL);
            INSERT INTO observations VALUES (1, 'legacy');
        """)
    old = Laboratory(path, RECEIVERS, wall=lambda: 120)
    assert old.close()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE measurement_runs SET ended_at=NULL WHERE run_id=2")
        db.execute("INSERT INTO receiver_sessions(run_id, receiver_id, started_at) VALUES (2, 'east', 120)")
    lab = Laboratory(path, [ReceiverConfig("east", "localhost", chain_label="new")], wall=lambda: RETENTION + 240)
    assert lab.close()
    assert rows(path, "PRAGMA user_version") == [(1,)]
    assert rows(path, "SELECT raw FROM observations") == [("legacy",)]
    assert rows(path, "SELECT DISTINCT run_id FROM receiver_minutes") == [(3,)]
    assert rows(path, "SELECT ended_at, end_reason FROM receiver_sessions") == [(None, "interrupted")]
    assert rows(path, "SELECT chain_label FROM receivers WHERE run_id=3") == [("new",)]


def test_locked_writer_retries_without_losing_batch(tmp_path, monkeypatch):
    path = tmp_path / "lab.db"
    initialized = threading.Event()
    failed = threading.Event()
    original_start = Laboratory._start
    original_write = Laboratory._write

    def start(self, db):
        run = original_start(self, db)
        initialized.set()
        return run

    def write(self, *args):
        try:
            return original_write(self, *args)
        except sqlite3.Error:
            failed.set()
            raise

    monkeypatch.setattr(Laboratory, "_start", start)
    monkeypatch.setattr(Laboratory, "_write", write)
    lab = Laboratory(path, RECEIVERS)
    assert initialized.wait(2)
    with closing(sqlite3.connect(path)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        for _ in range(50):
            lab.record(Frame("east", RAW, (), time.time(), time.monotonic()), "accepted")
        assert failed.wait(2)
        blocker.rollback()
    assert lab.close()
    assert rows(path, "SELECT sum(framed), sum(accepted), sum(dropped), max(database_gap) "
                      "FROM receiver_minutes WHERE receiver_id='east'") == [(50, 50, 0, 1)]


def test_transition_overflow_keeps_unknown_online_time(tmp_path, monkeypatch):
    path = tmp_path / "lab.db"
    ready = threading.Event()
    original = Laboratory._open
    now = [0]

    def open_after_events(self):
        assert ready.wait(2)
        return original(self)

    monkeypatch.setattr(Laboratory, "_open", open_after_events)
    lab = Laboratory(path, RECEIVERS, 1, wall=lambda: 120, clock=lambda: now[0])
    for index in range(258):
        now[0] = index / 10
        lab.connection("east", index % 2 == 0)
    now[0] = 61
    ready.set()
    assert lab.close()
    assert rows(path, "SELECT online_seconds, dropped, complete FROM receiver_minutes "
                      "WHERE receiver_id='east' AND minute_utc=120") == [(None, 2, 0)]
