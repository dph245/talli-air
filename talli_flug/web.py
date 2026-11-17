"""A server-rendered table and a small read-only JSON endpoint."""

from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import re
from pathlib import Path
from urllib.parse import urlsplit

from .state import AircraftStore
from .metadata import MetadataLookup, LocalAircraftMetadata, enrich_rows


def cell(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, float):
        value = f"{value:.5f}".rstrip("0").rstrip(".")
    return escape(str(value))


def metadata_notice(source: dict | None) -> str:
    if not source:
        return "<p>External aircraft metadata: no local database loaded.</p>"

    def link(text, url):
        if urlsplit(url).scheme not in ("http", "https"):
            return cell(text)
        return f'<a href="{escape(url, quote=True)}">{cell(text)}</a>'

    return (f'<p>External aircraft metadata from {link(source["name"], source["url"])}, '
            f'available under {link(source["license"], source["license_url"])}. '
            f'Snapshot downloaded: {cell(source["downloaded_at"])}. '
            'These database records are not receiver observations and may be outdated.</p>')


def valid_coordinate(value, limit):
    return type(value) in (int, float) and math.isfinite(value) and abs(value) <= limit


def dashboard_stats(rows, statuses, message_rate=None):
    rates = message_rate.snapshot() if message_rate is not None else {}
    return {
        "aircraft_count": len(rows),
        "positioned_aircraft_count": sum(
            valid_coordinate(row.get("latitude"), 90) and valid_coordinate(row.get("longitude"), 180)
            for row in rows),
        "receiver_count": len(statuses),
        "receivers_online": sum(bool(online) for online in statuses.values()),
        "message_rate_window_seconds": message_rate.window if message_rate is not None else 10.0,
        "messages_per_second": sum(rates.values(), 0.0),
        "receivers": [{"receiver_id": name, "connected": bool(online),
                       "messages_per_second": rates.get(name, 0.0)} for name, online in statuses.items()],
    }


def render(rows: list[dict], receiver_id: str, connected: bool, metadata_source=None,
           map_center=(51.0, 10.0), receiver_status=None, message_rate=None) -> str:
    """Render an initial snapshot, also usable without JavaScript."""
    statuses = receiver_status if receiver_status is not None else ({receiver_id: connected} if receiver_id else {})
    stats = dashboard_stats(rows, statuses, message_rate)
    map_data = json.dumps({"center": map_center, "aircraft": [
        {**{key: row.get(key) for key in ("icao", "callsign", "latitude", "longitude", "altitude",
                                        "speed", "track", "vertical_rate", "position_age_seconds",
                                        "df", "on_ground", "selected_altitude", "last_seen", "receiver_id",
                                        "latest_raw_frame", "receiver_metadata")},
         "external_metadata": {key: row.get("external_metadata", {}).get(key)
                               for key in ("registration", "type_designator")}}
        for row in rows]}, allow_nan=False).replace("<", "\\u003c")

    cards = []
    for receiver in stats["receivers"]:
        state = "online" if receiver["connected"] else "offline"
        label = "Online" if receiver["connected"] else "Offline (disconnected)"
        cards.append(f'<div class="receiver {state}"><strong>{cell(receiver["receiver_id"])}</strong>'
                     f'<span>{label}</span><span>{receiver["messages_per_second"]:.1f} frames/s</span></div>')
    body = []
    for row in rows:
        ground = row["on_ground"]
        metadata = row.get("external_metadata", {})
        values = [row["icao"], metadata.get("registration"), metadata.get("type_designator"), row["df"], row["callsign"],
                  None if ground is None else "Ground" if ground else "Airborne",
                  row["latitude"], row["longitude"], row["altitude"], row["selected_altitude"], row["speed"],
                  row["track"], row["vertical_rate"],
                  datetime.fromtimestamp(row["last_seen"], timezone.utc).isoformat(timespec="seconds"),
                  row["receiver_id"], row["latest_raw_frame"],
                  json.dumps(row["receiver_metadata"]) if row["receiver_metadata"] else None]
        link = f'<a href="/aircraft/{escape(row["icao"], quote=True)}">{cell(row["icao"])}</a>'
        body.append("<tr><td>" + link + "</td>" + "".join(f"<td>{cell(value)}</td>" for value in values[1:]) + "</tr>")
    if not body:
        body.append('<tr><td colspan="17">No current aircraft.</td></tr>')
    headings = ["ICAO", "Registration", "Type", "Latest DF", "Callsign", "State", "Latitude", "Longitude",
                "Baro altitude (ft)", "Selected altitude (ft)", "Ground speed (kt)", "Track (°)",
                "Vertical rate (ft/min)", "Last seen (UTC)", "Latest receiver", "Latest raw frame", "Latest metadata"]
    sort_keys = {"ICAO": "icao", "Baro altitude (ft)": "altitude", "Last seen (UTC)": "last_seen"}
    header_cells = []
    for heading in headings:
        key = sort_keys.get(heading)
        if key:
            selected = ' aria-sort="ascending"' if key == "icao" else ''
            indicator = ' ▲' if key == "icao" else ''
            header_cells.append(f'<th scope="col"{selected}><button type="button" data-sort="{key}">'
                                f'{heading}<span aria-hidden="true">{indicator}</span></button></th>')
        else:
            header_cells.append(f'<th scope="col">{heading}</th>')
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Talli-Flug · AirScope</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
 integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<script defer src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
 integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<script defer src="/static/aircraft.js"></script>
