# Air-data validation — 2026-10-06

A read-only capture of `192.168.88.88:47806` began at 18:19:03 UTC and ran for
60.05 seconds, receiving **2,945 frames**. Classification used pyModeS 3.6.0 with
meteorological validators enabled. Counts describe received frames and library
inference, not a guarantee of aircraft identity or independently verified BDS
registers. Application acceptance additionally requires recent CRC-valid ADS-B
corroboration. [Machine-readable counts and example frames](live-airdata-2026-10-06.json)
are retained; the runtime does not capture history.

| Message/register | Frames observed |
| --- | ---: |
| ADS-B TC4 identification | 14 |
| ADS-B TC11 / TC12 airborne position | 120 / 31 |
| ADS-B TC19 subtype 1 ground velocity | 152 |
| ADS-B TC29 subtype 1 target state | 51 |
| ADS-B TC31 subtype 0 operational status | 28 |
| Unique Comm-B BDS 1,0 / 1,7 / 2,0 | 47 / 26 / 48 |
| Unique Comm-B BDS 4,0 selected intention | 106 |
| Unique Comm-B BDS 5,0 track and turn | 76 |
| Unique Comm-B BDS 6,0 heading and speed | 113 |
| Unidentified Comm-B | 3 |
| Ambiguous Comm-B | 0 |
| BDS 4,4 / 4,5 meteorological reports | 0 / 0 |

Other observed downlink formats: DF0 (92), DF4 (393), DF5 (25), DF11 (1,614),
DF16 (6). ADS-B was DF17 (396); Comm-B used DF20 (360) and DF21 (59). No DF18
appeared in this window. The originally supplied `8D440DA5F82300030049B8930905`
is TC31 operational status; it does not itself contain the newly requested values.

Replaying the capture through the application produced selected altitude, IAS,
TAS, Mach, magnetic heading, and roll. Compatible observations also yielded:

* Derived temperature: aircraft `440823`, TAS 406 kt and Mach 0.676,
  approximately **−35.6 °C**. Sources were unique BDS 5,0 and 6,0 frames received
  at approximately 18:19:08.245 UTC.
* Derived wind: aircraft `484B32`, approximately **11.1 kt from 128.8° true**
  at 18:19:24.023 UTC. Inputs were no more than 4.87 seconds apart. WMM2025
  declination was +4.3285°; ground speed 460 kt, track 283.8867°, TAS 450 kt,
  and magnetic heading 278.9648°. The unmodified source sequence is an offline
  [test fixture](../tests/fixtures/live_wind.json), not application history.

These are estimates passing compatibility checks, not independent weather
validation. No directly reported temperature or wind can be claimed from this
capture. BDS 4,4 is covered by a published pyModeS example; BDS 4,5 is supported
through the library and tested with an explicitly synthetic status-gated payload.
ADS-B TC19 airspeed subtypes 3/4 and GNSS altitude TC20–22 did not appear in this
window. Wind used the observed barometric altitude plus GNSS-minus-baro instead.

No ambiguous message happened to appear live. The known ambiguous frame
`A8001EBCFFFB23286004A73F6A5B` matches BDS 5,0 and 6,0 in pyModeS and is rejected
by an offline regression test. See the [reference inference example](https://mode-s.org/1090mhz/content/mode-s/9-inference.html).
Absence in one minute does not establish that a register is never transmitted.

Validation completed with **73 passing tests** and a successful Docker build.
A separate temporary container connected to the live receiver at 18:28:48 UTC
and served two current aircraft during the check. All six new direct fields
(selected altitude, IAS, TAS, Mach, magnetic heading, roll) and all three derived
outputs (temperature, wind speed, wind direction) appeared. Main table, details,
JSON output, and graceful SIGTERM shutdown passed. The test container was removed.

Reproduce a finite diagnostic capture explicitly (the output path is required):

```sh
PYTHONPATH=. .venv/bin/python tools/probe_receiver.py \
  --host 192.168.88.88 --seconds 60 --output /tmp/talli-airdata-capture.json
```

The inspected checkout contains a plain table and no map implementation or map
assets. The existing page was retained; selected altitude and ICAO details links
were added without introducing a map or redesign.
