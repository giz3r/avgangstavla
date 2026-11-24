"""Local preview of the departure board using a saved JSON snapshot."""

import argparse
import base64
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from math import ceil
import os
from pathlib import Path
import re
from threading import Lock
from time import monotonic
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def color(value, fallback):
    return value if re.fullmatch(r"#[0-9a-fA-F]{6}", value or "") else fallback


API = "https://ext-api.vasttrafik.se"
STOPS = {
    "Bergsprängaregatan": "9021014001390000",
    "Engdahlsgatan": "9021014002230000",
}


def read_json(request):
    with urlopen(request, timeout=8) as response:
        return json.load(response)


def live_source(client_id, secret):
    credentials = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    cache = {}
    refresh_at = 0
    token = ""
    expires_at = 0
    lock = Lock()

    def get_departures():
        nonlocal cache, refresh_at, token, expires_at
        with lock:
            if monotonic() < refresh_at:
                return cache
            if monotonic() >= expires_at:
                request = Request(
                    f"{API}/token",
                    data=urlencode({"grant_type": "client_credentials"}).encode(),
                    headers={"Authorization": f"Basic {credentials}"},
                )
                response = read_json(request)
                token = response["access_token"]
                expires_at = monotonic() + max(0, int(response.get("expires_in", 0)) - 30)
            headers = {"Authorization": f"Bearer {token}"}
            cache = {
                name: read_json(Request(
                    f"{API}/pr/v4/stop-areas/{gid}/departures", headers=headers
                ))["results"]
                for name, gid in STOPS.items()
            }
            refresh_at = monotonic() + 30
            return cache

    return get_departures


def render_stop(name, departures, now=None):
    groups = {}
    for departure in departures:
        journey = departure["serviceJourney"]
        line = journey["line"]
        label = line.get("designation") or line.get("name") or "–"
        destination = journey.get("directionDetails", {}).get("shortDirection")
        destination = destination or journey.get("direction") or "–"
        platform = departure.get("stopPoint", {}).get("platform", "")
        timestamp = departure.get("estimatedTime") or departure["plannedTime"]
        moment = datetime.fromisoformat(timestamp)
        if now is not None and moment < now:
            continue
        groups.setdefault((platform, label, destination), []).append(
            (moment, departure.get("isCancelled", False), line)
        )

    rows = []
    for (platform, label, destination), times in sorted(
        groups.items(), key=lambda entry: (entry[0][0], min(item[0] for item in entry[1]), entry[0][1:])
    ):
        times.sort(key=lambda item: item[0])
        line = times[0][2]
        background = color(line.get("backgroundColor"), "#dddddd")
        foreground = color(line.get("foregroundColor"), "#000000")
        labels = [
            "Inställd" if cancelled else departure_label(moment, now)
            for moment, cancelled, _ in times
        ]
        rows.append(
            f"<tr><td>{escape(platform)}</td>"
            f'<td><span class="line" style="background:{background};color:{foreground}">'
            f"{escape(label)}</span></td><td>{escape(destination)}</td>"
            f'<td class="times">{", ".join(labels)}</td></tr>'
        )

    return (
        f"<section><h2>{escape(name)}</h2><table>"
        '<thead><tr><th scope="col">Läge</th><th scope="col">Linje</th>'
        '<th scope="col">Mot</th><th scope="col">Avgår</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></section>'
    )


def departure_label(moment, now):
    if now is None:
        return moment.strftime("%H:%M")
    seconds = (moment - now).total_seconds()
    return "Nu" if seconds <= 30 else f"{ceil(seconds / 60)} min"


def render_page(data, live=False):
    now = datetime.now().astimezone() if live else None
    status = "Liveavgångar" if live else "Testdata · sparade avgångstider"
    refresh = '<meta http-equiv="refresh" content="30">' if live else ""
    sections = "".join(render_stop(name, departures, now) for name, departures in data.items())
    return f"""<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Avgångstavla</title>
{refresh}
<style>
body {{ margin: 0; background: #1d242a; color: white; font: 18px Arial, sans-serif; }}
header {{ text-align: center; padding: 16px 12px 4px; }}
h1 {{ margin: 0 0 6px; font-size: 26px; }}
header p {{ margin: 0; color: #c6cdd4; font-size: 14px; }}
main {{ padding: 0 8px 16px; }}
section {{ padding: 0 8px; box-sizing: border-box; }}
h2 {{ font-size: 21px; margin: 16px 0 8px; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ text-align: left; padding: 8px 5px; border-bottom: 1px solid #52606b; }}
th {{ font-size: 14px; color: #c6cdd4; }}
th:last-child, .times {{ text-align: right; }}
.times {{ font-weight: bold; }}
.line {{ display: inline-block; min-width: 24px; padding: 3px 5px;
         border-radius: 4px; text-align: center; font-weight: bold; }}
@media (orientation: landscape) and (min-width: 760px) {{
  section {{ float: left; width: 50%; }}
  main:after {{ content: ""; display: block; clear: both; }}
}}
</style>
</head>
<body>
<header><h1>Avgångstavla</h1><p>{status}</p></header>
<main>{sections}</main>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data", type=Path, default=Path(__file__).with_name("departures.json"))
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.live:
        client_id = os.environ.get("VASTTRAFIK_CLIENT_ID")
        secret = os.environ.get("VASTTRAFIK_CLIENT_SECRET")
        if not client_id or not secret:
            parser.error("--live kräver VASTTRAFIK_CLIENT_ID och VASTTRAFIK_CLIENT_SECRET")
        load_data = live_source(client_id, secret)
    else:
        data = json.loads(args.data.read_text(encoding="utf-8"))

        def load_data():
            return data

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/":
                self.send_error(404)
                return
            try:
                page = render_page(load_data(), live=args.live).encode("utf-8")
            except (URLError, TimeoutError, ValueError, KeyError) as error:
                status = getattr(error, "code", type(error).__name__)
                self.log_error("Data kunde inte hämtas eller läsas: %s", status)
                self.send_error(503, "Kunde inte visa avgangar. Forsok igen.")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)

    with ThreadingHTTPServer((args.bind, args.port), Handler) as server:
        print(f"Avgångstavla på http://{args.bind}:{args.port}/", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
