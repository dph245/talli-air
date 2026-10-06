from dataclasses import dataclass
import math
import os

from .input import ReceiverConfig


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
    receiver: ReceiverConfig
    aircraft_ttl: float
    web_host: str
    web_port: int
    surface_ref: tuple[float, float] | None

    @classmethod
    def from_env(cls):
        host = os.environ.get("RECEIVER_HOST", "").strip()
        receiver_id = os.environ.get("RECEIVER_ID", "receiver-1").strip()
        if not host or not receiver_id:
            raise ValueError("RECEIVER_HOST and RECEIVER_ID must be nonempty")
        lat, lon = os.environ.get("SURFACE_LAT") or None, os.environ.get("SURFACE_LON") or None
        ref = None
        if lat is not None or lon is not None:
            if lat is None or lon is None:
                raise ValueError("Set both SURFACE_LAT and SURFACE_LON")
            ref = (float(lat), float(lon))
            if not (-90 <= ref[0] <= 90 and -180 <= ref[1] <= 180):
                raise ValueError("Invalid surface reference coordinates")
        return cls(
            ReceiverConfig(receiver_id, host, port("RECEIVER_PORT", "47806"),
                           positive("RECONNECT_SECONDS", "5"),
                           positive("RECEIVER_IDLE_SECONDS", "60")),
            positive("AIRCRAFT_TTL_SECONDS", "60"),
            os.environ.get("WEB_HOST", "0.0.0.0"), port("WEB_PORT", "8080"), ref,
        )