<style>
html {{color-scheme:dark;}}
body {{font:14px system-ui,sans-serif; margin:0; color:#e4ebf5; background:#10151d;}}
main {{max-width:100rem; margin:auto; padding:clamp(.75rem,2vw,1.75rem);}}
h1, h2, p {{margin:0;}}
h1 {{font-size:clamp(1.4rem,4vw,2rem);}}
h2 {{font-size:1.1rem;}}
section {{margin-block:1rem;}}
.muted {{color:#b3c0d2; margin-block:.45rem;}}
a, .leaflet-container .leaflet-popup-content a {{color:#8bd7ff;}}
table {{border-collapse:collapse; width:100%;}}
th, td {{padding:.5rem .65rem; border-bottom:1px solid #303e50; white-space:nowrap; text-align:left;}}
th {{background:#243246;}}
tbody tr:nth-child(even) {{background:#1c2838;}}
.table {{overflow-x:auto; border:1px solid #303e50; border-radius:.5rem;}}
#map {{height:clamp(400px,60vh,740px); width:100%; border-radius:.5rem; background:#1a2534; color:#e4ebf5;}}
.leaflet-popup-content-wrapper, .leaflet-popup-tip {{background:#1a2534; color:#e4ebf5;}}
.aircraft-icon svg {{display:block;width:24px;height:24px;}}
#kpi, #receivers {{display:grid; gap:.65rem; margin-block:.65rem;}}
#kpi {{grid-template-columns:repeat(4,minmax(0,1fr));}}
#receivers {{grid-template-columns:repeat(auto-fill,minmax(min(100%,11rem),1fr));}}
.kpi, .receiver {{background:#1a2534; padding:.7rem .85rem; border:1px solid #303e50; border-radius:.5rem;}}
.kpi h2 {{font-size:.85rem; color:#b3c0d2;}}
.kpi p {{margin-top:.2rem; font-size:1.45rem; font-weight:650; font-variant-numeric:tabular-nums; color:#8bd7ff;}}
.receiver {{display:grid; gap:.2rem; overflow-wrap:anywhere; border-inline-start:3px solid #f4ad91;}}
.receiver.online {{border-inline-start-color:#7cdeb4;}}
#refresh-status {{color:#ffbf9f; min-height:1.4em;}}
.search {{display:flex; flex-wrap:wrap; align-items:center; gap:.5rem; margin-block:.6rem;}}
#search-box {{box-sizing:border-box; width:min(100%,24rem); padding:.5rem; background:#1a2534;
    color:inherit; border:1px solid #65768d; border-radius:.35rem; font:inherit;}}
th button {{color:inherit; background:none; border:0; padding:0; cursor:pointer; font:inherit;}}
:focus-visible {{outline:2px solid #8bd7ff; outline-offset:2px;}}
@media (max-width:640px) {{
    #kpi {{grid-template-columns:repeat(2,minmax(0,1fr));}}
    #map {{height:clamp(250px,48vh,420px);}}
}}
</style></head>
<body><main><header><h1>AirScope · Talli-Flug</h1><p class="muted">ADS-B monitoring · Updates every 5 seconds</p>
<p id="refresh-status" role="status"></p><noscript>Live updates require JavaScript.</noscript></header>
<section id="kpi" aria-label="Live statistics">
  <div class="kpi"><h2>Active aircraft</h2><p id="kpi-active">{stats['aircraft_count']}</p></div>
  <div class="kpi"><h2>Aircraft with position</h2><p id="kpi-positioned">{stats['positioned_aircraft_count']}</p></div>
  <div class="kpi"><h2>Frames/s <span id="rate-window">({stats['message_rate_window_seconds']:g} s)</span></h2><p id="kpi-msg">{stats['messages_per_second']:.1f}</p></div>
  <div class="kpi"><h2>Receivers online / total</h2><p id="kpi-recv">{stats['receivers_online']} / {stats['receiver_count']}</p></div>
</section>
<section aria-labelledby="receivers-heading"><h2 id="receivers-heading">Receivers</h2>
<div id="receivers">{''.join(cards) or '<p>No receivers configured.</p>'}</div></section>
<section aria-labelledby="radar-heading"><h2 id="radar-heading">Live radar</h2>
<p class="muted">Map positions may be older than Last seen; select an aircraft for its position age. Markers without a known track are circles.</p>
<div id="map" aria-label="Current aircraft positions">Map requires JavaScript and Leaflet.</div></section>
<script id="map-data" type="application/json">{map_data}</script>
<section aria-labelledby="aircraft-heading"><h2 id="aircraft-heading">Aircraft</h2>
<p class="muted">— means unknown. Aircraft state combines observations from multiple frames;
individual values may be older than Last seen. Latest DF, receiver, raw frame, and metadata refer only to the latest accepted frame.</p>
<p class="muted">Registration and Type are external database metadata. Select an ICAO address for aircraft metadata, air data, and sources.</p>
<label class="search" for="search-box">Search aircraft <input type="search" id="search-box" placeholder="ICAO, callsign or registration" disabled></label>
<div class="table" role="region" aria-label="Aircraft table" tabindex="0">
<table><thead><tr>{''.join(header_cells)}</tr></thead>
<tbody id="aircraft-rows">{''.join(body)}</tbody></table></div></section>{metadata_notice(metadata_source)}</main></body></html>"""


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds") if timestamp is not None else None


def render_details(row: dict) -> str:
    metadata = row.get("external_metadata", {})
    metadata_source = metadata.get("source")
    external_rows = "".join(
        f"<tr><th>{label}</th><td>{cell(metadata.get(key))}</td></tr>"
        for key, label in (("registration", "Registration"), ("type_designator", "ICAO aircraft type"),
                           ("description", "Manufacturer / model description"), ("operator", "Operator")))
    metadata_status = "" if metadata.get("matched") else "<p>No matching local aircraft record is available.</p>"
    if metadata_source:
        external_rows += (f'<tr><th>Database revision</th><td>{cell(metadata_source["revision"])}</td></tr>'
                          f'<tr><th>Database published</th><td>{cell(metadata_source.get("published_at"))}</td></tr>')
    fields = [("selected_altitude", "Selected altitude (ft)"),
              ("selected_altitude_mcp", "MCP/FCU selected altitude (ft)"),
              ("selected_altitude_fms", "FMS selected altitude (ft)"),
              ("ias", "IAS (kt)"), ("tas", "TAS (kt)"), ("mach", "Mach"),
              ("magnetic_heading", "Magnetic heading (° magnetic)"), ("roll", "Roll (°)"),
              ("temperature", "Reported temperature (°C)"),
              ("reported_wind_speed", "Reported wind speed (kt)"),
              ("reported_wind_direction", "Reported wind direction (° true, from)")]
    rows = []
    for name, label in fields:
        observation = row["field_observations"].get(name, {})
        source = observation.get("source")
        if name == "selected_altitude" and row["selected_altitude_source"]:
            source = f'{source}; {row["selected_altitude_source"]}'
        values = [label, row[name], source, utc(observation.get("received_at")),
                  observation.get("receiver_id"), observation.get("raw_frame")]
        rows.append("<tr>" + "".join(f"<td>{cell(value)}</td>" for value in values) + "</tr>")
    derived = []
    for name, label in (("temperature", "Derived temperature (°C)"),
                        ("wind_speed", "Derived wind speed (kt)"),
                        ("wind_direction", "Derived wind direction (° true, from)")):
        estimate = row["derived"].get(name)
        derived.append(f"<h3>{label}: {cell(estimate['value'] if estimate else None)}</h3>")
        if estimate:
            derived.append(f'<p>{cell(estimate["method"])}</p>')
            if "declination" in estimate:
                derived.append(f'<p>Modelled magnetic declination: {cell(estimate["declination"])}° east.</p>')
            derived.append('<table><tr><th>Input</th><th>Value</th><th>Source</th><th>Received (UTC)</th><th>Receiver</th><th>Source frame</th></tr>')
            for key, observation in estimate["inputs"].items():
                values = [key, observation["value"], observation["source"], utc(observation["received_at"]),
                          observation["receiver_id"], observation["raw_frame"]]
                derived.append("<tr>" + "".join(f"<td>{cell(value)}</td>" for value in values) + "</tr>")
            derived.append("</table>")
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Talli-Flug — {cell(row["icao"])}</title>
<style>body {{font: 14px system-ui, sans-serif; margin: 24px; color: #222; background: white}}
table {{border-collapse: collapse}} th, td {{padding: 6px 10px; border: 1px solid #bbb; text-align: left}}
.table {{overflow-x: auto}}</style></head><body>
<p><a href="/">Aircraft</a> · <a href="/aircraft/{cell(row["icao"])}">Refresh details</a></p>
<h1>{cell(row["icao"])} {cell(row["callsign"])}</h1>
<p>Latest observations from multiple frames; values may be older than Last seen ({cell(utc(row["last_seen"]))}).
This details snapshot does not refresh automatically. — means unknown or unavailable.</p>
<h2>External aircraft metadata</h2>{metadata_status}
<table>{external_rows}</table>{metadata_notice(metadata_source)}
<h2>Decoded reports</h2><div class="table"><table><tr><th>Field</th><th>Value</th><th>Source</th><th>Received (UTC)</th><th>Receiver</th><th>Source frame</th></tr>
{''.join(rows)}</table></div><h2>Calculated estimates</h2>
<p>Calculated values are not directly transmitted weather observations. Inputs must come from the same receiver,
be at most 10 seconds old, and be no more than 5 seconds apart. Wind also requires straight, approximately level flight,
a position, geometric altitude, and a valid magnetic model. Missing estimates mean these checks were not met;
wind below 1 kt has no reported direction.</p><div class="table">{''.join(derived)}</div></body></html>'''


def make_server(address: tuple[str, int], store: AircraftStore, receiver_id: str,
                connected, metadata: MetadataLookup | None = None,
                map_center=(51.0, 10.0), receiver_status=None,
                message_rate=None) -> ThreadingHTTPServer:
    """Create the HTTP server.

    ``message_rate`` is an optional :class:`MessageRate` instance used to
    populate the ``/api/stats`` endpoint.  If ``None`` the endpoint returns a
    minimal structure with zero rates.
    """
    provider = metadata if metadata is not None else LocalAircraftMetadata()

    def snapshot():
        return enrich_rows(store.snapshot(), provider)

    def statuses():
        if receiver_status is not None:
            return receiver_status()
        return {receiver_id: connected()} if receiver_id else {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                payload = render(snapshot(), receiver_id, False, provider.source, map_center,
                                 statuses(), message_rate).encode()
                content_type = "text/html; charset=utf-8"
            elif path == "/static/aircraft.js":
                payload = (Path(__file__).parent / "static" / "aircraft.js").read_bytes()
                content_type = "text/javascript; charset=utf-8"
            elif path == "/api/aircraft":
                payload = json.dumps(snapshot(), allow_nan=False).encode()
                content_type = "application/json"
            elif path == "/api/stats":
                payload = json.dumps(dashboard_stats(store.snapshot(), statuses(), message_rate),
                                     allow_nan=False).encode()
                content_type = "application/json"
            elif re.fullmatch(r"/aircraft/[0-9A-Fa-f]{6}", path):
                icao = path.rsplit("/", 1)[1].upper()
                row = next((row for row in snapshot() if row["icao"] == icao), None)
                if row is None:
                    self.send_error(404, "Aircraft not current")
                    return
                payload = render_details(row).encode()
                content_type = "text/html; charset=utf-8"
            elif path == "/healthz":
                payload, content_type = b"ok\n", "text/plain"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    return ThreadingHTTPServer(address, Handler)
