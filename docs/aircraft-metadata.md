# Local aircraft metadata

Registration, ICAO aircraft type, manufacturer/model description, and any operator
record are **external database information**, not Mode-S/ADS-B observations.
The main table adds only Registration and Type. The details page groups longer
information under “External aircraft metadata”, with attribution and revision.
Unknown fields display `—`; they are never inferred from callsigns or ADS-B
emitter categories.

## Source and license

The bundled source is the [Mictronics aircraft database](https://github.com/Mictronics/aircraft-database),
whose [publisher README](https://github.com/Mictronics/aircraft-database/blob/main/README.md)
explicitly licenses its exports under [ODC-By 1.0](https://opendatacommons.org/licenses/by/1-0/).
The direct `indexedDB_old.zip` export is used, pinned to a commit; no merged
tar1090/ADS-B Exchange database is used. The archive includes the publisher's full
license plus a `SOURCE.json` manifest with URL, revision, version, publication and
download dates, original export SHA-256, and record count. UI attribution and
API provenance travel with displayed metadata. Preserve those notices when
redistributing the data or presenting enriched API output.

The current [bundled snapshot](../data/README.md) has 451,880 aircraft records.
It is included in Docker, so `docker compose up -d --build` needs no separate
metadata download, service, credential, or persistent database server.

## Field mapping and limits

| Export field | Application field | Meaning |
| --- | --- | --- |
| ICAO24 dictionary key | lookup key | Exactly six hex digits, normalized uppercase |
| `r` | `registration` | Published registration |
| `t` | `type_designator` | Published ICAO type, e.g. A320, A20N, A359, B77W |
| `d` | `description` | Published manufacturer/model text, retained as one description |
| none | `operator` | Unknown in this snapshot |

The aircraft export does not associate aircraft with operators. Its separate
operator-code directory does not provide that relationship, so it is not used
and **operator remains unknown**. Registered owner, callsign prefix, and aircraft
operator are not treated as interchangeable. The replaceable lookup interface
can return an operator when a future licensed source explicitly supplies one.

Coverage and descriptions are incomplete. Blank text becomes `null`; dubious
type-code syntax is not displayed. Some valid-looking codes are special purpose
or generic designators. No more specific model is inferred from a type code or
registration. Records can be stale, incorrect, reassigned, or absent for private,
military, newly registered, or misconfigured aircraft. Database download time is
not a per-aircraft verification time. A lookup match does not prove that a
received transmitter is the registered airframe.

## Runtime boundary and configuration

`talli_flug/metadata.py` provides the small `MetadataLookup` interface:
`lookup(icao24) -> AircraftMetadata | None` plus source provenance. The local
adapter reads the snapshot once at startup and performs dictionary lookups.
There are **no runtime network requests** for enrichment, including misses.

The web/API layer joins metadata into an `external_metadata` object. Aircraft
state, receiver metadata, last-seen times, observation timestamps, and derivation
inputs remain untouched. `external_metadata.source` describes the database,
not a receiver. `matched: false` means no record matched; `source: null` means no
database is loaded. Unknown values are JSON `null`.

`AIRCRAFT_METADATA_PATH` selects a local snapshot ZIP. Its default is
`data/aircraft-metadata.zip` relative to the application source directory
(`/app/data/aircraft-metadata.zip` in Docker). Set it to an empty string to disable
enrichment. A missing or invalid snapshot logs a warning and leaves the receiver
and web interface operational with unknown metadata. A custom adapter can replace
the source without changing input or decoding code; the bundled ZIP reader itself
expects the documented Mictronics format and provenance manifest.

For a custom local snapshot in Compose, use an override with a read-only bind:

```yaml
services:
  talli-flug:
    volumes:
      - ./my-aircraft-metadata.zip:/app/data/custom.zip:ro
    environment:
      AIRCRAFT_METADATA_PATH: /app/data/custom.zip
```

## Updates

Updates are explicit bulk downloads, never triggered by aircraft reception.
The publisher describes weekly exports; refresh when appropriate for your use:

```sh
python3 tools/update_aircraft_metadata.py
docker compose up -d --build
```

The updater resolves the current upstream commit, downloads that exact revision,
validates the schema/license and all rows, retains the original aircraft JSON and
license, adds provenance, then atomically replaces the local snapshot. A failed
download or validation leaves the previous snapshot intact. Review the source
license when updating; the automated license-name check is not a complete review
of changed terms. The data is reloaded on application restart, not on every read.

To reproduce a particular snapshot or write a separate file:

```sh
python3 tools/update_aircraft_metadata.py \
  --revision 3f80a2d7c8a170352e03e29f2e8c9bd77e00d5db \
  --output data/aircraft-metadata.zip
```

The updater uses Python's standard library; it does not require the decoder's
dependencies. It accesses only the publisher's GitHub commit and bulk export.
There are no route, flight-number, airport, schedule, or commercial data APIs.
