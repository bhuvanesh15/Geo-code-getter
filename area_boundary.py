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
import numpy as np

from maps_area_bounds import UINT32, build_url, http_get, parse_place
from trace_maps_boundary import close_kernel, trace

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

# Latency vs accuracy. `fast` is the 3–5s SLO: fewer tiles, one zoom, one kernel.
MODES = {
    "fast": {
        "zoom_cap": 10,
        "target_px": 800,
        "max_tiles": 24,
        "margin": False,
        "workers": 16,
        "single_zoom": True,
        "single_kernel": True,
    },
    "balanced": {
        "zoom_cap": 13,
        "target_px": 1800,
        "max_tiles": 420,
        "margin": True,
        "workers": 8,
        "single_zoom": False,
        "single_kernel": False,
    },
    "hi-fi": {
        "zoom_cap": 13,
        "target_px": 2200,
        "max_tiles": 420,
        "margin": True,
        "workers": 8,
        "single_zoom": False,
        "single_kernel": False,
    },
}


def deg_to_e7(value: float) -> int:
    """Degrees to E7. Western/southern values are uint32-wrapped, matching Maps."""
    n = round(value * 1e7)
    return n % UINT32 if n < 0 else n


def lon_to_tile_x(lon: float, zoom: int) -> int:
    return math.floor((lon + 180.0) / 360.0 * (2**zoom))


