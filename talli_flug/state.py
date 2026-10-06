"""Decode frames and keep only current aircraft and bounded CPR state."""

from dataclasses import asdict, dataclass, field
import logging
import threading
import time

import pyModeS
from pyModeS.position import airborne_position_pair, surface_position_pair
from pyModeS.util import crc

from .input import Frame
from .airdata import apply_airdata, derive_airdata

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Observation:
    received_at: float
    received_monotonic: float
    receiver_id: str
    raw_frame: str
    source: str
    value: object


@dataclass
class Aircraft:
    icao: str
    df: int | None = None
    callsign: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: int | None = None
    speed: float | None = None
    track: float | None = None
    vertical_rate: int | None = None
    on_ground: bool | None = None
    selected_altitude: int | None = None
    selected_altitude_source: str | None = None
    selected_altitude_mcp: int | None = None
    selected_altitude_fms: int | None = None
    ias: float | None = None
    tas: float | None = None
    mach: float | None = None
    magnetic_heading: float | None = None
    roll: float | None = None
    track_rate: float | None = None
    temperature: float | None = None
    reported_wind_speed: float | None = None
    reported_wind_direction: float | None = None
    gnss_altitude: float | None = None
    geo_minus_baro: float | None = None
    last_seen: float = 0
    receiver_id: str = ""
    latest_raw_frame: str = ""
    receiver_metadata: tuple[str, ...] = ()
    updated: float = field(default=0, repr=False)
    adsb_verified_at: float | None = field(default=None, repr=False)
    observations: dict[str, Observation] = field(default_factory=dict, repr=False)
    # Latest even/odd per receiver and position family; never mix ground/air
    # or GNSS/barometric position messages.
    cpr: dict = field(default_factory=dict, repr=False)

    def observe(self, name: str, value, frame: Frame, source: str = "Mode-S / ADS-B") -> None:
        """Timestamp an actual report, including an explicitly unavailable value."""
        setattr(self, name, value)
        self.observations[name] = Observation(frame.received_at, frame.received_monotonic,
                                               frame.receiver_id, frame.raw, source, value)

    def invalidate(self, *names: str) -> None:
        """A transition invalidates retained values; it does not observe new ones."""
        for name in names:
            setattr(self, name, None)
            self.observations.pop(name, None)


