# 1090 MHz laboratory storage (schema 1)

Enable with `LAB_DB_PATH`; an empty value disables all lab activity. Compose
stores the database and its WAL on the existing `/var/lib/airscope` volume,
without making the container root filesystem writable. Run only one service
instance per database.

## Ingestion and reliability

The receiver submits one compact event after Mode-S processing; it contains no
payload. The UTC minute is assigned from the frame's monotonic receipt time
mapped to epoch time at process startup. Writer delays and wall clock steps do
not move frames across windows. Keep the host clock synchronized before launch;
clock adjustments during a run intentionally do not retime the measurement.

The frame queue defaults to 8192 entries (about 13 seconds at 600 frames/s).
Producers use `put_nowait`; their short memory-only critical sections never
hold a SQLite lock. A separate bounded transition buffer preserves reconnects
when only the frame queue is full. If that buffer also overflows, the affected
connection interval is unknown; the writer resumes from the latest known state.
Loss counters are transferred separately even with no frames in the queue.

Only the background thread opens SQLite. It aggregates up to 2048 frames per
transaction and checkpoints coverage about every 200 ms while idle. An in-flight
TCP chunk can delay completion of its minute. SQLite lock waits and retries are
confined to the writer. A failed batch is retained for retry, and affected
minutes are marked `database_gap` on recovery even if all events were recovered.
Startup failure is retried without stopping live reception. With persistent
failure or abrupt process termination, memory-only events and counters cannot
be recovered; absence of a row is not evidence of zero. Shutdown drains queued
events when possible, with a three-second join budget; failures are logged.

## Minute metrics

The key is `(run_id, receiver_id, minute_utc)`; a restart can produce separate
partial rows in the same minute. Do not collapse them without checking coverage.
`minute_utc` is the Unix epoch second at the UTC minute start, inclusive; the
next minute start is exclusive.

| Column | Meaning |
| --- | --- |
| `observed_seconds` | Duration covered by this process in the minute, irrespective of receiver connection. |
| `online_seconds` | Seconds of recorded TCP connection within coverage; zero means offline in that coverage, `NULL` means a connection interval is unknown. |
| `framed` | Retained Mode-S frames emitted by Beast/AVR framing, before decoder acceptance. Repeated receptions count once each, including across receivers. |
| `accepted` | Frames accepted by `AircraftStore`, including frames contributing receiver provenance. |
| `cpr` | New successful CPR resolutions: a duplicate newer half with identical CPR coordinates and no newer opposite half does not count again. Live position updates are unchanged. |
| `no_position` | Retained frames without a new geographic CPR result from that observation, including rejected frames and retries of an already resolved pair. Existing aircraft coordinates do not count. |
| `dropped` | Queue-discarded frame or connection events attributed to this receiver and minute; not included in frame counters. |
| `complete` | One only for a closed, fully covered UTC minute with known connection duration and no known queue or database gap. Other counts are partial when zero. |
| `database_gap` | A writer failure intersected the minute; not a count of lost frames. |

An online but silent receiver has positive online time and zero retained frames.
Mode A/C and Beast status are excluded from these metrics and the live Mode-S
rate. Duration uses TCP connectivity, not continuous RF observability. No
geographic measurements, raw frames, calibrated RSSI or cross-receiver matching
are persisted by the new writer.

## Schema, retention and migration

- `runs` and `receivers` retain run start and receiver configuration, including
  location and reception-chain label. `measurement_runs` records the last
  committed coverage boundary and normal writer end time.
- `receiver_sessions` records reconnect intervals. An interrupted session is
  marked on next startup without inventing an end time. A connection whose
  end was lost also keeps a `NULL` end. Time indexes support receiver queries.
- `receiver_minutes` has receiver/time and retention indexes. The writer
  deletes at most 2048 minute rows per second below the minute containing the
  60-day cutoff. SQLite may reuse freed pages without shrinking the file.
  Session and run metadata are not subject to minute retention.
- Schema 0 from the previous sprint is upgraded additively in a transaction via
  `PRAGMA user_version`; legacy `observations` and `connections` are not altered.
  They lack sufficient coverage evidence to reconstruct reliable minute rows.
  Before rollout, make an SQLite-consistent backup (SQLite backup API, or stop
  the service before copying after a checkpoint). Retain legacy raw data in
  that archive; if necessary to reclaim volume space, use a fresh database
  path after verifying the archive. Do not delete the live WAL or old tables
  as part of the migration. Unknown schema versions are not migrated.

## Synthetic load test

Run `PYTHONPATH=. .venv/bin/python tools/benchmark_laboratory.py`. Two loopback
Beast streams each offer 300 frames/s for 10 seconds, first without and then
with storage. Results include delivered frames, persisted totals, loss,
sampled queue occupancy, process CPU time and flush duration. Effective writer
throughput is the achieved rate under this offered load, not maximum capacity.
The fixture repeats one ADS-B payload and is not representative of diverse
traffic or evidence of real RF reception quality.