def lat_to_tile_y(lat: float, zoom: int) -> int:
    lat_rad = math.radians(lat)
    return math.floor((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * (2**zoom))


def pick_zoom(
    bbox: dict[str, float], target_px: int, max_tiles: int, zoom_cap: int = 13
) -> int:
    """Smallest zoom that renders the area near target_px wide, within budget."""
    span = max(bbox["east"] - bbox["west"], 1e-6)
    ideal = math.log2(target_px * 360.0 / (span * TILE_SIZE))
    for zoom in range(min(int(round(ideal)), zoom_cap), 4, -1):
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


def fetch_png(url: str) -> Image.Image:
    return Image.open(io.BytesIO(http_get(url, use_bee=False, binary=True))).convert("RGBA")


def fetch_mosaic(
    place: dict[str, Any],
    zoom: int,
    output: Path | None,
    workers: int,
    *,
    margin: bool = True,
    keep_tiles: bool = False,
) -> tuple[Image.Image, Image.Image, dict[str, Any]]:
    bbox = place["bbox"]
    min_x, max_x = lon_to_tile_x(bbox["west"], zoom), lon_to_tile_x(bbox["east"], zoom)
    min_y, max_y = lat_to_tile_y(bbox["north"], zoom), lat_to_tile_y(bbox["south"], zoom)
    if margin:
        min_x, max_x, min_y, max_y = min_x - 1, max_x + 1, min_y - 1, max_y + 1
    coords = [(x, y) for y in range(min_y, max_y + 1) for x in range(min_x, max_x + 1)]
    print(f"fetching {len(coords)} tile pairs ({len(coords) * 2} GETs) at z{zoom}", flush=True)

    jobs: list[tuple[str, int, int, str]] = []
    for x, y in coords:
        jobs.append(("spot", x, y, build_tile_url(place, zoom, x, y)))
        jobs.append(("base", x, y, build_base_url(zoom, x, y)))

    got: dict[tuple[str, int, int], Image.Image] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_png, url): (kind, x, y) for kind, x, y, url in jobs}
        for future in as_completed(futures):
            kind, x, y = futures[future]
            got[(kind, x, y)] = future.result()

    size = ((max_x - min_x + 1) * TILE_SIZE, (max_y - min_y + 1) * TILE_SIZE)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    plain = Image.new("RGBA", size, (0, 0, 0, 0))
    for x, y in coords:
        origin = ((x - min_x) * TILE_SIZE, (y - min_y) * TILE_SIZE)
        canvas.paste(got[("spot", x, y)], origin)
        plain.paste(got[("base", x, y)], origin)

    metadata = {
        "zoom": zoom,
        "min_x": min_x,
        "max_x": max_x,
        "min_y": min_y,
        "max_y": max_y,
        "tile_width": TILE_SIZE,
        "tile_height": TILE_SIZE,
    }
    if keep_tiles and output is not None:
        output.mkdir(parents=True, exist_ok=True)
        image_path = output / f"stitched_z{zoom}.png"
        canvas.save(image_path)
        plain.save(output / f"base_z{zoom}.png")
        (output / f"stitched_z{zoom}.json").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )
        print(f"stitched {canvas.width}x{canvas.height} -> {image_path}", flush=True)
    else:
        print(f"stitched {canvas.width}x{canvas.height} (in-memory)", flush=True)
    return canvas, plain, metadata


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
    stamp = props.get("extracted_at") or "—"
    print(
        f"{status}: {props.get('name')} — {props['vertices']} vertices, "
        f"~{props['approx_area_km2']} km2  extracted_at={stamp} -> {output}",
        flush=True,
    )
    print(f"geojson.io  {geojson_io_url(output)}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Trace a place's real outline into GeoJSON.")
    p.add_argument("query", help='Area name, e.g. "Koramangala, Bengaluru"')
    p.add_argument("--zoom", type=int, help="Force a tile zoom instead of auto-picking")
    p.add_argument("--output", type=Path, help="GeoJSON path (default: <slug>.geojson)")
    p.add_argument(
        "--mode",
        choices=tuple(MODES),
        default="fast",
        help="fast: 3–5s SLO, coarser ring. balanced/hi-fi: more tiles.",
    )
    p.add_argument("--workers", type=int, help="Concurrent GETs (default depends on --mode)")
    p.add_argument("--target-px", type=int, help="Desired mosaic width (overrides --mode)")
    p.add_argument("--max-tiles", type=int, help="Tile-pair budget (overrides --mode)")
    p.add_argument("--target", default="241,156,149", help="Outline RGB to match")
    p.add_argument("--tolerance", type=float, default=50)
    p.add_argument("--close-size", type=int, default=17)
    p.add_argument(
        "--bridge-size",
        type=int,
        help="Gap bridged between dots. Omit to search (balanced) or auto kernel (fast).",
    )
    p.add_argument("--keep-tiles", action="store_true", help="Keep the stitched mosaic")
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore an existing GeoJSON cache and fetch again",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    cfg = dict(MODES[args.mode])
    if args.target_px is not None:
        cfg["target_px"] = args.target_px
    if args.max_tiles is not None:
        cfg["max_tiles"] = args.max_tiles
    workers = args.workers if args.workers is not None else cfg["workers"]

    slug_hint = slugify(args.query)
    output = args.output or Path("data") / f"{slug_hint}.geojson"
    if output.exists() and not args.refresh:
        print(f"cache hit {output}", flush=True)
        closed = bool(
            json.loads(output.read_text(encoding="utf-8"))["features"][0]["properties"].get(
                "closed_ring"
            )
        )
        print_result(output, closed)
        return 0

    try:
        place = parse_place(http_get(build_url(args.query), use_bee=False))
    except Exception as exc:
        print(f"Failed to resolve place: {exc}", file=sys.stderr)
        return 1

    print(f"resolved {place['name']} ({place['type']}) {place['feature_id']}")
    slug = slugify(place.get("name") or args.query)
    if args.output is None:
        output = Path("data") / f"{slug}.geojson"
        if output.exists() and not args.refresh:
            print(f"cache hit {output}", flush=True)
            closed = bool(
                json.loads(output.read_text(encoding="utf-8"))["features"][0]["properties"].get(
                    "closed_ring"
                )
            )
            print_result(output, closed)
            return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    tile_dir = Path(f"tiles_{slug}")

    top_zoom = args.zoom or pick_zoom(
        place["bbox"], cfg["target_px"], cfg["max_tiles"], cfg["zoom_cap"]
    )
    # Fast: one zoom, one kernel. An open ring is written and we stop — no
    # z12→11→10 cascade and no 7-size bridge search (that is why London was 29s).
    zooms = [top_zoom] if cfg["single_zoom"] else [top_zoom, top_zoom - 1, top_zoom - 2]
    target = tuple(int(v) for v in args.target.split(","))

    closed = False
    try:
        for zoom in zooms:
            spotlit, base, metadata = fetch_mosaic(
                place,
                zoom,
                tile_dir,
                workers,
                margin=cfg["margin"],
                keep_tiles=args.keep_tiles,
            )
            if args.bridge_size is not None:
                bridges = [args.bridge_size]
            elif cfg["single_kernel"]:
                bridges = [close_kernel(zoom, 31)]
            else:
                bridges = [31, 45, 61, 81, 101, 131, 151]
            print(
                f"tracing outline at z{zoom} "
                f"({len(bridges)} gap size{'s' if len(bridges) != 1 else ''}"
                f"{', no retry' if cfg['single_kernel'] else ''}) — CPU",
                flush=True,
            )
            rgb = np.asarray(spotlit.convert("RGB"))
            base_rgb = np.asarray(base.convert("RGB"))
            for bridge in bridges:
                print(f"  kernel={bridge}px", flush=True)
                closed = trace(
                    None,
                    None,
                    output,
                    target,
                    args.tolerance,
                    args.close_size,
                    bridge,
                    name=place.get("name"),
                    quiet=True,
                    rgb=rgb,
                    base_rgb=base_rgb,
                    metadata=metadata,
                    write_diagnostics=args.keep_tiles,
                    mode=args.mode,
                )
                if closed or cfg["single_kernel"]:
                    if not closed:
                        print(
                            "    open ring — not retrying (single kernel)",
                            flush=True,
                        )
                    break
                print("    open ring, next gap size", flush=True)
            if closed or cfg["single_zoom"]:
                if not closed and cfg["single_zoom"]:
                    print(
                        f"z{zoom} open fragment — not stepping zoom",
                        file=sys.stderr,
                        flush=True,
                    )
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

    if tile_dir.exists() and not args.keep_tiles:
        for path in tile_dir.glob("*"):
            path.unlink()
        tile_dir.rmdir()
    return 0 if output.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
