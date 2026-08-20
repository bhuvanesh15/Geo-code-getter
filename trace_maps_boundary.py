#!/usr/bin/env python3
"""Trace Google Maps' dotted locality outline from a stitched raster tile image."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from scipy.spatial import cKDTree


def inspect_colors(image_path: Path) -> None:
    image = Image.open(image_path).convert("RGB")
    colors = Counter(image.getdata())
    reddish = [
        (count, color)
        for color, count in colors.items()
        if color[0] > 180 and color[0] - color[1] > 25 and color[0] - color[2] > 25
    ]
    reddish.sort(reverse=True)
    print("Most common red-family colors:")
    for count, color in reddish[:40]:
        print(f"{color}: {count}")


def link_dashes(mask: np.ndarray, max_gap: int, min_dash: int = 12) -> np.ndarray:
    """Join dashes with thin straight segments instead of thickening them.

    Morphological closing bridges gaps with a disc as wide as the gap, which
    also seals genuine narrow necks and erases slim arms of a boundary. Linking
    the nearest points of distinct dashes adds only 1px connectors, so the
    traced shape keeps its detail.
    """
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    points: list[np.ndarray] = []
    owners: list[int] = []
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area < min_dash:
            continue
        x, y, w, h = (int(stats[label, k]) for k in (
            cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP, cv2.CC_STAT_WIDTH, cv2.CC_STAT_HEIGHT
        ))
        roi = (labels[y : y + h, x : x + w] == label).astype(np.uint8)
        contours, _ = cv2.findContours(
            roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        if not contours:
            continue
        outline = np.vstack([c.reshape(-1, 2) for c in contours])
        outline[:, 0] += x
        outline[:, 1] += y
        if len(outline) > 40:
            outline = outline[:: len(outline) // 40 + 1]
        points.append(outline)
        owners.extend([len(points) - 1] * len(outline))

    if len(points) < 2:
        return mask

    coords = np.vstack(points)
    owner = np.asarray(owners)
    parent = list(range(len(points)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # Only each point's few nearest neighbours can ever be the shortest link
    # between two dashes, so this stays linear-ish even for a large max_gap.
    tree = cKDTree(coords)
    neighbours = min(12, len(coords))
    dist, idx = tree.query(coords, k=neighbours, distance_upper_bound=max_gap)
    dist, idx = np.atleast_2d(dist), np.atleast_2d(idx)

    source = np.repeat(np.arange(len(coords)), neighbours)
    a, b = source, idx.ravel()
    lengths = dist.ravel()
    valid = np.isfinite(lengths) & (b < len(coords)) & (owner[a] != owner[b.clip(max=len(coords) - 1)])
    a, b, lengths = a[valid], b[valid], lengths[valid]
    if len(a) == 0:
        return mask
    order = np.argsort(lengths)

    linked = mask.copy()
    for index in order:
        i, j = find(int(owner[a[index]])), find(int(owner[b[index]]))
        if i == j:
            continue
        parent[i] = j
        cv2.line(
            linked,
            tuple(int(v) for v in coords[a[index]]),
            tuple(int(v) for v in coords[b[index]]),
            255,
            2,
        )
    return linked


def pixel_to_lonlat(
    x: float, y: float, *, zoom: int, min_tile_x: int, min_tile_y: int, tile_size: int
) -> list[float]:
    world_size = tile_size * (2**zoom)
    world_x = min_tile_x * tile_size + x
    world_y = min_tile_y * tile_size + y
    lon = world_x / world_size * 360.0 - 180.0
    mercator_y = math.pi - 2.0 * math.pi * world_y / world_size
    lat = math.degrees(math.atan(math.sinh(mercator_y)))
    return [round(lon, 7), round(lat, 7)]


def polygon_area_km2(ring: list[list[float]]) -> float:
    mean_lat = math.radians(sum(point[1] for point in ring[:-1]) / (len(ring) - 1))
    projected = [
        (lon * 111.32 * math.cos(mean_lat), lat * 111.32) for lon, lat in ring
    ]
    twice_area = sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(projected, projected[1:])
    )
    return abs(twice_area) / 2


def trace(
    image_path: Path,
    metadata_path: Path,
    output: Path,
    target_rgb: tuple[int, int, int],
    tolerance: float,
    close_size: int,
    bridge_size: int,
    name: str | None = None,
    quiet: bool = False,
) -> bool:
    """Write a GeoJSON polygon. Returns whether the outline formed a closed ring."""
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    rgb = np.asarray(Image.open(image_path).convert("RGB"))
    pixels = rgb.astype(np.int32)

    # Preferred path: the same tiles were fetched without the spotlit overlay,
    # so anything that differs between the two renders is the outline itself.
    # This needs no colour assumptions and ignores POI pins and labels entirely.
    base_path = image_path.parent / image_path.name.replace("stitched", "base")
    if base_path.exists():
        base = np.asarray(Image.open(base_path).convert("RGB")).astype(np.int32)
        # Dropping the overlay also shifts Google's global tint by ~24, so only
        # much larger differences are real. Requiring the changed pixel to be
        # reddish then discards label and POI jitter, whatever its exact shade.
        changed = np.abs(pixels - base).sum(axis=2) > 40
        reddish = ((pixels[:, :, 0] - pixels[:, :, 1]) > 15) & (
            (pixels[:, :, 0] - pixels[:, :, 2]) > 15
        )
        mask = (changed & reddish).astype(np.uint8) * 255
    else:
        # Fallback: match the drawn colour directly, excluding yellow road
        # shields and the grey/red of map labels.
        target = np.array(target_rgb, dtype=np.int32)
        distance = np.sqrt(np.sum((pixels - target) ** 2, axis=2))
        mask = (
            (distance < tolerance)
            & (pixels[:, :, 0] > 180)
            & ((pixels[:, :, 0] - pixels[:, :, 1]) > 35)
            & ((pixels[:, :, 0] - pixels[:, :, 2]) > 35)
            & (np.abs(pixels[:, :, 1] - pixels[:, :, 2]) < 35)
        ).astype(np.uint8) * 255

    connected = link_dashes(mask, bridge_size)
    connected = cv2.dilate(
        connected, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_size, close_size))
    )
    # A sufficiently bridged ring encloses a large black component. Recover that
    # interior, then dilate by half the bridge width to move its edge back from
    # the inner side of the thickened line to approximately the source centerline.
    inverted = cv2.bitwise_not(connected)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(inverted)
    height, width = connected.shape
    enclosed: list[tuple[int, int]] = []
    # An interior worth reporting covers a real fraction of the mosaic; anything
    # smaller is a gap between dots rather than the area itself.
    min_interior = max(10_000, (height * width) // 400)
    for label in range(1, count):
        x, y, w, h, component_area = stats[label]
        touches_edge = x == 0 or y == 0 or x + w == width or y + h == height
        if not touches_edge and component_area > min_interior:
            enclosed.append((component_area, label))

    closed = bool(enclosed)
    if enclosed:
        _, interior_label = max(enclosed)
        interior = (labels == interior_label).astype(np.uint8) * 255
        # Only nudge the edge from the inner side of the drawn line to its
        # centre. Compensating for the full closing kernel would balloon the
        # whole ring, when closing actually only ate in near a bridged gap.
        radius = max(1, close_size // 2 + 2)
        polygon_mask = cv2.dilate(
            interior,
            cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1)
            ),
        )
        contours, _ = cv2.findContours(
            polygon_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
        )
    else:
        contours, _ = cv2.findContours(
            connected, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
        )
    if not contours:
        raise RuntimeError("No red outline found")

    contour = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(contour)
    if area < 1_000:
        raise RuntimeError(f"Largest red contour is too small ({area} pixels)")

    simplified = cv2.approxPolyDP(contour, epsilon=1.25, closed=True)
    tile_size = int(metadata["tile_width"])
    ring = [
        pixel_to_lonlat(
            float(point[0][0]),
            float(point[0][1]),
            zoom=int(metadata["zoom"]),
            min_tile_x=int(metadata["min_x"]),
            min_tile_y=int(metadata["min_y"]),
            tile_size=tile_size,
        )
        for point in simplified
    ]
    if ring[0] != ring[-1]:
        ring.append(ring[0])

    signed_area = sum(
        x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:])
    )
    if signed_area < 0:
        ring = list(reversed(ring))

    bbox = [
        min(point[0] for point in ring),
        min(point[1] for point in ring),
        max(point[0] for point in ring),
        max(point[1] for point in ring),
    ]
    feature = {
        "type": "FeatureCollection",
        "bbox": bbox,
        "features": [
            {
                "type": "Feature",
                "bbox": bbox,
                "properties": {
                    "name": name,
                    "source": "Google Maps selected-place raster outline",
                    "zoom": metadata["zoom"],
                    "vertices": len(ring) - 1,
                    "approx_area_km2": round(polygon_area_km2(ring), 3),
                    "closed_ring": closed,
                    "warning": (
                        "Reverse-traced from raster tiles; accuracy is limited "
                        "by Web Mercator pixel resolution at the captured zoom."
                    ),
                },
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        ],
    }
    output.write_text(json.dumps(feature, indent=2), encoding="utf-8")

    diagnostic = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    cv2.drawContours(diagnostic, [simplified], -1, (255, 0, 255), 2)
    cv2.imwrite(str(output.with_suffix(".trace.png")), diagnostic)
    cv2.imwrite(str(output.with_suffix(".mask.png")), connected)
    if not quiet:
        print(
            f"wrote {output}: {len(ring) - 1} vertices "
            f"from a {area:.0f}-pixel contour"
            + ("" if closed else " (open outline, traced largest fragment)")
        )
    return closed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--output", type=Path, default=Path("boundary.geojson"))
    parser.add_argument(
        "--target",
        default="234,67,53",
        help="RGB color at the center of the dotted outline",
    )
    parser.add_argument("--tolerance", type=float, default=105)
    parser.add_argument("--close-size", type=int, default=17)
    parser.add_argument("--bridge-size", type=int, default=17)
    args = parser.parse_args()
    if args.metadata:
        target = tuple(int(value) for value in args.target.split(","))
        if len(target) != 3:
            parser.error("--target must be R,G,B")
        trace(
            args.image,
            args.metadata,
            args.output,
            target,
            args.tolerance,
            args.close_size,
            args.bridge_size,
        )
    else:
        inspect_colors(args.image)


if __name__ == "__main__":
    main()
