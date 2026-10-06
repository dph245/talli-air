"""Recorded/reference messages plus explicitly synthetic status-bit edge cases."""

from dataclasses import replace
import json
from pathlib import Path

import pyModeS
from pyModeS.util import crc
import pytest

from talli_flug import airdata
from talli_flug.input import Frame
from talli_flug.state import AircraftStore
from talli_flug.web import render, render_details

# Reference examples from The 1090 MHz Riddle / pyModeS quickstart.
BDS40 = "A000029C85E42F313000007047D3"
BDS50 = "A000139381951536E024D4CCF6B5"
BDS60 = "A00004128F39F91A7E27C46ADC21"
BDS44 = "A0001692185BD5CF400000DFC696"
AMBIGUOUS = "A8001EBCFFFB23286004A73F6A5B"
TARGET = "8DA32844EA2D0878015F8869B3A5"
TAS = "8DA05F219B06B6AF189400CBC33F"


def parity(raw, address=0):
    raw = raw[:-6] + "000000"
    return raw[:-6] + f"{crc(raw) ^ address:06X}"


def frame(raw, at=100, receiver="r1"):
    return Frame(receiver, raw, (), 1791310600 + at, at)


def trusted_store(raw, at=100):
    """Synthetic CRC-valid ADS-B identity to corroborate a reference frame's ICAO."""
    icao = pyModeS.decode(raw)["icao"]
    store = AircraftStore()
    seed = parity("8D" + icao + "202CC371C32CE0000000")
    store.update(frame(seed, at - 1))
    return store, icao


@pytest.mark.parametrize("raw,bds,expected", [
    (BDS40, "4,0", {"selected_altitude": 3008, "selected_altitude_mcp": 3008, "selected_altitude_fms": 3008}),
    (BDS50, "5,0", {"tas": 424, "roll": 2.109375, "speed": 438, "track": 114.2578125}),
    (BDS60, "6,0", {"ias": 252, "mach": 0.42, "magnetic_heading": 42.71484375}),
    (BDS44, "4,4", {"temperature": -48.75, "reported_wind_speed": 22, "reported_wind_direction": 344.53125}),
])
def test_known_commb_frames(raw, bds, expected):
    assert airdata.unique_bds(pyModeS.decode(raw, include_meteo=True)) == bds
    store, icao = trusted_store(raw)
    f = frame(raw)
    store.update(f)
    aircraft = store.aircraft[icao]
    for name, value in expected.items():
        assert getattr(aircraft, name) == pytest.approx(value)
        observation = aircraft.observations[name]
        assert observation.raw_frame == raw
        assert f"BDS {bds}" in observation.source
        assert observation.receiver_id == f.receiver_id
        assert observation.received_at == f.received_at
        assert observation.received_monotonic == f.received_monotonic


def test_known_df21_selected_altitude_from_live_capture():
    raw = "A8000800BC95E4B0A80187AD1AF3"
    store, icao = trusted_store(raw)
    store.update(frame(raw))
    aircraft = store.aircraft[icao]
    assert aircraft.df == 21
    assert aircraft.selected_altitude == 31008
    assert aircraft.selected_altitude_source == "FMS"


def test_known_adsb_target_and_airspeed_frames():
    store = AircraftStore()
    store.update(frame(TARGET))
    aircraft = store.aircraft["A32844"]
    assert aircraft.selected_altitude == 23008
    assert aircraft.selected_altitude_source == "MCP/FCU"
    store.update(frame(TAS))
    aircraft = store.aircraft["A05F21"]
    assert aircraft.tas == 375
    assert aircraft.speed is None
    assert aircraft.magnetic_heading is None  # Unconfirmed ADS-B heading reference.


def test_adsb_ias_and_unsupported_target_subtype():
    # Synthetic ADS-B TAS -> IAS type flag, preserving all other payload bits.
    payload = int(TAS[8:22], 16) & ~(1 << 31)
    ias = parity(TAS[:8] + f"{payload:014X}" + "000000")
    store = AircraftStore()
    store.update(frame(ias))
    assert store.aircraft["A05F21"].ias == 375
    payload = int(TARGET[8:22], 16) & ~(3 << 49)
    unsupported = parity(TARGET[:8] + f"{payload:014X}" + "000000")
    store.update(frame(unsupported))
    assert store.aircraft["A32844"].selected_altitude is None


