from dataclasses import replace
import time

import pytest
from pyModeS.util import crc

from talli_flug.input import parse_line
from talli_flug.state import AircraftStore

EVEN = "8D40621D58C382D690C8AC2863A7"
ODD = "8D40621D58C386435CC412692AD6"
CALLSIGN = "8D4840D6202CC371C32CE0576098"
VELOCITY = "8D485020994409940838175B284F"
SAMPLE = "8D440DA5F82300030049B8930905"


def frame(raw, at=None, receiver="r1", metadata=()):
    result = parse_line(f"*{raw};".encode(), receiver)
    return replace(result, received_monotonic=time.monotonic() if at is None else at,
                   metadata=metadata)


def with_crc(raw, address=0):
    """Generate parity for synthetic edge-case messages, not recorded fixtures."""
    raw = raw[:-6] + "000000"
    return raw[:-6] + f"{crc(raw) ^ address:06X}"


def test_sample_preserves_raw_metadata_without_inventing_values():
    store = AircraftStore()
    store.update(frame(SAMPLE, metadata=("FE3418B8", "06", "057A")))
    row, = store.snapshot()
    assert row["icao"] == "440DA5"
    assert row["df"] == 17
    assert row["latest_raw_frame"] == SAMPLE
    assert row["receiver_metadata"] == ("FE3418B8", "06", "057A")
    assert row["latitude"] is row["altitude"] is row["speed"] is row["callsign"] is None
    assert row["on_ground"] is False


def test_callsign_and_updates_from_second_receiver():
    store = AircraftStore()
    store.update(frame(CALLSIGN))
    assert store.aircraft["4840D6"].callsign == "KLM1023"
    store.update(frame(with_crc("5D4840D6000000"), receiver="r2"))
    row, = store.snapshot()
    assert row["callsign"] == "KLM1023"
    assert row["receiver_id"] == "r2"
    assert row["df"] == 11
    assert row["on_ground"] is False


def test_velocity():
    store = AircraftStore()
    store.update(frame(VELOCITY))
    row, = store.snapshot()
    assert row["speed"] == 159
    assert row["track"] == pytest.approx(182.88037755)
    assert row["vertical_rate"] == -832
    assert row["altitude"] is None


@pytest.mark.parametrize("messages, expected_lat, expected_lon", [
    ((EVEN, ODD), 52.2657801741, 3.9389125279),
    ((ODD, EVEN), 52.2572021484, 3.9193725586),
])
def test_cpr_both_arrival_orders(messages, expected_lat, expected_lon):
    store = AircraftStore()
    store.update(frame(messages[0], at=100))
    assert store.aircraft["40621D"].latitude is None
    store.update(frame(messages[1], at=105))
    aircraft = store.aircraft["40621D"]
    assert aircraft.latitude == pytest.approx(expected_lat)
    assert aircraft.longitude == pytest.approx(expected_lon)
    assert aircraft.altitude == 38000
    assert aircraft.on_ground is False


def test_cpr_rejects_stale_and_cross_receiver_pairs():
    store = AircraftStore()
    store.update(frame(EVEN, at=100))
    store.update(frame(ODD, at=111))
    assert store.aircraft["40621D"].latitude is None
    store.update(frame(EVEN, at=112, receiver="r2"))
    assert store.aircraft["40621D"].latitude is None
    store.update(frame(EVEN, at=113))
    assert store.aircraft["40621D"].latitude is not None


def test_expiration_removes_aircraft_and_pending_cpr():
    store = AircraftStore(ttl=20)
    store.update(frame(EVEN, at=100))
    store.expire(119.9)
    assert len(store.aircraft) == 1
    store.expire(120)
    assert not store.aircraft
    store.update(frame(ODD, at=121))
    assert store.aircraft["40621D"].latitude is None


def test_snapshot_expires_without_new_frames():
    store = AircraftStore(ttl=20)
    store.update(frame(EVEN, at=time.monotonic() - 21))
    assert store.snapshot() == []


def test_bad_crc_and_unsupported_df18_do_not_create_aircraft():
    store = AircraftStore()
    store.update(frame(EVEN[:-1] + "0"))
    store.update(frame(with_crc("91" + EVEN[2:])))
    assert not store.aircraft


def test_address_parity_requires_known_aircraft():
    store = AircraftStore()
    short = with_crc("20001718000000", int("40621D", 16))
    store.update(frame(short))
    assert not store.aircraft
    store.update(frame(EVEN))
    store.update(frame(short))
    row, = store.snapshot()
    assert row["icao"] == "40621D"
    assert row["df"] == 4
    assert row["altitude"] == 36000


