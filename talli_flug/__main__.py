import logging
import signal
import threading
import zipfile

from .config import Config
from .input import receive
from .state import AircraftStore
from .web import make_server
from .metadata import LocalAircraftMetadata


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config.from_env()
    store = AircraftStore(config.aircraft_ttl, config.surface_ref)
    metadata = LocalAircraftMetadata()
    if config.aircraft_metadata_path:
        try:
            metadata = LocalAircraftMetadata.load(config.aircraft_metadata_path)
            logging.info("Loaded %s external aircraft metadata records", len(metadata))
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            logging.warning("Aircraft metadata unavailable: %s", exc)
    stop = threading.Event()
    connections = {receiver.receiver_id: threading.Event() for receiver in config.receivers}
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())

    server = make_server((config.web_host, config.web_port), store,
                         "", lambda: False, metadata, config.map_center,
                         receiver_status=lambda: {key: event.is_set() for key, event in connections.items()})
    web = threading.Thread(target=server.serve_forever, daemon=True)
    receivers = []
    for receiver_config in config.receivers:
        connected = connections[receiver_config.receiver_id]
        worker = threading.Thread(
            target=receive,
            args=(receiver_config, store.update, stop,
                  lambda value, event=connected: event.set() if value else event.clear()),
            name=f"receiver-{receiver_config.receiver_id}", daemon=True,
        )
        receivers.append(worker)
    web.start()
    for worker in receivers:
        worker.start()
    logging.info("Talli-Flug listening on %s:%s", config.web_host, config.web_port)
    try:
        while not stop.wait(1):
            store.expire()
            if not web.is_alive():
                raise RuntimeError("Application worker stopped unexpectedly")
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        web.join(timeout=2)
        for worker in receivers:
            worker.join(timeout=6)


if __name__ == "__main__":
    main()
