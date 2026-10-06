"""Explicit bulk refresh from Mictronics; never imported by the running app.

Run from the repository root: python3 tools/update_aircraft_metadata.py
"""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.request import Request, urlopen
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from talli_flug.metadata import DEFAULT_DATABASE, LocalAircraftMetadata, parse_aircraft

REPOSITORY = "https://github.com/Mictronics/aircraft-database"


def download(url):
    request = Request(url, headers={"User-Agent": "Talli-Flug-metadata-updater"})
    with urlopen(request, timeout=120) as response:
        return response.read()


def make_snapshot(raw: bytes, revision: str, published_at: str, downloaded_at: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(raw)) as upstream:
        aircraft_bytes = upstream.read("aircrafts.json")
        count = len(parse_aircraft(json.loads(aircraft_bytes)))
        license_bytes = upstream.read("LICENSE")
        if b"ODC Attribution License" not in license_bytes:
            raise ValueError("Upstream license changed; review before redistributing")
        version = json.loads(upstream.read("dbversion.json"))["version"]
    source = {
        "schema_version": 1,
        "name": "Mictronics aircraft database",
        "url": REPOSITORY,
        "license": "ODC-By 1.0",
        "license_url": "https://opendatacommons.org/licenses/by/1-0/",
        "revision": revision,
        "published_at": published_at,
        "downloaded_at": downloaded_at,
        "database_version": version,
        "records": count,
        "archive_url": f"https://raw.githubusercontent.com/Mictronics/aircraft-database/{revision}/indexedDB_old.zip",
        "upstream_sha256": hashlib.sha256(raw).hexdigest(),
        "changes": "Retained unmodified aircrafts.json and LICENSE; omitted unused files; added provenance.",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("aircrafts.json", aircraft_bytes)
        archive.writestr("LICENSE", license_bytes)
        archive.writestr("SOURCE.json", json.dumps(source, indent=2) + "\n")
    return output.getvalue()


def install_snapshot(content: bytes, destination: Path):
    """Validate completely before atomically replacing the last usable snapshot."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".zip", delete=False) as temp:
        temp_path = Path(temp.name)
        temp.write(content)
    try:
        database = LocalAircraftMetadata.load(temp_path)
        temp_path.chmod(0o644)  # Container runs as an unprivileged user.
        os.replace(temp_path, destination)
        return database.source
    finally:
        temp_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", help="Full upstream commit SHA; defaults to current main")
    parser.add_argument("--output", type=Path, default=DEFAULT_DATABASE)
    args = parser.parse_args()
    if args.revision and not re.fullmatch(r"[0-9a-fA-F]{40}", args.revision):
        parser.error("--revision must be a full 40-character commit SHA")
    commit = json.loads(download(
        "https://api.github.com/repos/Mictronics/aircraft-database/commits/" + (args.revision or "main")))
    revision = commit["sha"]
    raw = download(f"https://raw.githubusercontent.com/Mictronics/aircraft-database/{revision}/indexedDB_old.zip")
    content = make_snapshot(raw, revision, commit["commit"]["committer"]["date"],
                            datetime.now(timezone.utc).isoformat())
    source = install_snapshot(content, args.output)
    print(json.dumps(source, indent=2))
    print(f"Updated {args.output}. Restart the application (rebuild Docker) to load it.")


if __name__ == "__main__":
    main()
