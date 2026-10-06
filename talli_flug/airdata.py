"""Conservative library-backed air-data decoding and time-aligned estimates."""

from dataclasses import asdict
from datetime import datetime, timezone
import math

from pygeomag import GeoMag, BlackoutZoneException, CautionZoneException
from pygeomag.wmm.wmm_2025 import WMM_2025

# Each store serializes calls with its lock. No network or model downloads.
MAX_AGE = 10.0
MAX_SKEW = 5.0
COMMB_TRUST_AGE = 60.0


def unique_bds(decoded: dict) -> str | None:
    candidates = decoded.get("bds_candidates", [decoded["bds"]] if "bds" in decoded else [])
    return candidates[0] if len(candidates) == 1 else None


def apply_airdata(aircraft, frame, decoded: dict) -> None:
    df, tc = decoded["df"], decoded.get("typecode")
    if df in (17, 18):
        source = f"ADS-B TC{tc}"
        if tc == 29 and decoded.get("subtype") == 1:
            aircraft.observe("selected_altitude", decoded.get("selected_altitude"), frame, source)
            aircraft.selected_altitude_source = decoded.get("selected_altitude_source")
        if tc == 19:
            kind = decoded.get("airspeed_type")
            if kind in ("IAS", "TAS"):
                aircraft.observe(kind.lower(), decoded.get("airspeed"), frame, source)
            aircraft.observe("geo_minus_baro", decoded.get("geo_minus_baro"), frame, source)
            # TC19's heading reference needs independent HRD/version context.
            # Magnetic heading is taken only from unambiguous BDS 6,0 here.
        if tc in (20, 21, 22):
            aircraft.observe("gnss_altitude", decoded.get("altitude"), frame, source)
        return

    if df not in (20, 21):
        return
    # DF11's weak parity check is insufficient to trust new Comm-B air data.
    trusted = aircraft.adsb_verified_at
    if trusted is None or not 0 <= frame.received_monotonic - trusted <= COMMB_TRUST_AGE:
        return
    bds = unique_bds(decoded)
    if bds == "4,4" and decoded.get("figure_of_merit") not in (1, 2, 3, 4):
        return  # FOM/source zero explicitly means invalid meteorological data.
    fields = {
        "4,0": {"selected_altitude_mcp": "selected_altitude_mcp",
                "selected_altitude_fms": "selected_altitude_fms"},
        "5,0": {"true_airspeed": "tas", "roll": "roll", "track_rate": "track_rate",
                "groundspeed": "speed", "true_track": "track"},
        "6,0": {"indicated_airspeed": "ias", "mach": "mach",
                "magnetic_heading": "magnetic_heading"},
        "4,4": {"static_air_temperature": "temperature", "wind_speed": "reported_wind_speed",
                "wind_direction": "reported_wind_direction"},
        "4,5": {"static_air_temperature": "temperature"},
    }.get(bds)
    if fields is None:
        return
    source = f"Comm-B BDS {bds} (unique library inference)"
    # Missing status-gated fields mean unavailable in this recognized register.
    for source_name, target in fields.items():
        aircraft.observe(target, decoded.get(source_name), frame, source)
    if bds == "4,0":
        # Keep both reports. Prefer the explicitly named target when available;
        # otherwise display the MCP report, then FMS, with a source label.
        preferred = "fms" if decoded.get("target_altitude_source") == "fms" else "mcp"
        other = "mcp" if preferred == "fms" else "fms"
        for kind in (preferred, other):
            value = decoded.get(f"selected_altitude_{kind}")
            if value is not None:
                aircraft.observe("selected_altitude", value, frame, source)
                aircraft.selected_altitude_source = "MCP/FCU" if kind == "mcp" else "FMS"
                break
        else:
            aircraft.observe("selected_altitude", None, frame, source)
            aircraft.selected_altitude_source = None


def compatible(aircraft, names, now):
    observations = {name: aircraft.observations.get(name) for name in names}
    if any(item is None or item.value is None for item in observations.values()):
        return None
    times = [item.received_monotonic for item in observations.values()]
    if any(not 0 <= now - timestamp <= MAX_AGE for timestamp in times):
        return None
    if max(times) - min(times) > MAX_SKEW:
        return None
    if len({item.receiver_id for item in observations.values()}) != 1:
        return None
    return observations


