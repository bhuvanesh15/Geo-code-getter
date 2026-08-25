#!/usr/bin/env python3
"""Serve the region viewer and run live --mode fast extracts."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PORT = 8000
RECENT_PATH = ROOT / "viewer" / "recent.json"
RECENT_MAX = 12
_recent_lock = threading.Lock()


def slugify(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "_" for c in text]
    return "".join(keep).strip("_").replace("__", "_") or "area"


def load_recent() -> list[dict]:
    if not RECENT_PATH.exists():
        return []
    try:
        data = json.loads(RECENT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    places = data.get("places") if isinstance(data, dict) else data
    return list(places or [])


def save_recent(places: list[dict]) -> list[dict]:
    clipped = places[:RECENT_MAX]
    RECENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECENT_PATH.write_text(
        json.dumps({"places": clipped}, indent=2), encoding="utf-8"
    )
    return clipped


def remember(query: str, output: Path, geojson: dict) -> list[dict]:
    props = {}
    features = geojson.get("features") or []
    if features:
        props = features[0].get("properties") or {}
    item = {
        "id": slugify(query),
        "query": query,
        "name": props.get("name") or query,
        "file": "/data/" + output.name,
        "group": "recent",
        "closed_ring": bool(props.get("closed_ring")),
        "extracted_at": props.get("extracted_at"),
    }
    with _recent_lock:
        places = [
            p
            for p in load_recent()
            if str(p.get("query") or "").casefold() != query.casefold()
        ]
        places.insert(0, item)
        return save_recent(places)


def extract(query: str) -> tuple[int, dict]:
    query = query.strip()
    if not query:
        return 400, {"error": "query is required"}
    output = ROOT / "data" / f"{slugify(query)}.geojson"
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-u",
        str(ROOT / "area_boundary.py"),
        query,
        "--mode",
        "fast",
        "--refresh",
        "--output",
        str(output),
    ]
    started = time.perf_counter()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    latency_s = round(time.perf_counter() - started, 2)
    if proc.returncode != 0 or not output.exists():
        err = (proc.stderr or proc.stdout or "extract failed").strip()
        return 502, {"error": err[-2000:], "latency_s": latency_s}
    geojson = json.loads(output.read_text(encoding="utf-8"))
    recent = remember(query, output, geojson)
    return 200, {"latency_s": latency_s, "geojson": geojson, "recent": recent}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/viewer", "/viewer/"):
            self.send_response(302)
            self.send_header("Location", "/viewer/index.html")
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") != "/api/extract":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON"})
            return
        status, payload = extract(str(body.get("query") or ""))
        self._json(status, payload)

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Region viewer  http://127.0.0.1:{PORT}/viewer/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
