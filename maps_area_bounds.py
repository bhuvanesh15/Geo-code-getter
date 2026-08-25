#!/usr/bin/env python3
"""Area boundaries (lat/lng bbox) for a place name via Google Maps' internal
maps/preview/place endpoint.

This is the unofficial path captured in r1.txt/r3.txt, not the official
Geocoding API. The r4 EvxQ3b RPC is viewport traffic and is ignored.

The endpoint is driven entirely by the `pb` parameter, a bang-delimited
protobuf-ish struct. Querying by free text (`!2s<query>`) avoids needing a
feature id up front, so a single request resolves the place and returns its
viewport. Coordinates come back as E7 integers (degrees * 1e7), with negative
values encoded as unsigned 32-bit.

Usage:
    python maps_area_bounds.py "HSR Layout, Bengaluru, Karnataka"
    python maps_area_bounds.py "Brooklyn, New York" --json
    python maps_area_bounds.py "Koramangala" --geojson out.geojson
    python maps_area_bounds.py --from-file r3_response.txt
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

PREVIEW_PLACE = "https://www.google.com/maps/preview/place"

# Everything after the viewport block. Copied from a live Maps page load; these
# flags select which sections the backend renders. The place/viewport data we
# want is present as long as this block is well formed.
PB_TAIL = (
    "!2m3!1f0.0!2f0.0!3f0.0!3m2!1i1024!2i768!4f13.1"
    "!12m4!2m3!1i360!2i120!4i8"
    "!13m57!2m2!1i203!2i100!3m2!2i4!5b1!6m6!1m2!1i86!2i86!1m2!1i408!2i240"
    "!7m33!1m3!1e1!2b0!3e3!1m3!1e2!2b1!3e2!1m3!1e2!2b0!3e3!1m3!1e8!2b0!3e3"
    "!1m3!1e10!2b0!3e3!1m3!1e10!2b1!3e2!1m3!1e10!2b0!3e4!1m3!1e9!2b1!3e2!2b1!9b0"
    "!15m8!1m7!1m2!1m1!1e2!2m2!1i195!2i195!3i20"
    "!21m0!22m1!1e81!30m8!3b1!6m2!1b1!2b1!7m2!1e3!2b1!9b1!37i791"
)
# Unresolved "whole map" viewport: span, lng, lat. Google's own shell sends this
# before it knows where the query is, and the backend still resolves the place.
DEFAULT_VIEWPORT = ("1.5243982559647353E7", "81.914063", "21.125498")

UINT32 = 1 << 32
INT32_MAX = 1 << 31

# [[sw_lat,sw_lng],[ne_lat,ne_lng]],null,null,"gcid:sublocality1"
BBOX_RE = re.compile(
    r"\[\[(-?\d{6,10}),(-?\d{6,10})\],\[(-?\d{6,10}),(-?\d{6,10})\]\]"
    r",null,null,\"(gcid:[a-z0-9_]+)\""
)
# [null,null,<lat>,<lng>],"0x..:0x..","Name"
HEAD_RE = re.compile(
    r"\[null,null,(-?\d+\.\d+),(-?\d+\.\d+)\],"
    r"\"(0x[0-9a-f]+:0x[0-9a-f]+)\",\"([^\"]*)\""
)
CHIJ_RE = re.compile(r"\"(ChIJ[A-Za-z0-9_-]{16,})\"")
MID_RE = re.compile(r"\"(/[gm]/[0-9a-z_]+)\"")
ADDRESS_RE = re.compile(r"\[2,\[\[\"([^\"]+)\"\]\]\]")
# Fallback anchors for older captures (r3_response.txt) that lack the ,null,null tail
LEGACY_BBOX_RE = re.compile(
    r"\[\[(-?\d{6,10}),(-?\d{6,10})\],\[(-?\d{6,10}),(-?\d{6,10})\]\]"
    r"[^\]]{0,80}\"(gcid:[a-z0-9_]+)\""
)


def e7_to_deg(value: int) -> float:
    """E7 int to degrees, undoing unsigned 32-bit wrap for western/southern coords."""
    if value >= INT32_MAX:
        value -= UINT32
    return value / 1e7


def load_scrapingbee_key() -> str:
    env = Path(__file__).resolve().parent / ".cursor" / "env" / "scrapingbee.env"
    if not env.exists():
        return ""
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("SCRAPINGBEE_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


class PermanentHttpError(RuntimeError):
    """4xx (except 429): bad pb/template — do not retry."""


_tls = threading.local()


def http_session():
    """One curl_cffi Session per thread so TLS/TCP is reused across tiles."""
    session = getattr(_tls, "session", None)
    if session is None:
        from curl_cffi import requests as cffi

        session = cffi.Session(impersonate="chrome131")
        _tls.session = session
    return session


def http_get(url: str, *, use_bee: bool = False, binary: bool = False, retries: int = 4) -> Any:
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "referer": "https://www.google.com/maps/",
    }
    if use_bee:
        key = load_scrapingbee_key()
        if not key:
            raise RuntimeError("ScrapingBee requested but SCRAPINGBEE_API_KEY is empty")
        params = {
            "api_key": key,
            "url": url,
            # Google domains are rejected by ScrapingBee without this flag.
            "custom_google": "true",
            "render_js": "false",
        }
        url = "https://app.scrapingbee.com/api/v1/?" + "&".join(
            f"{k}={quote(v, safe='')}" for k, v in params.items()
        )
        headers = {"accept": "*/*"}

    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = http_session().get(url, headers=headers, timeout=45)
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                raise PermanentHttpError(
                    f"HTTP {resp.status_code} (permanent; not retrying): {url[:160]}"
                )
            resp.raise_for_status()
            return resp.content if binary else resp.text
        except PermanentHttpError:
            raise
        except ImportError:
            import urllib.request

            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read()
            return raw if binary else raw.decode("utf-8", errors="replace")
        except Exception as exc:
            last = exc
            if attempt + 1 >= retries:
                break
            time.sleep(0.5 * (2**attempt) + random.random() * 0.3)
    assert last is not None
    raise last


def build_url(query: str) -> str:
    span, lng, lat = DEFAULT_VIEWPORT
    pb = f"!1m14!2s{query}!3m12!1m3!1d{span}!2d{lng}!3d{lat}{PB_TAIL}"
    return (
        f"{PREVIEW_PLACE}?authuser=0&hl=en&gl=in"
        f"&q={quote(query)}&pb={quote(pb, safe='!')}"
    )


def bbox_area_km2(south: float, west: float, north: float, east: float) -> float:
    mean_lat = math.radians((south + north) / 2)
    height = (north - south) * 111.32
    width = (east - west) * 111.32 * math.cos(mean_lat)
    return round(abs(height * width), 3)


def parse_place(raw: str) -> dict[str, Any]:
    text = raw[4:] if raw.startswith(")]}'") else raw

    match = BBOX_RE.search(text) or LEGACY_BBOX_RE.search(text)
    if not match:
        raise ValueError(
            "No viewport found in the response. The place may not exist, or the "
            "pb template has drifted and needs recapturing from a live Maps load."
        )

    south, west, north, east, gcid = (
        e7_to_deg(int(match.group(1))),
        e7_to_deg(int(match.group(2))),
        e7_to_deg(int(match.group(3))),
        e7_to_deg(int(match.group(4))),
        match.group(5),
    )

    head = HEAD_RE.search(text)
    if head:
        center = {"lat": float(head.group(1)), "lng": float(head.group(2))}
        feature_id, name = head.group(3), head.group(4)
    else:
        center = {
            "lat": round((south + north) / 2, 7),
            "lng": round((west + east) / 2, 7),
        }
        feature_id = name = None

    chij = CHIJ_RE.search(text)
    mid = MID_RE.search(text)
    address = ADDRESS_RE.search(text)

    ring = [
        [west, south],
        [east, south],
        [east, north],
        [west, north],
        [west, south],
    ]
    return {
        "name": name,
        "address": address.group(1) if address else None,
        "type": gcid,
        "feature_id": feature_id,
        "place_id": chij.group(1) if chij else None,
        "mid": mid.group(1) if mid else None,
        "center": center,
        "bbox": {
            "south": round(south, 7),
            "west": round(west, 7),
            "north": round(north, 7),
            "east": round(east, 7),
        },
        "approx_area_km2": bbox_area_km2(south, west, north, east),
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def verify_urls(result: dict[str, Any]) -> dict[str, str]:
    """Links for eyeballing the extracted box against what Maps actually draws."""
    b, c = result["bbox"], result["center"]
    sw = f"{b['south']},{b['west']}"
    ne = f"{b['north']},{b['east']}"
    urls = {
        "place": (
            "https://www.google.com/maps/search/?api=1&query="
            f"{quote(result.get('query') or result.get('name') or '')}"
        ),
        "center": f"https://www.google.com/maps?q={c['lat']},{c['lng']}&z=14",
        "sw_corner": f"https://www.google.com/maps?q={sw}&z=15",
        "ne_corner": f"https://www.google.com/maps?q={ne}&z=15",
        # Diagonal of the box: both corners visible in one view.
        "both_corners": f"https://www.google.com/maps/dir/{sw}/{ne}",
        "geojson_io": "https://geojson.io/#data=data:application/json,"
        + quote(json.dumps(to_geojson(result)), safe=""),
    }
    if result.get("place_id"):
        urls["place_id"] = (
            "https://www.google.com/maps/search/?api=1&query="
            f"{quote(result.get('name') or '')}&query_place_id={result['place_id']}"
        )
    return urls


def to_geojson(result: dict[str, Any]) -> dict[str, Any]:
    props = {k: v for k, v in result.items() if k not in ("geometry", "bbox")}
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "bbox": [
                    result["bbox"]["west"],
                    result["bbox"]["south"],
                    result["bbox"]["east"],
                    result["bbox"]["north"],
                ],
                "properties": props,
                "geometry": result["geometry"],
            }
        ],
    }


def render(result: dict[str, Any]) -> str:
    b, c = result["bbox"], result["center"]
    lines = [
        f"name       {result.get('name')}",
        f"address    {result.get('address')}",
        f"type       {result.get('type')}",
        f"feature_id {result.get('feature_id')}",
        f"place_id   {result.get('place_id')}",
        f"center     {c['lat']}, {c['lng']}",
        "",
        f"south      {b['south']}",
        f"west       {b['west']}",
        f"north      {b['north']}",
        f"east       {b['east']}",
        "",
        f"SW corner  {b['south']}, {b['west']}",
        f"NE corner  {b['north']}, {b['east']}",
        f"area       ~{result['approx_area_km2']} km2",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Area lat/lng boundaries from Google Maps' internal preview/place endpoint."
    )
    p.add_argument("query", nargs="?", help='Area name, e.g. "HSR Layout, Bengaluru"')
    p.add_argument("--from-file", type=Path, help="Parse a saved preview/place body instead")
    p.add_argument("--scrapingbee", action="store_true", help="Route the fetch through ScrapingBee")
    p.add_argument("--json", action="store_true", help="Print raw JSON")
    p.add_argument("--geojson", type=Path, metavar="PATH", help="Write a GeoJSON FeatureCollection")
    p.add_argument("--save-raw", type=Path, metavar="PATH", help="Save the raw response body")
    p.add_argument("--urls", action="store_true", help="Print links to verify the box on a map")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if not args.query and not args.from_file:
        build_parser().error("provide a query or --from-file")

    try:
        if args.from_file:
            raw = args.from_file.read_text(encoding="utf-8", errors="replace")
        else:
            raw = http_get(build_url(args.query), use_bee=args.scrapingbee)
            if args.save_raw:
                args.save_raw.write_text(raw, encoding="utf-8")
        result = parse_place(raw)
        if args.query:
            result["query"] = args.query
    except Exception as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1

    if args.geojson:
        args.geojson.write_text(json.dumps(to_geojson(result), indent=2), encoding="utf-8")
        print(f"wrote {args.geojson}")

    if args.urls:
        result["verify_urls"] = verify_urls(result)

    print(json.dumps(result, indent=2) if args.json else render(result))

    if args.urls and not args.json:
        print("\nverify:")
        for label, url in result["verify_urls"].items():
            print(f"  {label:13} {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
