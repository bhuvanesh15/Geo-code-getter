#!/usr/bin/env python3
"""Trace Google Maps' dotted locality outline from a stitched raster tile image."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from scipy.spatial import cKDTree


def outline_mask(
    rgb: np.ndarray,
    base_path: Path | None,
    target_rgb: tuple[int, int, int],
    tolerance: float,
    base_rgb: np.ndarray | None = None,
) -> np.ndarray:
    """Pixels that belong to Maps' selected-place outline, not POI pins."""
    base = None
    if base_rgb is not None:
        base = np.asarray(base_rgb)
        if base.ndim == 3 and base.shape[2] == 4:
            base = base[:, :, :3]
        base = base.astype(np.int16)
    elif base_path is not None and Path(base_path).exists():
        base = np.asarray(Image.open(base_path).convert("RGB")).astype(np.int16)
    if base is not None:
        change = np.abs(rgb.astype(np.int16) - base).sum(axis=2)
        # Overlay tint is ~35. Outline pixels are the heavy tail (~100–400).
        threshold = max(50.0, float(np.percentile(change, 99.15)))
        binary = (change > threshold).astype(np.uint8) * 255
    else:
        pixels = rgb.astype(np.int32)
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
        hsv_red = (
            ((hsv[:, :, 0] <= 10) | (hsv[:, :, 0] >= 168))
            & (hsv[:, :, 1] >= 70)
            & (hsv[:, :, 2] >= 70)
        )
        target = np.array(target_rgb, dtype=np.int32)
        distance = np.sqrt(np.sum((pixels - target) ** 2, axis=2))
        binary = (
            hsv_red
            | (
                (distance < tolerance)
                & (pixels[:, :, 0] > 180)
                & ((pixels[:, :, 0] - pixels[:, :, 1]) > 35)
                & ((pixels[:, :, 0] - pixels[:, :, 2]) > 35)
            )
        ).astype(np.uint8) * 255
    return cv2.dilate(binary, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))


def close_kernel(zoom: int, bridge_size: int) -> int:
    """Dash spacing in pixels grows with zoom; ~71px closed Coimbatore at z12."""
    sized = int(round(71 * (2 ** (zoom - 12))))
    k = max(sized, bridge_size)
    k = max(31, min(k, 181))
    return k if k % 2 else k + 1


def close_open_contour(connected: np.ndarray) -> np.ndarray:
    """If the outline is one chain with two ends, draw a closing segment."""
    skeleton = connected.copy()
    contours, _ = cv2.findContours(skeleton, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return connected
    contour = max(contours, key=len)
    pts = contour.reshape(-1, 2)
    if len(pts) < 50:
        return connected
    # Endpoints of a stroke-like contour are the pair of points with max
    # geodesic separation along the contour that are also spatially close
    # relative to the contour length (the remaining gap).
    n = len(pts)
    step = max(1, n // 400)
    sampled = pts[::step]
    tree = cKDTree(sampled)
    dist, idx = tree.query(sampled, k=min(8, len(sampled)))
    dist = np.atleast_2d(dist)
    idx = np.atleast_2d(idx)
    best = None
    for i, neighbours in enumerate(idx):
        for k, j in enumerate(neighbours[1:]):
            if not np.isfinite(dist[i, k + 1]):
                continue
            along = min(abs(i - j), len(sampled) - abs(i - j)) * step
            gap = float(dist[i, k + 1])
            if along < n * 0.4:
                continue
            if gap > max(40.0, n * 0.08):
                continue
            score = along / (gap + 1.0)
            if best is None or score > best[0]:
                best = (score, tuple(int(v) for v in sampled[i]), tuple(int(v) for v in sampled[j]))
    if best is None:
        return connected
    closed = connected.copy()
    cv2.line(closed, best[1], best[2], 255, 3)
    return closed


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


def link_dashes(mask: np.ndarray, max_gap: int, min_dash: int = 3) -> np.ndarray:
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
    image_path: Path | None,
    metadata_path: Path | None,
    output: Path,
    target_rgb: tuple[int, int, int],
    tolerance: float,
    close_size: int,
    bridge_size: int,
    name: str | None = None,
    quiet: bool = False,
    rgb: np.ndarray | None = None,
    base_rgb: np.ndarray | None = None,
    metadata: dict | None = None,
    write_diagnostics: bool = True,
    mode: str | None = None,
) -> bool:
    """Write a GeoJSON polygon. Returns whether the outline formed a closed ring."""
    if metadata is None:
        if metadata_path is None:
            raise ValueError("metadata or metadata_path is required")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if rgb is None:
        if image_path is None:
            raise ValueError("rgb or image_path is required")
        rgb = np.asarray(Image.open(image_path).convert("RGB"))
    elif rgb.ndim == 3 and rgb.shape[2] == 4:
        rgb = rgb[:, :, :3]
    base_path = None
    if image_path is not None:
        base_path = image_path.parent / image_path.name.replace("stitched", "base")
    mask = outline_mask(rgb, base_path, target_rgb, tolerance, base_rgb=base_rgb)
    kernel = close_kernel(int(metadata["zoom"]), bridge_size)
    connected = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel, kernel))
    )
    # A closed outline turns the city into a hole in the background. Recover
    # that hole; if the dashes never join, fall back to linking them.
    inverted = cv2.bitwise_not(connected)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(inverted)
    height, width = connected.shape
    enclosed: list[tuple[int, int]] = []
    min_interior = max(10_000, (height * width) // 400)
    for label in range(1, count):
        x, y, w, h, component_area = stats[label]
        touches_edge = x == 0 or y == 0 or x + w == width or y + h == height
        if not touches_edge and component_area > min_interior:
            enclosed.append((component_area, label))

    if not enclosed:
        connected = link_dashes(mask, max(bridge_size, kernel))
        connected = cv2.dilate(
            connected, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_size, close_size))
        )
        connected = close_open_contour(connected)
        inverted = cv2.bitwise_not(connected)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(inverted)
        enclosed = []
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
                    "extracted_at": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "mode": mode,
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

    if write_diagnostics:
        diagnostic = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2BGR)
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
