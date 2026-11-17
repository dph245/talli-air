"""Run two synthetic Beast TCP feeds, with and without measurement storage."""

import argparse
from contextlib import ExitStack, closing
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time

from talli_flug.input import ReceiverConfig, receive
from talli_flug.laboratory import Laboratory
from talli_flug.state import AircraftStore


WIRE = b"\x1a3" + bytes(7) + bytes.fromhex("8D40621D58C382D690C8AC2863A7")


def measure(seconds, rate, path):
    stop = threading.Event()
    store = AircraftStore()
    counts = [0, 0]
    high_water_sample = 0
    workers = []
    with ExitStack() as resources:
        listeners = [resources.enter_context(socket.socket()) for _ in counts]
        for listener in listeners:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(5)
        configs = [ReceiverConfig(name, "127.0.0.1", listener.getsockname()[1])
                   for name, listener in zip(("WF-Ost", "WF-Nord"), listeners)]
        lab = Laboratory(path, configs) if path else None

        def consume(index, frame):
            result = store.update(frame)
            counts[index] += 1
            return result

        for index, config in enumerate(configs):
            worker = threading.Thread(target=receive, daemon=True, args=(
                config, lambda frame, index=index: consume(index, frame), stop, lambda _: None, None, lab))
            worker.start()
            workers.append(worker)
        peers = [resources.enter_context(listener.accept()[0]) for listener in listeners]
        for peer in peers:
            peer.settimeout(5)
        start = time.monotonic()
        cpu_start = time.process_time()
        offered = 0
        while offered < seconds * rate:
            step = min(rate // 10, seconds * rate - offered)
            for peer in peers:
                peer.sendall(WIRE * step)
            offered += step
            if lab:
                high_water_sample = max(high_water_sample, lab._queue.qsize())
            time.sleep(max(0, start + offered / rate - time.monotonic()))
        deadline = time.monotonic() + 5
        while min(counts) < offered and time.monotonic() < deadline:
            time.sleep(0.01)
        stop.set()
        for peer in peers:
            peer.shutdown(socket.SHUT_WR)
        for worker in workers:
            worker.join(5)
        flush_start = time.monotonic()
        flushed = lab.close() if lab else None
        elapsed = time.monotonic() - start
        result = dict(offered_per_receiver=offered, received_per_receiver=counts,
                      elapsed_s=round(elapsed, 3), cpu_s=round(time.process_time() - cpu_start, 3),
                      sampled_queue_peak=high_water_sample, flushed=flushed,
                      flush_s=round(time.monotonic() - flush_start, 3))
    assert counts == [offered, offered]
    if lab:
        with closing(sqlite3.connect(path)) as db:
            result["persisted"] = db.execute("SELECT receiver_id, sum(framed), sum(dropped), max(database_gap) "
                                             "FROM receiver_minutes GROUP BY receiver_id ORDER BY receiver_id").fetchall()
        assert flushed and result["persisted"] == [("WF-Nord", offered, 0, 0), ("WF-Ost", offered, 0, 0)]
        result["effective_writer_frames_per_s"] = round(offered * 2 / elapsed, 1)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--rate", type=int, default=300, help="Frames/s per receiver")
    args = parser.parse_args()
    if args.seconds < 1 or args.rate < 250:
        parser.error("Supply at least one second and 250 frames/s per receiver")
    with tempfile.TemporaryDirectory() as directory:
        baseline = measure(args.seconds, args.rate, None)
        laboratory = measure(args.seconds, args.rate, Path(directory) / "lab.sqlite3")
    print(json.dumps(dict(baseline=baseline, laboratory=laboratory), indent=2))


if __name__ == "__main__":
    main()
