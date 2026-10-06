# Talli-Flug

A small, self-contained Python application that reads an existing TCP Mode-S /
ADS-B receiver and shows current aircraft in a plain web table. No SDR access,
database, aircraft lookup, frontend build, or external web services are required.

## Start with Docker

```sh
docker compose up -d --build
```

Open <http://localhost:8080>. Compose defaults to receiver `192.168.88.88:47806`
with ID `receiver-1`. The container needs ordinary outbound TCP access to that
address; no privileged mode, host networking, or device access is used.

Optional: copy `.env.example` to `.env` and change the values before starting.
Use `docker compose logs -f` for connection logs and `docker compose down` to stop.

## Configuration

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `RECEIVER_HOST` | `192.168.88.88` in Compose; required otherwise | TCP receiver host |
| `RECEIVER_PORT` | `47806` | TCP receiver port |
| `RECEIVER_ID` | `receiver-1` | ID carried on every parsed frame |
| `RECONNECT_SECONDS` | `5` | Delay between connection attempts |
| `RECEIVER_IDLE_SECONDS` | `60` | Reconnect if no bytes arrive for this long |
| `AIRCRAFT_TTL_SECONDS` | `60` | Remove aircraft after this many seconds without an accepted frame |
| `HTTP_PORT` | `8080` | Compose's published host port |
| `WEB_HOST`, `WEB_PORT` | `0.0.0.0`, `8080` | Python HTTP bind address; Compose uses these defaults internally |
| `SURFACE_LAT`, `SURFACE_LON` | unset | Optional known reference coordinates for surface CPR; set both |

The surface reference must be near the aircraft (within approximately 45 NM).
Leave it unset unless the location is known. Airborne positions do not need a
receiver location. The host/port default lives in Compose, not receiver logic.

## Architecture and behavior

* `input.py`: reconnecting TCP reader, bounded line framing, and immutable frames
  carrying receiver ID, raw hex, metadata fields, UTC receipt time, and monotonic
  receipt time. Fragmented/coalesced TCP chunks and CRLF/LF/CR lines are supported.