class AircraftStore:
    def __init__(self, ttl: float = 60, surface_ref: tuple[float, float] | None = None):
        self.ttl = ttl
        self.surface_ref = surface_ref
        self.aircraft: dict[str, Aircraft] = {}
        self.lock = threading.Lock()

    def expire(self, now: float | None = None) -> None:
        with self.lock:
            self._expire(time.monotonic() if now is None else now)

    def _expire(self, now: float) -> None:
        for icao in list(self.aircraft):
            if now - self.aircraft[icao].updated >= self.ttl:
                del self.aircraft[icao]

    def update(self, frame: Frame) -> None:
        try:
            # Include meteorological validators when checking ambiguity too.
            # A preferred candidate alone is not a confidence decision.
            decoded = pyModeS.decode(frame.raw, include_meteo=True)
        except (ValueError, RuntimeError, NotImplementedError):
            LOG.debug("Undecodable frame from %s", frame.receiver_id, exc_info=True)
            return
        df = decoded.get("df")
        icao = decoded.get("icao")
        if not icao or df not in (0, 4, 5, 11, 16, 17, 18, 20, 21):
            return
        if decoded.get("crc_valid") is False:
            return
        # DF18 CF=0 explicitly carries an ICAO address. Other control fields
        # include non-ICAO and rebroadcast formats outside this MVP's scope.
        if df == 18 and int(frame.raw[:2], 16) & 7 != 0:
            return
        if df == 11 and crc(frame.raw) > 127:
            return
        with self.lock:
            self._expire(frame.received_monotonic)
            # Address/parity replies cannot independently verify their ICAO.
            # Require a current aircraft learned from an explicit address.
            if df in (0, 4, 5, 16, 20, 21) and icao not in self.aircraft:
                return
            aircraft = self.aircraft.setdefault(icao, Aircraft(icao))
            if frame.received_monotonic < aircraft.updated:
                return
            if df in (17, 18) and decoded.get("crc_valid") is True:
                aircraft.adsb_verified_at = frame.received_monotonic
            aircraft.df = df
            aircraft.last_seen = frame.received_at
            aircraft.updated = frame.received_monotonic
            aircraft.receiver_id = frame.receiver_id
            aircraft.latest_raw_frame = frame.raw
            aircraft.receiver_metadata = frame.metadata
            tc = decoded.get("typecode", 0)
            ground = None
            if df in (17, 18):
                if 5 <= tc <= 8 or (tc == 31 and decoded.get("subtype") == 1):
                    ground = True
                elif 9 <= tc <= 22 or (tc == 31 and decoded.get("subtype") == 0):
                    ground = False
            if df in (4, 5, 20, 21) and decoded.get("flight_status") in (0, 1, 2, 3):
                ground = decoded["flight_status"] in (1, 3)
            if df in (0, 16):
                ground = decoded.get("vertical_status") == "on-ground"
            if df == 11 and decoded.get("capability") in (4, 5):
                ground = decoded["capability"] == 4
            if ground is not None:
                if aircraft.on_ground is not None and ground != aircraft.on_ground:
                    aircraft.cpr.clear()
                    aircraft.invalidate("latitude", "longitude", "speed", "track",
                                        "vertical_rate", "altitude", "ias", "tas", "mach",
                                        "magnetic_heading", "roll", "track_rate", "temperature",
                                        "reported_wind_speed", "reported_wind_direction",
                                        "gnss_altitude", "geo_minus_baro")
                aircraft.observe("on_ground", ground, frame)
            if df in (17, 18):
                for source, target in (("callsign", "callsign"), ("groundspeed", "speed"),
                                       ("track", "track"), ("vertical_rate", "vertical_rate")):
                    if source in decoded:
                        value = decoded[source]
                        if source == "callsign" and value is not None:
                            value = value.strip().strip("_") or None
                        aircraft.observe(target, value, frame, f"ADS-B TC{tc}")
            if df in (0, 4, 16, 20) or (df in (17, 18) and 9 <= tc <= 18):
                aircraft.observe("altitude", decoded.get("altitude"), frame)

            if "cpr_format" in decoded:
                self._position(aircraft, frame, decoded)
            apply_airdata(aircraft, frame, decoded)

    def _position(self, aircraft: Aircraft, frame: Frame, decoded: dict) -> None:
        tc = decoded["typecode"]
        family = "surface" if tc <= 8 else "baro" if tc <= 18 else "gnss"
        parity = decoded["cpr_format"]
        now = frame.received_monotonic
        aircraft.cpr = {key: value for key, value in aircraft.cpr.items()
                        if now - value[0] <= 10}
        key = (frame.receiver_id, family, parity)
        aircraft.cpr[key] = (now, decoded["cpr_lat"], decoded["cpr_lon"])
        even = aircraft.cpr.get((frame.receiver_id, family, 0))
        odd = aircraft.cpr.get((frame.receiver_id, family, 1))
        if even is None or odd is None or abs(even[0] - odd[0]) > 10:
            return
        args = (*even[1:], *odd[1:])
        if family == "surface":
            if self.surface_ref is None:
                return
            position = surface_position_pair(*args, lat_ref=self.surface_ref[0],
                                             lon_ref=self.surface_ref[1],
                                             even_is_newer=even[0] >= odd[0])
        else:
            position = airborne_position_pair(*args, even_is_newer=even[0] >= odd[0])
        if position is not None:
            # Updates arrive in monotonic order; this is the newer CPR half's
            # receipt time, not the time of a later unrelated raw frame.
            aircraft.observe("latitude", position[0], frame, "ADS-B CPR pair")
            aircraft.observe("longitude", position[1], frame, "ADS-B CPR pair")

    def snapshot(self) -> list[dict]:
        with self.lock:
            now = time.monotonic()
            self._expire(now)
            rows = []
            for aircraft in sorted(self.aircraft.values(), key=lambda item: item.icao):
                row = asdict(aircraft)
                del row["updated"], row["cpr"], row["observations"], row["adsb_verified_at"]
                row["field_observations"] = {
                    name: {key: value for key, value in asdict(observation).items()
                           if key != "received_monotonic"}
                    for name, observation in aircraft.observations.items()
                }
                row["derived"] = derive_airdata(aircraft, now)
                rows.append(row)
            return rows