def test_ambiguous_inference_never_uses_preferred_candidate():
    decoded = pyModeS.decode(AMBIGUOUS, include_meteo=True)
    assert decoded["bds_candidates"] == ["5,0", "6,0"]
    assert decoded["bds"] == "5,0"  # Library preference is deliberately ignored.
    store, icao = trusted_store(AMBIGUOUS)
    store.update(frame(AMBIGUOUS))
    aircraft = store.aircraft[icao]
    assert aircraft.tas is aircraft.ias is aircraft.roll is aircraft.magnetic_heading is None
    assert "tas" not in aircraft.observations
    assert aircraft.latest_raw_frame == AMBIGUOUS


def test_commb_requires_recent_crc_valid_adsb_even_if_aircraft_remains_active():
    store, icao = trusted_store(BDS60)
    # Keep aircraft alive with a valid DF11 all-call, without renewing ADS-B trust.
    store.update(frame(parity("5D" + icao + "000000"), at=150))
    store.update(frame(BDS60, at=161))
    assert store.aircraft[icao].mach is None
    assert store.aircraft[icao].latest_raw_frame == BDS60


def test_status_unavailable_clears_previously_decoded_field():
    store, icao = trusted_store(BDS60)
    store.update(frame(BDS60))
    # Synthetic BDS60 IAS status and value unset. Other valid fields retained.
    payload = int(BDS60[8:22], 16) & ~(((1 << 11) - 1) << 33)
    raw = parity(BDS60[:8] + f"{payload:014X}" + "000000", int(icao, 16))
    assert airdata.unique_bds(pyModeS.decode(raw, include_meteo=True)) == "6,0"
    store.update(frame(raw, at=101))
    assert store.aircraft[icao].ias is None
    assert store.aircraft[icao].observations["ias"].received_monotonic == 101


def test_synthetic_bds45_temperature():
    # BDS45 has no recorded example in our live capture. This deliberately
    # synthetic MHR payload exercises pyModeS's temperature status handling.
    raw = parity("A00004190001FB80000000000000", int("484B32", 16))
    assert airdata.unique_bds(pyModeS.decode(raw, include_meteo=True)) == "4,5"
    store, icao = trusted_store(raw)
    store.update(frame(raw))
    assert store.aircraft[icao].temperature == -4.5
    assert airdata.derive_airdata(store.aircraft[icao], 100) == {}


def test_meteorological_report_with_invalid_source_is_rejected():
    raw = parity(BDS44[:8] + "0" + BDS44[9:], int("3C4DD7", 16))
    assert pyModeS.decode(raw, include_meteo=True)["figure_of_merit"] == 0
    store, icao = trusted_store(raw)
    store.update(frame(raw))
    assert store.aircraft[icao].temperature is None
    assert store.aircraft[icao].reported_wind_speed is None


def load_live_wind():
    capture = json.loads((Path(__file__).parent / "fixtures/live_wind.json").read_text())
    store = AircraftStore()
    for record in capture["records"]:
        f = Frame("receiver-1", record["raw"], tuple(record["metadata"]),
                  record["received_at"], record["offset"] + 100)
        store.update(f)
    return store, store.aircraft["484B32"], f.received_monotonic


def test_live_frames_produce_time_aligned_estimates_and_provenance():
    _, aircraft, now = load_live_wind()
    result = airdata.derive_airdata(aircraft, now)
    assert result["wind_speed"]["value"] == pytest.approx(11.05448, abs=0.01)
    assert result["wind_direction"]["value"] == pytest.approx(128.82081, abs=0.01)
    assert result["wind_speed"]["declination"] == pytest.approx(4.3285, abs=0.001)
    inputs = result["wind_speed"]["inputs"]
    times = [value["received_at"] for value in inputs.values()]
    assert max(times) - min(times) <= 5
    assert all(value["raw_frame"] and value["source"] for value in inputs.values())
    assert aircraft.temperature is None  # Never overwrite transmitted weather with estimates.
    assert aircraft.reported_wind_speed is None
    assert result["temperature"]["method"] == "Derived from TAS and Mach"