* `state.py`: [pyModeS 3.6.0](https://github.com/junzis/pyModeS) decodes protocol
  fields; a locked in-memory dictionary merges observations by ICAO. Latest
  even/odd CPR halves are retained per aircraft, receiver, and position family.
  Compatible halves at most 10 seconds apart are resolved using pyModeS's CPR
  functions. Expiration uses the monotonic clock, runs every second, and also
  runs before reads/updates. No state survives restart.
* `airdata.py`: strict Comm-B candidate filtering, additional ADS-B fields, and
  time-aligned temperature/wind estimates. Bundled WMM2025 coefficients from
  pygeomag correct magnetic heading; no model download or external API is used.
* `web.py`: standard-library HTTP server with a server-rendered table, refreshed
  every five seconds. `/api/aircraft` provides the same state as JSON (`null` for
  unknown fields); `/healthz` checks HTTP liveness, not receiver connectivity.
* `__main__.py`: receiver worker, HTTP worker, expiration, and graceful shutdown.
  The input callback boundary and receiver-tagged CPR cache allow another reader
  to feed the same store later. This MVP configures one receiver.

The table shows ICAO, latest DF, callsign, airborne/ground state, latitude/longitude,
barometric and selected altitude in feet, ground speed in knots, true track in degrees,
vertical rate in feet/minute, last seen in UTC, last receiver, latest raw frame,
and that frame's metadata. Vertical rate may use a barometric or GNSS source;
it is not necessarily the derivative of the displayed barometric altitude.
Unknown values appear as `—`. A frame updates only the fields it reports;
an explicitly unavailable field clears that value. Aircraft state combines
observations from multiple frames: the latest DF, receiver, raw frame, and metadata
describe only the latest accepted frame, not the source of every displayed value.
The JSON keys `df`, `receiver_id`, `latest_raw_frame`, and `receiver_metadata`
have the same latest-frame meaning; `last_seen` is that frame's UTC receipt time.
Ground/air transitions clear old position and motion fields before applying the
new observation. Select an ICAO address to open `/aircraft/ICAO` for secondary
air data, source frames, UTC receipt times, and separately labelled calculated
estimates. Details refresh manually so they remain readable.

Internally, each state field has its own UTC and monotonic receipt timestamps
in `Aircraft.observations`. Unrelated frames leave these timestamps unchanged;
repeated observations refresh them even when the value is unchanged. Explicitly
unavailable reports record their receipt time too. Transition-invalidated fields
lose their timestamps until observed again. Position timestamps use the newer
half of a successfully resolved CPR pair; unpaired or rejected position messages
do not refresh them. These are local receipt times, not aircraft transmission
times. Observations also retain each value, receiver ID, source description, and
raw source frame. `/api/aircraft` exposes these as `field_observations` (excluding
monotonic clock values). `derived` contains only currently eligible estimates,
their methods, and copies of the exact inputs with provenance.

## Additional air data

| Value | Directly decoded source |
| --- | --- |
| Selected altitude | ADS-B TC29 subtype 1; uniquely inferred Comm-B BDS 4,0 |
| IAS | ADS-B TC19 subtype 3/4 when marked IAS; BDS 6,0 |
| TAS | ADS-B TC19 subtype 3/4 when marked TAS; BDS 5,0 |
| Mach, magnetic heading | BDS 6,0 |
| Roll, track rate, ground speed, true track | BDS 5,0 |
| Reported static temperature | BDS 4,4 or 4,5 |
| Reported wind | BDS 4,4 (separate from calculated wind) |

For BDS 4,0, both MCP/FCU and FMS reports are retained. The main table uses the
explicitly named target if available, otherwise MCP/FCU, then FMS; details show
the chosen source. This fallback is a reported selection, not a claim that the
aircraft is following that target. ADS-B uses its explicit source flag. Other
TC29 subtypes are ignored because the library applies the subtype-1 layout to
all subtypes. TC19 heading is not labelled magnetic without confirmed reference
context; magnetic heading currently comes from BDS 6,0.

Comm-B additions require a CRC-valid ADS-B address observation within 60 seconds.
pyModeS validates status bits, reserved bits, and field ranges with meteorological
inference enabled. **Exactly one candidate must remain.** Ambiguous/unidentified
payloads are discarded even if the library chooses a preferred register. Its
`known=` scoring only reorders candidates, so it is not treated as proof. BDS 4,4
also needs a valid nonzero FOM/source. Recognized status-unavailable fields clear
the corresponding value. Header altitude and latest-frame metadata remain
independent of whether the Comm-B payload was accepted. Inference is conservative
but still heuristic, not an explicit transmitted BDS identifier.

Calculated estimates are separate from directly reported weather:

* **Temperature:** `T = (TAS_m/s / Mach)^2 / (1.4 × 287.05) − 273.15` °C.
  Requires TAS 100–600 kt, Mach 0.3–1.0 and a result between −80 and +60 °C.
  No standard-atmosphere temperature is substituted for missing inputs.
* **Wind:** subtract the true-heading/TAS air vector from the true-track/ground
  speed vector. Magnetic heading is corrected using WMM2025 at the aircraft's
  position, geometric altitude, and observation date. Geometric altitude comes
  from ADS-B GNSS altitude or barometric altitude plus ADS-B GNSS-minus-baro.
  Pressure altitude alone is insufficient. Direction is where the wind comes
  **from**, in degrees true; below 1 kt its direction is left unknown.

Both estimates require airborne state, one receiver, inputs at most **10 seconds
old** and no more than **5 seconds apart**, checked on the monotonic clock. Wind
additionally requires |roll| ≤5°, |track rate| ≤1°/s, |vertical rate| ≤1,000 ft/min,
TAS 100–600 kt and ground speed 100–700 kt; estimates above 200 kt are rejected.
No wind is emitted in magnetic warning zones or outside WMM2025's 2025–2030
validity interval. Estimates are recalculated on reads and disappear as inputs
age out, even if unrelated frames keep the aircraft active. Direct reports stay
as last observations with their actual timestamps until aircraft expiration.
Quantization, heading/model error, sideslip, and aircraft instrument biases
limit estimate accuracy; these are not calibrated meteorological measurements.

Protocol/derivation references: [pyModeS inference behavior](https://github.com/junzis/pyModeS/blob/main/docs/quickstart.md),
[Mode-S EHS registers](https://mode-s.org/1090mhz/content/mode-s/7-ehs.html),
[meteorological registers](https://mode-s.org/1090mhz/content/mode-s/8-meteo.html),
[TAS/Mach temperature relation](https://amt.copernicus.org/preprints/amt-2024-110/amt-2024-110-manuscript-version6.pdf),
and [WMM calculation and validity](https://pygeomag.readthedocs.io/en/latest/api.html).

See [the live-data report](docs/airdata-validation.md) for observed registers,
unavailable fields, and examples from the configured receiver.

## Extended AVR input

Observed example supplied for this project:

```text
*8D440DA5F82300030049B8930905;FE3418B8;06;057A;
```

The first semicolon-delimited field is `*` followed by a 56-bit (14 hex digits)
or 112-bit (28 hex digits) Mode-S frame. The remaining fields are receiver
metadata. **Their semantics are unknown**: no timestamp, signal level, channel,
or other meaning is assigned. The parser preserves their order, spelling,
leading zeros, and empty fields as strings, separately from uppercase raw hex.
Normal `*HEX;` lines with no metadata also work. The final semicolon is required.
Live inspection of the configured receiver confirmed CRLF line termination and
the same extended format, including short frames.

Malformed/non-ASCII records, mismatched DF/length, and lines exceeding 4096 bytes
are discarded. An oversized record is skipped through the next line delimiter.
Incomplete records are discarded on disconnect. Only complete newline-terminated
records are consumed; semicolons alone cannot delimit records with unknown
metadata field counts. Raw Beast binary and timestamp-prefixed AVR are unsupported.

## Decode limits

* CRC-invalid ADS-B is rejected. DF17 and DF18 control field 0 are supported;
  other DF18 control fields (including non-ICAO/rebroadcast variants) are ignored.
* DF11 all-call replies are accepted only with a parity remainder consistent
  with the 7-bit interrogator overlay. This is weaker error detection than ADS-B.
* Address/parity replies (DF0/4/5/16/20/21) update only an already active ICAO
  learned from explicit-address frames. An isolated Mode-S-only aircraft may
  therefore be absent. These replies cannot independently validate their ICAO.
* Only uniquely inferred supported Comm-B payloads update additional fields.
  GNSS height, airspeed, and heading are not substituted for barometric altitude,
  ground speed, or track. Unsupported frames do not update the aircraft table.
* CPR uses local receipt times, not unexplained receiver metadata. Buffered or
  replayed feeds can defeat the timing assumption. No extrapolation, local
  single-frame CPR, MLAT, or additional motion plausibility filter is applied.
  Positions need a compatible fresh pair; surface pairs also need a reference.
* Direct values are latest observations and may be older than the aircraft's
  last-seen time. Whole-aircraft expiration and derived-input age checks are
  implemented. The latest frame/metadata and one source observation per field
  are retained; the running application has no history or raw-frame archive.
* This is a small trusted-network HTTP application, without TLS or accounts.

## Local development and tests

Python 3.13 is used by Docker. To run locally:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
RECEIVER_HOST=192.168.88.88 .venv/bin/python -m talli_flug
```

Run tests and build validation:

```sh
.venv/bin/python -m pytest -q
docker compose build
```

Tests cover extended AVR metadata, 56/112-bit frames, malformed and oversized
input, TCP fragmentation and reconnection, state merging, CRC rejection,
airborne CPR in both arrival orders, stale/cross-receiver/incompatible CPR
halves, surface reference handling, ground transitions, expiration, configuration,
HTML escaping, and HTTP/JSON output. Tests use supplied/reference frames and
explicitly synthetic CRC-correct messages for protocol edge cases. Network tests
use only local ephemeral ports and do not require the real receiver.
Additional tests cover known BDS 4,0/5,0/6,0/4,4 frames, an explicitly synthetic
BDS 4,5 example, ambiguous BDS 5,0/6,0 inference, invalid meteorological source,
ADS-B target/airspeed reports, observed live wind inputs, stale/incompatible
inputs, model validity, wind direction convention, and details rendering.
