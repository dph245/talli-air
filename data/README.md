# Bundled aircraft metadata

`aircraft-metadata.zip` contains information from the
[Mictronics aircraft database](https://github.com/Mictronics/aircraft-database),
made available under the [ODC Attribution License (ODC-By) 1.0](https://opendatacommons.org/licenses/by/1-0/).
The complete upstream license is retained as `LICENSE` inside the archive.
Keep this notice and the archive's license/provenance when redistributing.

The initial bundled snapshot uses upstream revision
`3f80a2d7c8a170352e03e29f2e8c9bd77e00d5db` (published 2026-10-04), database
version 525, downloaded 2026-10-06. It has 451,880 records.
After an update, `SOURCE.json` inside the ZIP is the authoritative revision and
record-count record; the updater prints it after installation.

The bulk export is `indexedDB_old.zip` from the publisher's own repository.
Its `aircrafts.json` and `LICENSE` are unchanged. Unused type-classification and
operator-code files were omitted, and `SOURCE.json` was added with source URLs,
revision, version, publication/download times, record count, and original export
SHA-256. The metadata snapshot is separate from the application software and
from live receiver data. No third-party merged aircraft database is included.

See [update instructions and limitations](../docs/aircraft-metadata.md).
