#!/usr/bin/env python3
"""Extract and stitch Google Maps raster tiles embedded in a Playwright HAR."""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
from pathlib import Path

from PIL import Image


TILE_RE = re.compile(r"!1i(\d+)!2i(\d+)!3i(\d+)")


def extract(har_path: Path, output: Path) -> None:
    har = json.loads(har_path.read_text(encoding="utf-8"))
    tiles: dict[tuple[int, int, int], Image.Image] = {}

    for entry in har["log"]["entries"]:
        url = entry["request"]["url"]
        if "/maps/vt/" not in url:
            continue
        match = TILE_RE.search(url)
        content = entry["response"]["content"]
        if not match or content.get("mimeType") != "image/png" or "text" not in content:
            continue

        z, x, y = map(int, match.groups())
        raw = content["text"]
        data = base64.b64decode(raw) if content.get("encoding") == "base64" else raw.encode()
        tiles[(z, x, y)] = Image.open(io.BytesIO(data)).convert("RGBA")

    if not tiles:
        raise RuntimeError("No embedded maps/vt PNG tiles found")

    zooms = sorted({key[0] for key in tiles})
    output.mkdir(parents=True, exist_ok=True)
    print(f"found {len(tiles)} tiles at zooms {zooms}")

    for zoom in zooms:
        group = {(x, y): image for (z, x, y), image in tiles.items() if z == zoom}
        min_x = min(x for x, _ in group)
        max_x = max(x for x, _ in group)
        min_y = min(y for _, y in group)
        max_y = max(y for _, y in group)
        tile_w, tile_h = next(iter(group.values())).size
        canvas = Image.new(
            "RGBA",
            ((max_x - min_x + 1) * tile_w, (max_y - min_y + 1) * tile_h),
            (0, 0, 0, 0),
        )

        for (x, y), image in group.items():
            image.save(output / f"z{zoom}_{x}_{y}.png")
            canvas.paste(image, ((x - min_x) * tile_w, (y - min_y) * tile_h))

        canvas.save(output / f"stitched_z{zoom}.png")
        metadata = {
            "zoom": zoom,
            "min_x": min_x,
            "max_x": max_x,
            "min_y": min_y,
            "max_y": max_y,
            "tile_width": tile_w,
            "tile_height": tile_h,
        }
        (output / f"stitched_z{zoom}.json").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )
        print(f"wrote {output / f'stitched_z{zoom}.png'} ({canvas.size[0]}x{canvas.size[1]})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("har", type=Path)
    parser.add_argument("--output", type=Path, default=Path("maps_boundary_tiles"))
    args = parser.parse_args()
    extract(args.har, args.output)


if __name__ == "__main__":
    main()
