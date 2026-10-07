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
    connected = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())

    server = make_server((config.web_host, config.web_port), store,
                         config.receiver.receiver_id, connected.is_set, metadata, config.map_center)
    web = threading.Thread(target=server.serve_forever, daemon=True)
    receiver = threading.Thread(
        target=receive,
        args=(config.receiver, store.update, stop,
              lambda value: connected.set() if value else connected.clear()),
        daemon=True,
    )
    web.start()
    receiver.start()
    logging.info("Talli-Flug listening on %s:%s", config.web_host, config.web_port)
    try:
        while not stop.wait(1):
            store.expire()
            if not receiver.is_alive() or not web.is_alive():
                raise RuntimeError("Application worker stopped unexpectedly")
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        web.join(timeout=2)
        receiver.join(timeout=6)


if __name__ == "__main__":
    main()
