"""A server-rendered table and a small read-only JSON endpoint."""

from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
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


def render(rows: list[dict], receiver_id: str, connected: bool, metadata_source=None,
           map_center=(51.0, 10.0)) -> str:
    map_data = json.dumps({"center": map_center, "aircraft": [
        {key: row.get(key) for key in ("icao", "callsign", "latitude", "longitude", "altitude",
                                     "speed", "track", "vertical_rate", "position_age_seconds")}
        for row in rows]}, allow_nan=False).replace("<", "\\u003c")
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
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<noscript><meta http-equiv="refresh" content="5"></noscript><title>Talli-Flug</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
 integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<script defer src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
 integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<script defer src="/static/aircraft.js"></script>
<style>body {{font: 14px system-ui, sans-serif; margin: 24px; color: #222; background: white}}
table {{border-collapse: collapse}} th, td {{padding: 6px 10px; border: 1px solid #bbb; text-align: left; white-space: nowrap}}
.table {{overflow-x: auto}} h1 {{font-size: 24px}}
#map {{height: 280px; max-width: 900px; margin: 16px 0; background: #eee}}
.aircraft-icon svg {{display: block; width: 24px; height: 24px}}
</style></head>
<body><h1>Talli-Flug</h1><p id="receiver-status">Receiver {escape(receiver_id)}: {"connected" if connected else "disconnected; retrying"}.</p>
<p>Refreshes every 5 seconds. — means unknown. Aircraft state combines observations from multiple frames;
individual values may be older than Last seen. Latest DF, receiver, raw frame, and metadata refer only to the latest accepted frame.</p>
<p>Registration and Type are external database metadata. Select an ICAO address for aircraft metadata, air data, and sources.</p>
<div id="map" aria-label="Current aircraft positions">Map requires JavaScript and Leaflet.</div>
<p>Map positions may be older than Last seen; select an aircraft for its position age. Markers without a known track are circles.</p>
<p id="refresh-status" role="status"></p>
<script id="map-data" type="application/json">{map_data}</script>
<div class="table"><table><thead><tr>{''.join(f'<th scope="col">{escape(h)}</th>' for h in headings)}</tr></thead>
<tbody>{''.join(body)}</tbody></table></div>{metadata_notice(metadata_source)}</body></html>"""


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
                map_center=(51.0, 10.0)) -> ThreadingHTTPServer:
    provider = metadata if metadata is not None else LocalAircraftMetadata()

    def snapshot():
        return enrich_rows(store.snapshot(), provider)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                payload = render(snapshot(), receiver_id, connected(), provider.source, map_center).encode()
                content_type = "text/html; charset=utf-8"
            elif path == "/static/aircraft.js":
                payload = (Path(__file__).parent / "static" / "aircraft.js").read_bytes()
                content_type = "text/javascript; charset=utf-8"
            elif path == "/api/aircraft":
                payload = json.dumps(snapshot(), allow_nan=False).encode()
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
