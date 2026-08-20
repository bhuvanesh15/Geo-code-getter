#!/usr/bin/env python3
"""Area outline (real polygon) for any place name, end to end.

Google only ships the selected-place outline as rasterised `maps/vt` PNG tiles,
so the shape has to be traced back out of pixels. This driver chains the whole
pipeline for an arbitrary query:

    resolve place  -> maps/preview/place  (name, feature id, mid, bbox, gcid)
    build tile url -> maps/vt spotlit     (same fields, re-encoded)
    fetch + stitch -> one RGBA canvas
    trace          -> GeoJSON Polygon in WGS84

No HAR capture is required. Everything the tile endpoint needs about a place
comes from the preview/place response.

Usage:
    python area_boundary.py "Koramangala, Bengaluru"
    python area_boundary.py "Anna Nagar, Chennai" --zoom 16
    python area_boundary.py "Brooklyn, New York" --output brooklyn.geojson
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote

from PIL import Image

from maps_area_bounds import build_url, http_get, parse_place
from trace_maps_boundary import trace

TILE_SIZE = 256
# Tile style epoch from a live Maps load. Google bumps this periodically; if
# tiles start coming back blank, recapture it from any maps/vt request.
TILE_EPOCH = "791557318"
# Rendering experiment ids sent by the live client. Kept verbatim because the
# spotlit layer is not drawn without them.
EXPERIMENTS = (
    "!23i1368782!23i1368785!23i4861626!23i10211310!23i1381938!23i47054629"
    "!23i47029525!23i72272233!23i72272234!23i72272236!23i72458815!23i94243289"
    "!23i94255677!23i72860224!23i10211515!23i94260020!23i100799651!23i72549439"
)


def deg_to_e7(value: float) -> int:
    return round(value * 1e7)


def lon_to_tile_x(lon: float, zoom: int) -> int:
    return math.floor((lon + 180.0) / 360.0 * (2**zoom))


def lat_to_tile_y(lat: float, zoom: int) -> int:
    lat_rad = math.radians(lat)
    return math.floor((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * (2**zoom))


def pick_zoom(bbox: dict[str, float], target_px: int, max_tiles: int) -> int:
    """Smallest zoom that renders the area near target_px wide, within budget."""
    span = max(bbox["east"] - bbox["west"], 1e-6)
    ideal = math.log2(target_px * 360.0 / (span * TILE_SIZE))
    for zoom in range(min(int(round(ideal)), 20), 4, -1):
        wide = lon_to_tile_x(bbox["east"], zoom) - lon_to_tile_x(bbox["west"], zoom) + 1
        tall = lat_to_tile_y(bbox["south"], zoom) - lat_to_tile_y(bbox["north"], zoom) + 1
        if wide * tall <= max_tiles:
            return zoom
    return 5


BASE_TILE = (
    "https://www.google.com/maps/vt/pb="
    "!1m4!1m3!1i{zoom}!2i{x}!3i{y}"
    "!2m3!1e0!2sm!3i" + TILE_EPOCH + "{layer}"
    "!3m8!2sen!3sin!5e1105!12m4!1e68!2m2!1sset!2sRoadmap!4e0!5m1!1e0"
    + EXPERIMENTS
)


def build_base_url(zoom: int, x: int, y: int) -> str:
    """The same tile without the spotlit overlay, for differencing."""
    return BASE_TILE.format(zoom=zoom, x=x, y=y, layer="")


def build_tile_url(place: dict[str, Any], zoom: int, x: int, y: int) -> str:
    feature_id = place.get("feature_id")
    if not feature_id or ":" not in feature_id:
        raise RuntimeError("Place has no feature id; cannot address the spotlit layer")
    high, low = (int(half, 16) for half in feature_id.split(":"))

    bbox, center = place["bbox"], place["center"]
    mid = place.get("mid")

    # !14m<n> and !1m<n> are item counts for their groups, so they shift when the
    # optional knowledge-graph mid is absent.
    if mid:
        encoded = base64.urlsafe_b64encode(mid.encode()).decode().rstrip("=")
        identity = f"!1m2!1y{high}!2y{low}!2z{encoded}"
        counts = "!14m21!1m16"
    else:
        identity = f"!1m2!1y{high}!2y{low}"
        counts = "!14m20!1m15"

    return (
        BASE_TILE.format(zoom=zoom, x=x, y=y, layer="!2m2!1e2!2sspotlit")
        + f"!27m23!299174093m22{counts}{identity}"
        f"!4m2!1x{deg_to_e7(center['lat'])}!2x{deg_to_e7(center['lng'])}!8b1"
        f"!12m6!1m2!1x{deg_to_e7(bbox['south'])}!2x{deg_to_e7(bbox['west'])}"
        f"!2m2!1x{deg_to_e7(bbox['north'])}!2x{deg_to_e7(bbox['east'])}"
        f"!15s{place['type'].replace(':', '%3A')}!2b0!3b1!6b0!8b0"
    )


def fetch_tile(
    place: dict[str, Any], zoom: int, x: int, y: int
) -> tuple[int, int, Image.Image, Image.Image]:
    spotlit = http_get(build_tile_url(place, zoom, x, y), use_bee=False, binary=True)
    base = http_get(build_base_url(zoom, x, y), use_bee=False, binary=True)
    return (
        x,
        y,
        Image.open(io.BytesIO(spotlit)).convert("RGBA"),
        Image.open(io.BytesIO(base)).convert("RGBA"),
    )


def fetch_mosaic(place: dict[str, Any], zoom: int, output: Path, workers: int) -> tuple[Path, Path]:
    bbox = place["bbox"]
    min_x, max_x = lon_to_tile_x(bbox["west"], zoom), lon_to_tile_x(bbox["east"], zoom)
    min_y, max_y = lat_to_tile_y(bbox["north"], zoom), lat_to_tile_y(bbox["south"], zoom)
    # One tile of margin so a boundary hugging the viewport edge is not clipped.
    min_x, max_x, min_y, max_y = min_x - 1, max_x + 1, min_y - 1, max_y + 1
    coords = [(x, y) for y in range(min_y, max_y + 1) for x in range(min_x, max_x + 1)]
    print(f"fetching {len(coords)} tiles at z{zoom}", flush=True)

    tiles: dict[tuple[int, int], tuple[Image.Image, Image.Image]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_tile, place, zoom, x, y) for x, y in coords]
        for future in as_completed(futures):
            x, y, spotlit, base = future.result()
            tiles[(x, y)] = (spotlit, base)

    output.mkdir(parents=True, exist_ok=True)
    size = ((max_x - min_x + 1) * TILE_SIZE, (max_y - min_y + 1) * TILE_SIZE)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    plain = Image.new("RGBA", size, (0, 0, 0, 0))
    for (x, y), (spotlit, base) in tiles.items():
        origin = ((x - min_x) * TILE_SIZE, (y - min_y) * TILE_SIZE)
        canvas.paste(spotlit, origin)
        plain.paste(base, origin)

    plain.save(output / f"base_z{zoom}.png")
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
                "tile_width": TILE_SIZE,
                "tile_height": TILE_SIZE,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"stitched {canvas.width}x{canvas.height} -> {image_path}", flush=True)
    return image_path, metadata_path


def slugify(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "_" for c in text]
    return "".join(keep).strip("_").replace("__", "_") or "area"


def geojson_io_url(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    compact = json.dumps(data, separators=(",", ":"))
    return "https://geojson.io/#data=data:application/json," + quote(compact, safe="")


def print_result(output: Path, closed: bool) -> None:
    summary = json.loads(output.read_text(encoding="utf-8"))
    props = summary["features"][0]["properties"]
    status = "closed ring" if closed else "open fragment"
    print(
        f"{status}: {props.get('name')} — {props['vertices']} vertices, "
        f"~{props['approx_area_km2']} km2 -> {output}",
        flush=True,
    )
    print(f"geojson.io  {geojson_io_url(output)}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Trace a place's real outline into GeoJSON.")
    p.add_argument("query", help='Area name, e.g. "Koramangala, Bengaluru"')
    p.add_argument("--zoom", type=int, help="Force a tile zoom instead of auto-picking")
    p.add_argument("--output", type=Path, help="GeoJSON path (default: <slug>.geojson)")
    p.add_argument("--workers", type=int, default=8)
    # Gaps in the dashed outline are a fixed geographic length, so their pixel
    # width grows with zoom. A moderate mosaic keeps them bridgeable; going
    # higher buys resolution but can leave the ring permanently open.
    p.add_argument("--target-px", type=int, default=1800, help="Desired mosaic width")
    p.add_argument("--max-tiles", type=int, default=420)
    p.add_argument("--target", default="241,156,149", help="Outline RGB to match")
    p.add_argument("--tolerance", type=float, default=50)
    p.add_argument("--close-size", type=int, default=17)
    p.add_argument(
        "--bridge-size",
        type=int,
        help="Gap bridged between dots. Omit to search for the smallest that closes.",
    )
    p.add_argument("--keep-tiles", action="store_true", help="Keep the stitched mosaic")
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        place = parse_place(http_get(build_url(args.query), use_bee=False))
    except Exception as exc:
        print(f"Failed to resolve place: {exc}", file=sys.stderr)
        return 1

    print(f"resolved {place['name']} ({place['type']}) {place['feature_id']}")
    slug = slugify(place.get("name") or args.query)
    output = args.output or Path(f"{slug}.geojson")
    tile_dir = Path(f"tiles_{slug}")

    top_zoom = args.zoom or pick_zoom(place["bbox"], args.target_px, args.max_tiles)
    # Outline gaps span a fixed ground distance, so they shrink in pixels as the
    # zoom drops. Start detailed and step down until the ring actually closes,
    # rather than betting everything on one guess.
    zooms = [top_zoom, top_zoom - 1, top_zoom - 2]
    # Past roughly 150px the kernel stops bridging the ring and starts merging
    # it into unrelated geometry.
    bridges = [args.bridge_size] if args.bridge_size else [31, 45, 61, 81, 101, 131, 151]
    target = tuple(int(v) for v in args.target.split(","))

    closed = False
    try:
        for zoom in zooms:
            image_path, metadata_path = fetch_mosaic(place, zoom, tile_dir, args.workers)
            print(
                f"tracing outline at z{zoom} "
                f"({len(bridges)} gap sizes) — this is CPU, not network",
                flush=True,
            )
            for bridge in bridges:
                print(f"  trying bridge={bridge}px ...", flush=True)
                closed = trace(
                    image_path,
                    metadata_path,
                    output,
                    target,
                    args.tolerance,
                    args.close_size,
                    bridge,
                    name=place.get("name"),
                    quiet=True,
                )
                if closed:
                    break
                print("    open ring, next gap size", flush=True)
            if closed:
                break
            print(f"z{zoom} never closed; stepping down", file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1

    if output.exists():
        print_result(output, closed)
    elif not closed:
        print(
            "warning: outline never closed. The polygon is the largest fragment; "
            "inspect the .trace.png before trusting it.",
            file=sys.stderr,
        )

    if not args.keep_tiles:
        for path in tile_dir.glob("*"):
            path.unlink()
        tile_dir.rmdir()
    return 0 if output.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
