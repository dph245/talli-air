"""A server-rendered table and a small read-only JSON endpoint."""

from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from urllib.parse import urlsplit

from .state import AircraftStore


def cell(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, float):
        value = f"{value:.5f}".rstrip("0").rstrip(".")
    return escape(str(value))


def render(rows: list[dict], receiver_id: str, connected: bool) -> str:
    body = []
    for row in rows:
        ground = row["on_ground"]
        values = [row["icao"], row["df"], row["callsign"],
                  None if ground is None else "Ground" if ground else "Airborne",
                  row["latitude"], row["longitude"], row["altitude"], row["selected_altitude"], row["speed"],
                  row["track"], row["vertical_rate"],
                  datetime.fromtimestamp(row["last_seen"], timezone.utc).isoformat(timespec="seconds"),
                  row["receiver_id"], row["latest_raw_frame"],
                  json.dumps(row["receiver_metadata"]) if row["receiver_metadata"] else None]
        link = f'<a href="/aircraft/{escape(row["icao"], quote=True)}">{cell(row["icao"])}</a>'
        body.append("<tr><td>" + link + "</td>" + "".join(f"<td>{cell(value)}</td>" for value in values[1:]) + "</tr>")
    if not body:
        body.append('<tr><td colspan="15">No current aircraft.</td></tr>')
    headings = ["ICAO", "Latest DF", "Callsign", "State", "Latitude", "Longitude",
                "Baro altitude (ft)", "Selected altitude (ft)", "Ground speed (kt)", "Track (°)",
                "Vertical rate (ft/min)", "Last seen (UTC)", "Latest receiver", "Latest raw frame", "Latest metadata"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="5"><title>Talli-Flug</title>
<style>body {{font: 14px system-ui, sans-serif; margin: 24px; color: #222; background: white}}
table {{border-collapse: collapse}} th, td {{padding: 6px 10px; border: 1px solid #bbb; text-align: left; white-space: nowrap}}
.table {{overflow-x: auto}} h1 {{font-size: 24px}}</style></head>
<body><h1>Talli-Flug</h1><p>Receiver {escape(receiver_id)}: {"connected" if connected else "disconnected; retrying"}.</p>
<p>Refreshes every 5 seconds. — means unknown. Aircraft state combines observations from multiple frames;
individual values may be older than Last seen. Latest DF, receiver, raw frame, and metadata refer only to the latest accepted frame.</p>
<p>Select an ICAO address for air data, sources, and observation times.</p>
<div class="table"><table><thead><tr>{''.join(f'<th scope="col">{escape(h)}</th>' for h in headings)}</tr></thead>
<tbody>{''.join(body)}</tbody></table></div></body></html>"""


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds") if timestamp is not None else None


def render_details(row: dict) -> str:
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
<h2>Decoded reports</h2><div class="table"><table><tr><th>Field</th><th>Value</th><th>Source</th><th>Received (UTC)</th><th>Receiver</th><th>Source frame</th></tr>
{''.join(rows)}</table></div><h2>Calculated estimates</h2>
<p>Calculated values are not directly transmitted weather observations. Inputs must come from the same receiver,
be at most 10 seconds old, and be no more than 5 seconds apart. Wind also requires straight, approximately level flight,
a position, geometric altitude, and a valid magnetic model. Missing estimates mean these checks were not met;
wind below 1 kt has no reported direction.</p><div class="table">{''.join(derived)}</div></body></html>'''


def make_server(address: tuple[str, int], store: AircraftStore, receiver_id: str,
                connected) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                payload = render(store.snapshot(), receiver_id, connected()).encode()
                content_type = "text/html; charset=utf-8"
            elif path == "/api/aircraft":
                payload = json.dumps(store.snapshot(), allow_nan=False).encode()
                content_type = "application/json"
            elif re.fullmatch(r"/aircraft/[0-9A-Fa-f]{6}", path):
                icao = path.rsplit("/", 1)[1].upper()
                row = next((row for row in store.snapshot() if row["icao"] == icao), None)
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
