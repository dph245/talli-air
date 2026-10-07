from dataclasses import dataclass
import math
import json
import os

from .input import ReceiverConfig
from .metadata import DEFAULT_DATABASE


def positive(name: str, default: str) -> float:
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return value


def port(name: str, default: str) -> int:
    value = int(os.environ.get(name, default))
    if not 1 <= value <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return value


@dataclass(frozen=True)
class Config:
    receivers: tuple[ReceiverConfig, ...]
    aircraft_ttl: float
    web_host: str
    web_port: int
    surface_ref: tuple[float, float] | None
    aircraft_metadata_path: str = str(DEFAULT_DATABASE)
    map_center: tuple[float, float] = (51.0, 10.0)

    @classmethod
    def from_env(cls):
        reconnect = positive("RECONNECT_SECONDS", "5")
        idle = positive("RECEIVER_IDLE_SECONDS", "60")
        maximum = positive("RECONNECT_MAX_SECONDS", "60")
        raw = os.environ.get("RECEIVERS", "").strip()
        if raw:
            entries = json.loads(raw)
            if not isinstance(entries, list) or not entries:
                raise ValueError("RECEIVERS must be a nonempty JSON array")
        else:
            entries = [{"receiver_id": os.environ.get("RECEIVER_ID", "receiver-1").strip(),
                        "host": os.environ.get("RECEIVER_HOST", "").strip(),
                        "port": port("RECEIVER_PORT", "30005"),
                        "protocol": os.environ.get("RECEIVER_PROTOCOL", "beast")}]
        receivers = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) - {"receiver_id", "host", "port", "protocol"}:
                raise ValueError("Invalid RECEIVERS entry")
            if not {"receiver_id", "host"} <= set(entry):
                raise ValueError("Every receiver needs an explicit receiver_id and host")
            receivers.append(ReceiverConfig(**entry, reconnect_seconds=reconnect,
                                             idle_seconds=idle, reconnect_max_seconds=maximum))
        if len({receiver.receiver_id for receiver in receivers}) != len(receivers):
            raise ValueError("Receiver IDs must be unique")
        lat, lon = os.environ.get("SURFACE_LAT") or None, os.environ.get("SURFACE_LON") or None
        ref = None
        if lat is not None or lon is not None:
            if lat is None or lon is None:
                raise ValueError("Set both SURFACE_LAT and SURFACE_LON")
            ref = (float(lat), float(lon))
            if not (-90 <= ref[0] <= 90 and -180 <= ref[1] <= 180):
                raise ValueError("Invalid surface reference coordinates")
        map_center = (float(os.environ.get("MAP_LAT", "51")),
                      float(os.environ.get("MAP_LON", "10")))
        if not (-90 <= map_center[0] <= 90 and -180 <= map_center[1] <= 180):
            raise ValueError("Invalid map center coordinates")
        return cls(
            tuple(receivers),
            positive("AIRCRAFT_TTL_SECONDS", "60"),
            os.environ.get("WEB_HOST", "0.0.0.0"), port("WEB_PORT", "8080"), ref,
            os.environ.get("AIRCRAFT_METADATA_PATH", str(DEFAULT_DATABASE)),
            map_center,
        )
