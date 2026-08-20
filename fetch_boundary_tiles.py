#!/usr/bin/env python3
"""Replay a captured Google Maps spotlit tile URL at a higher zoom."""

from __future__ import annotations

import argparse
import io
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from curl_cffi import requests
from PIL import Image


TILE_RE = re.compile(r"!1m4!1m3!1i(\d+)!2i(\d+)!3i(\d+)")
BBOX_RE = re.compile(
    r"!12m6!1m2!1x(-?\d+)!2x(-?\d+)!2m2!1x(-?\d+)!2x(-?\d+)"
)


def lon_to_x(lon: float, zoom: int) -> int:
    return math.floor((lon + 180.0) / 360.0 * (2**zoom))


def lat_to_y(lat: float, zoom: int) -> int:
    lat_rad = math.radians(lat)
    return math.floor(
        (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * (2**zoom)
    )


def captured_tile_url(har_path: Path) -> str:
    har = json.loads(har_path.read_text(encoding="utf-8"))
    for entry in har["log"]["entries"]:
        url = entry["request"]["url"]
        if "/maps/vt/" in url and TILE_RE.search(url) and BBOX_RE.search(url):
            return url
    raise RuntimeError("No place-specific maps/vt tile URL found in HAR")


def fetch_tile(url: str, x: int, y: int, zoom: int) -> tuple[int, int, Image.Image]:
    tile_url = TILE_RE.sub(
        f"!1m4!1m3!1i{zoom}!2i{x}!3i{y}", url, count=1
    )
    response = requests.get(
        tile_url,
        headers={
            "referer": "https://www.google.com/maps/",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        },
        impersonate="chrome131",
        timeout=30,
    )
    response.raise_for_status()
    if "image/png" not in response.headers.get("content-type", ""):
        raise RuntimeError(f"tile {x},{y} was not PNG")
    return x, y, Image.open(io.BytesIO(response.content)).convert("RGBA")


def fetch(har_path: Path, zoom: int, output: Path, workers: int) -> None:
    url = captured_tile_url(har_path)
    bbox = BBOX_RE.search(url)
    assert bbox
    south, west, north, east = (int(value) / 1e7 for value in bbox.groups())

    min_x = lon_to_x(west, zoom)
    max_x = lon_to_x(east, zoom)
    min_y = lat_to_y(north, zoom)
    max_y = lat_to_y(south, zoom)
    coordinates = [
        (x, y) for y in range(min_y, max_y + 1) for x in range(min_x, max_x + 1)
    ]
    print(
        f"fetching {len(coordinates)} tiles at z{zoom}: "
        f"x={min_x}..{max_x}, y={min_y}..{max_y}"
    )

    tiles: dict[tuple[int, int], Image.Image] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(fetch_tile, url, x, y, zoom) for x, y in coordinates
        ]
        for future in as_completed(futures):
            x, y, image = future.result()
            tiles[(x, y)] = image

    output.mkdir(parents=True, exist_ok=True)
    tile_width, tile_height = next(iter(tiles.values())).size
    canvas = Image.new(
        "RGBA",
        ((max_x - min_x + 1) * tile_width, (max_y - min_y + 1) * tile_height),
        (0, 0, 0, 0),
    )
    for (x, y), image in tiles.items():
        canvas.paste(image, ((x - min_x) * tile_width, (y - min_y) * tile_height))

    image_path = output / f"stitched_z{zoom}.png"
    metadata_path = output / f"stitched_z{zoom}.json"
    canvas.save(image_path)
    metadata_path.write_text(
        json.dumps(
            {
                "zoom": zoom,
                "min_x": min_x,
                "max_x": max_x,
                "min_y": min_y,
                "max_y": max_y,
                "tile_width": tile_width,
                "tile_height": tile_height,
                "source_bbox": {
                    "south": south,
                    "west": west,
                    "north": north,
                    "east": east,
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {image_path} ({canvas.width}x{canvas.height})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("har", type=Path)
    parser.add_argument("--zoom", type=int, default=13)
    parser.add_argument("--output", type=Path, default=Path("maps_boundary_highres"))
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    fetch(args.har, args.zoom, args.output, args.workers)


if __name__ == "__main__":
    main()