def test_gnss_altitude_does_not_replace_barometric_altitude():
    store = AircraftStore()
    store.update(frame(EVEN))
    gnss = with_crc(EVEN[:8] + "A0" + EVEN[10:])
    store.update(frame(gnss))
    assert store.aircraft["40621D"].altitude == 38000


def test_airspeed_and_heading_are_not_ground_speed_and_track():
    store = AircraftStore()
    store.update(frame("8DA05F219B06B6AF189400CBC33F"))
    row, = store.snapshot()
    assert row["speed"] is None
    assert row["track"] is None
    assert row["vertical_rate"] == -2304


def test_cpr_does_not_mix_gnss_and_baro_pairs():
    store = AircraftStore()
    store.update(frame(EVEN, at=100))
    store.update(frame(with_crc(ODD[:8] + "A0" + ODD[10:]), at=101))
    assert store.aircraft["40621D"].latitude is None


def test_surface_needs_reference_and_transition_clears_airborne_position():
    # Synthetic surface CPR pair encodes lat=0, lon=0 near a (0, 0) reference.
    even = with_crc("8D40621D28000000000000000000")
    odd = with_crc("8D40621D28000400000000000000")
    for reference in (None, (0, 0)):
        store = AircraftStore(surface_ref=reference)
        for at, raw in enumerate((EVEN, ODD, even, odd), start=100):
            store.update(frame(raw, at=at))
        aircraft = store.aircraft["40621D"]
        assert aircraft.on_ground is True
        assert aircraft.altitude is None
        assert aircraft.latitude == (None if reference is None else 0)
        store.update(frame(EVEN, at=105))
        assert aircraft.on_ground is False
        assert aircraft.latitude is None
        assert aircraft.altitude == 38000


def test_retained_state_keeps_observation_time_when_latest_frame_changes():
    store = AircraftStore()
    first = replace(frame(CALLSIGN, at=100), received_at=1000)
    latest = replace(frame(with_crc("5D4840D6000000"), at=105, receiver="r2"),
                     received_at=1005)
    store.update(first)
    store.update(latest)
    aircraft = store.aircraft["4840D6"]
    assert aircraft.latest_raw_frame == latest.raw
    assert aircraft.df == 11
    assert aircraft.last_seen == 1005
    assert aircraft.callsign == "KLM1023"
    assert aircraft.observations["callsign"].received_at == 1000
    assert aircraft.observations["callsign"].received_monotonic == 100
    assert aircraft.observations["on_ground"].received_at == 1005
    store.update(replace(first, received_at=1006, received_monotonic=106))
    assert aircraft.callsign == "KLM1023"
    assert aircraft.observations["callsign"].received_at == 1006


def test_position_observation_time_requires_successfully_resolved_pair():
    store = AircraftStore()
    store.update(replace(frame(EVEN, at=100), received_at=1000))
    aircraft = store.aircraft["40621D"]
    assert "latitude" not in aircraft.observations
    store.update(replace(frame(ODD, at=105), received_at=1005))
    position_time = aircraft.observations["latitude"]
    assert position_time.received_at == 1005
    assert position_time.received_monotonic == 105
    assert aircraft.observations["longitude"].received_at == position_time.received_at
    assert aircraft.observations["longitude"].received_monotonic == position_time.received_monotonic
    # Both cached halves have expired: altitude is re-observed, position is not.
    store.update(replace(frame(EVEN, at=116), received_at=1016))
    assert aircraft.observations["latitude"] == position_time
    assert aircraft.observations["altitude"].received_at == 1016
    ground = with_crc("8D40621D28000000000000000000")
    store.update(replace(frame(ground, at=117), received_at=1017))
    assert aircraft.latitude is aircraft.altitude is None
    assert "latitude" not in aircraft.observations
    assert "longitude" not in aircraft.observations
    assert "altitude" not in aircraft.observations
    assert aircraft.observations["on_ground"].received_at == 1017


def test_unavailable_altitude_records_report_time_without_retaining_old_value():
    store = AircraftStore()
    store.update(frame(EVEN, at=100))
    # DF4 with zero AC reports altitude unavailable for the known ICAO.
    unavailable = with_crc("20000000000000", int("40621D", 16))
    store.update(replace(frame(unavailable, at=101), received_at=1001))
    aircraft = store.aircraft["40621D"]
    assert aircraft.altitude is None
    assert aircraft.observations["altitude"].received_at == 1001
    assert aircraft.observations["altitude"].received_monotonic == 101