def estimate(value, unit, method, inputs):
    return {"value": value, "unit": unit, "method": method,
            "received_at": max(item.received_at for item in inputs.values()),
            "inputs": {name: {key: val for key, val in asdict(item).items()
                               if key != "received_monotonic"}
                       for name, item in inputs.items()}}


def temperature_from_tas_mach(tas, mach):
    # Dry-air speed of sound: a² = gamma * R * T; knots -> metres/second.
    if not (100 <= tas <= 600 and 0.3 <= mach <= 1):
        return None
    value = (tas * 1852 / 3600 / mach) ** 2 / (1.4 * 287.05) - 273.15
    return value if -80 <= value <= 60 else None


def wind_vector(groundspeed, track, tas, true_heading):
    track, heading = math.radians(track), math.radians(true_heading)
    east = groundspeed * math.sin(track) - tas * math.sin(heading)
    north = groundspeed * math.cos(track) - tas * math.cos(heading)
    speed = math.hypot(east, north)
    # Meteorological direction is where the wind comes FROM, clockwise from north.
    direction = (math.degrees(math.atan2(-east, -north)) + 360) % 360
    return speed, None if speed < 1 else direction


def magnetic_declination(latitude, longitude, altitude_ft, timestamp):
    date = datetime.fromtimestamp(timestamp, timezone.utc)
    start = datetime(date.year, 1, 1, tzinfo=timezone.utc)
    end = datetime(date.year + 1, 1, 1, tzinfo=timezone.utc)
    year = date.year + (date - start).total_seconds() / (end - start).total_seconds()
    # Pin WMM2025; never extrapolate beyond its lifespan or use magnetic warning zones.
    return GeoMag(coefficients_data=WMM_2025).calculate(
        glat=latitude, glon=longitude, alt=altitude_ft * 0.0003048,
        time=year, raise_in_warning_zone=True).d


def derive_airdata(aircraft, now):
    result = {}
    if aircraft.on_ground is not False:
        return result
    inputs = compatible(aircraft, ("tas", "mach"), now)
    if inputs:
        temperature = temperature_from_tas_mach(inputs["tas"].value, inputs["mach"].value)
        if temperature is not None:
            result["temperature"] = estimate(temperature, "°C", "Derived from TAS and Mach", inputs)

    # Require straight, approximately level flight and an independently observed
    # geometric altitude for WMM. Do not quietly treat pressure altitude as GNSS.
    names = ["speed", "track", "tas", "magnetic_heading", "latitude", "longitude",
             "roll", "track_rate", "vertical_rate"]
    if compatible(aircraft, names + ["gnss_altitude"], now):
        names += ["gnss_altitude"]
    else:
        names += ["altitude", "geo_minus_baro"]
    inputs = compatible(aircraft, names, now)
    if inputs is None:
        return result
    values = {name: item.value for name, item in inputs.items()}
    if (abs(values["roll"]) > 5 or abs(values["track_rate"]) > 1
            or abs(values["vertical_rate"]) > 1000
            or not 100 <= values["tas"] <= 600 or not 100 <= values["speed"] <= 700):
        return result
    altitude = (values["gnss_altitude"] if "gnss_altitude" in values
                else values["altitude"] + values["geo_minus_baro"])
    try:
        declination = magnetic_declination(values["latitude"], values["longitude"], altitude,
                                          inputs["magnetic_heading"].received_at)
    except (ValueError, ArithmeticError):
        return result
    # pygeomag warning-zone exceptions derive directly from Exception.
    except (BlackoutZoneException, CautionZoneException):
        return result
    heading = (values["magnetic_heading"] + declination) % 360
    speed, direction = wind_vector(values["speed"], values["track"], values["tas"], heading)
    if speed > 200:
        return result
    for name, value, unit in (("wind_speed", speed, "kt"), ("wind_direction", direction, "° true, from")):
        result[name] = estimate(value, unit, "Derived wind vector; magnetic heading corrected with WMM2025", inputs)
        result[name]["declination"] = declination
    return result