@pytest.mark.parametrize("name", ["tas", "mach", "magnetic_heading", "latitude", "longitude",
                                 "roll", "track_rate", "vertical_rate", "altitude", "geo_minus_baro"])
def test_stale_inputs_prevent_derivation(name):
    _, aircraft, now = load_live_wind()
    aircraft.observations[name] = replace(aircraft.observations[name], received_monotonic=now - 11)
    result = airdata.derive_airdata(aircraft, now)
    if name in ("tas", "mach"):
        assert "temperature" not in result
    if name != "mach":
        assert "wind_speed" not in result


def test_time_skew_and_cross_receiver_are_rejected():
    _, aircraft, now = load_live_wind()
    aircraft.observations["tas"] = replace(aircraft.observations["tas"], received_monotonic=now - 9)
    assert airdata.derive_airdata(aircraft, now) == {}
    _, aircraft, now = load_live_wind()
    aircraft.observations["tas"] = replace(aircraft.observations["tas"], receiver_id="other")
    assert airdata.derive_airdata(aircraft, now) == {}


@pytest.mark.parametrize("field,value", [("roll", 10), ("track_rate", 2), ("vertical_rate", 2000)])
def test_manoeuvres_suppress_wind(field, value):
    _, aircraft, now = load_live_wind()
    aircraft.observations[field] = replace(aircraft.observations[field], value=value)
    assert "wind_speed" not in airdata.derive_airdata(aircraft, now)


def test_unknown_altitude_and_expired_model_suppress_wind(monkeypatch):
    _, aircraft, now = load_live_wind()
    aircraft.invalidate("geo_minus_baro", "gnss_altitude")
    assert "wind_speed" not in airdata.derive_airdata(aircraft, now)
    _, aircraft, now = load_live_wind()
    aircraft.observations["magnetic_heading"] = replace(
        aircraft.observations["magnetic_heading"], received_at=1956528000)  # 2032
    assert "wind_speed" not in airdata.derive_airdata(aircraft, now)


def test_wind_direction_convention_and_temperature_formula():
    # 20 kt tailwind northbound comes from the south, not the north.
    assert airdata.wind_vector(420, 0, 400, 0) == pytest.approx((20, 180))
    assert airdata.wind_vector(380, 0, 400, 0) == pytest.approx((20, 0))
    assert airdata.wind_vector(400, 0, 400, 0) == (0, None)
    assert airdata.temperature_from_tas_mach(406, 0.676) == pytest.approx(-35.6027, abs=0.001)
    assert airdata.temperature_from_tas_mach(400, 0) is None
    assert airdata.temperature_from_tas_mach(400, 0.1) is None
    assert airdata.temperature_from_tas_mach(600, 0.3) is None


def test_details_and_api_distinguish_direct_and_derived_and_expire_estimates(monkeypatch):
    store, aircraft, now = load_live_wind()
    monkeypatch.setattr("talli_flug.state.time.monotonic", lambda: now)
    row, = store.snapshot()
    html = render([row], "r1", True)
    assert "Selected altitude (ft)" in html
    assert "/aircraft/484B32" in html
    assert "Magnetic heading" not in html
    details = render_details(row)
    assert "Reported temperature" in details and "Derived temperature" in details
    assert "WMM2025" in details and "Source frame" in details
    assert row["derived"]["wind_speed"]["inputs"]["tas"]["value"] == aircraft.tas
    monkeypatch.setattr("talli_flug.state.time.monotonic", lambda: now + 11)
    row, = store.snapshot()
    assert row["derived"] == {}
    assert row["tas"] == aircraft.tas  # Direct last observation remains with its actual time.
