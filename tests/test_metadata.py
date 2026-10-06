from dataclasses import asdict
import io
import json
from pathlib import Path
import socket
import zipfile

import pytest

from talli_flug.input import parse_line
from talli_flug.metadata import (
    DEFAULT_DATABASE, AircraftMetadata, LocalAircraftMetadata, enrich_rows, parse_aircraft,
)
from talli_flug.state import AircraftStore
from talli_flug.web import metadata_notice, render, render_details
from tools.update_aircraft_metadata import install_snapshot, make_snapshot


def upstream(rows=None, license_text="ODC Attribution License (fixture)"):
    rows = rows if rows is not None else {
        "440DA5": {"r": "OE-IDS", "t": "A320", "d": "Airbus A320 (test fixture)"},
        "000ABC": {"r": "", "t": "", "d": ""},
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("aircrafts.json", json.dumps(rows))
        archive.writestr("LICENSE", license_text)
        archive.writestr("dbversion.json", '{"version": 1}')
    return output.getvalue()


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "aircraft.zip"
    data = make_snapshot(upstream(), "a" * 40, "2026-10-01T00:00:00Z", "2026-10-06T00:00:00Z")
    install_snapshot(data, path)
    return LocalAircraftMetadata.load(path)


def test_lookup_normalizes_hex_and_preserves_unknown_fields(database, monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: pytest.fail("Lookup attempted network IO"))
    assert database.lookup("440da5").registration == "OE-IDS"
    assert database.lookup("440DA5").type_designator == "A320"
    assert database.lookup("440DA5").operator is None
    assert asdict(database.lookup("000abc")) == {
        "registration": None, "type_designator": None, "description": None, "operator": None,
    }
    for missing in ("FFFFFF", "ABC", "*440DA5;", "~440DA5", "440DA5 "):
        assert database.lookup(missing) is None


def test_external_metadata_never_changes_receiver_state_or_observations(database):
    store = AircraftStore()
    frame = parse_line(b"*8D440DA5F82300030049B8930905;FE3418B8;06;057A;", "rx")
    store.update(frame)
    original = store.snapshot()
    aircraft_before = asdict(store.aircraft["440DA5"])
    row, = enrich_rows(original, database)
    assert row["external_metadata"]["registration"] == "OE-IDS"
    assert row["external_metadata"]["source"]["license"] == "ODC-By 1.0"
    assert "external_metadata" not in original[0]
    assert "registration" not in row["field_observations"]
    assert row["last_seen"] == frame.received_at
    assert row["receiver_metadata"] == ("FE3418B8", "06", "057A")
    assert asdict(store.aircraft["440DA5"]) == aircraft_before
    row["external_metadata"]["registration"] = "modified response"
    assert database.lookup("440DA5").registration == "OE-IDS"


def test_unknown_and_disabled_database_are_distinct(database):
    unknown, = enrich_rows([{"icao": "FFFFFF"}], database)
    assert unknown["external_metadata"]["matched"] is False
    assert unknown["external_metadata"]["source"]["revision"] == "a" * 40
    disabled, = enrich_rows([{"icao": "440DA5"}], LocalAircraftMetadata())
    assert disabled["external_metadata"]["source"] is None
    assert disabled["external_metadata"]["registration"] is None


def test_replacement_provider_can_supply_reliable_operator(database):
    class Replacement:
        source = database.source

        def lookup(self, icao24):
            return AircraftMetadata("TEST-REG", "A359", "Example model", "Explicit operator record")

    row, = enrich_rows([{"icao": "440DA5"}], Replacement())
    assert row["external_metadata"]["operator"] == "Explicit operator record"


def test_ui_labels_metadata_and_keeps_description_in_details(database):
    store = AircraftStore()
    store.update(parse_line(b"*8D440DA5F82300030049B8930905;", "rx"))
    rows = enrich_rows(store.snapshot(), database)
    html = render(rows, "rx", True, database.source)
    assert "Registration</th>" in html and "Type</th>" in html
    assert "OE-IDS" in html and "A320" in html
    assert "Airbus A320 (test fixture)" not in html
    assert "external database metadata" in html
    assert "Mictronics aircraft database" in html and "ODC-By 1.0" in html
    details = render_details(rows[0])
    assert "External aircraft metadata" in details and "Manufacturer / model description" in details
    assert "Airbus A320 (test fixture)" in details
    assert "ODC-By 1.0" in details and "Mictronics aircraft database" in details
    assert "no local database loaded" not in details
    assert details.index("External aircraft metadata") < details.index("Decoded reports")
    rows[0]["external_metadata"]["description"] = '<script>alert("db")</script>'
    assert "<script>" not in render_details(rows[0])


def test_attribution_exists_on_empty_table_and_unsafe_urls_are_not_links(database):
    assert "ODC-By 1.0" in render([], "rx", False, database.source)
    notice = metadata_notice({**database.source, "url": "javascript:alert(1)"})
    assert "javascript:" not in notice


@pytest.mark.parametrize("rows", [[], {}, {"INVALID": {"r": "", "t": "", "d": ""}},
                                   {"000ABC": {"r": 123, "t": "", "d": ""}},
                                   {"000ABC": {}, "000abc": {}}])
def test_bad_database_records_are_rejected(rows):
    with pytest.raises((ValueError, KeyError)):
        parse_aircraft(rows)


def test_invalid_snapshot_does_not_replace_working_copy(tmp_path):
    path = tmp_path / "db.zip"
    good = make_snapshot(upstream(), "a" * 40, "date", "date")
    install_snapshot(good, path)
    with pytest.raises(zipfile.BadZipFile):
        install_snapshot(b"broken download", path)
    assert path.read_bytes() == good
    assert list(tmp_path.iterdir()) == [path]
    with pytest.raises(ValueError, match="license changed"):
        make_snapshot(upstream(license_text="Other terms"), "b" * 40, "date", "date")


def test_bundled_snapshot_is_self_contained():
    db = LocalAircraftMetadata.load(DEFAULT_DATABASE)
    assert len(db) == db.source["records"]
    assert len(db) > 400000
    assert db.lookup("440DA5") is not None
    with zipfile.ZipFile(DEFAULT_DATABASE) as archive:
        assert b"ODC Attribution License" in archive.read("LICENSE")
        assert db.source["upstream_sha256"]
