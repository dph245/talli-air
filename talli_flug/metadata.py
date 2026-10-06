"""External aircraft metadata. No receiver state, callsign inference, or network IO."""

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Protocol
import zipfile

DEFAULT_DATABASE = Path(__file__).resolve().parent.parent / "data" / "aircraft-metadata.zip"
ICAO24 = re.compile(r"[0-9A-F]{6}")


@dataclass(frozen=True, slots=True)
class AircraftMetadata:
    registration: str | None = None
    type_designator: str | None = None
    description: str | None = None
    operator: str | None = None


class MetadataLookup(Protocol):
    source: dict | None

    def lookup(self, icao24: str) -> AircraftMetadata | None: ...


def optional_text(value) -> str | None:
    if not isinstance(value, str):
        raise ValueError("Metadata fields must be strings")
    return value.strip() or None


def parse_aircraft(rows: dict) -> dict[str, AircraftMetadata]:
    if not isinstance(rows, dict) or not rows:
        raise ValueError("Aircraft database must be a nonempty ICAO24 dictionary")
    result = {}
    for address, record in rows.items():
        key = address.upper()
        if not ICAO24.fullmatch(key) or key in result or not isinstance(record, dict):
            raise ValueError(f"Invalid or duplicate ICAO24 database key: {address!r}")
        registration = optional_text(record["r"])
        designator = optional_text(record["t"])
        description = optional_text(record["d"])
        # A designator is a code, not the short physical description (e.g. L2J).
        # Keep questionable source values unavailable rather than inferring a type.
        if designator and not re.fullmatch(r"[A-Z0-9]{2,4}", designator):
            designator = None
        result[key] = AircraftMetadata(registration, designator, description)
    return result


class LocalAircraftMetadata:
    def __init__(self, records=None, source=None):
        self._records = records if records is not None else {}
        self.source = source

    @classmethod
    def load(cls, path: str | Path):
        # Read only the expected members, never extract paths from an archive.
        with zipfile.ZipFile(path) as archive:
            source = json.loads(archive.read("SOURCE.json"))
            if not isinstance(source, dict) or source.get("schema_version") != 1:
                raise ValueError("Unsupported metadata snapshot schema")
            for key in ("name", "url", "license", "license_url", "revision", "downloaded_at"):
                if not isinstance(source.get(key), str) or not source[key]:
                    raise ValueError(f"Missing metadata provenance: {key}")
            if not archive.read("LICENSE").strip():
                raise ValueError("Metadata snapshot must include its license")
            records = parse_aircraft(json.loads(archive.read("aircrafts.json")))
        return cls(records, source)

    def lookup(self, icao24: str) -> AircraftMetadata | None:
        key = icao24.upper()
        return self._records.get(key) if ICAO24.fullmatch(key) else None

    def __len__(self):
        return len(self._records)


def enrich_rows(rows: list[dict], provider: MetadataLookup) -> list[dict]:
    """Join at the presentation boundary; never write metadata into Aircraft."""
    enriched = []
    for row in rows:
        record = provider.lookup(row["icao"])
        enriched.append({**row, "external_metadata": {
            **asdict(record if record is not None else AircraftMetadata()),
            "matched": record is not None,
            "source": dict(provider.source) if provider.source is not None else None,
        }})
    return enriched
