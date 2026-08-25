#!/usr/bin/env python3
"""Live --mode fast --refresh for the region-UX catalog. Never times a cache hit."""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

PLACES = [
    ("india", "Koramangala, Bengaluru", ROOT / "data" / "koramangala.geojson"),
    ("world", "Lagos, Nigeria", ROOT / "data" / "lagos.geojson"),
    ("world", "Tokyo, Japan", ROOT / "data" / "tokyo.geojson"),
    ("world", "Sydney, Australia", ROOT / "data" / "sydney.geojson"),
]


def summarize(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    props = data["features"][0]["properties"]
    return {
        "name": props.get("name"),
        "closed": bool(props.get("closed_ring")),
        "vertices": props.get("vertices"),
        "km2": props.get("approx_area_km2"),
        "zoom": props.get("zoom"),
        "extracted_at": props.get("extracted_at"),
        "mode": props.get("mode"),
    }


def run_place(group: str, query: str, output: Path) -> dict[str, object]:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-u",
        str(ROOT / "area_boundary.py"),
        query,
        "--output",
        str(output),
        "--mode",
        "fast",
        "--refresh",
    ]
    print(f"\n--- {group} {query} -> {output} ---", flush=True)
    started = time.perf_counter()
    proc = subprocess.run(cmd, cwd=ROOT)
    elapsed = time.perf_counter() - started
    row: dict[str, object] = {
        "group": group,
        "query": query,
        "output": str(output.relative_to(ROOT)).replace("\\", "/"),
        "latency_s": round(elapsed, 2),
        "exit": proc.returncode,
        "closed": None,
        "vertices": None,
        "km2": None,
        "zoom": None,
        "extracted_at": None,
        "mode": None,
        "name": None,
        "error": None,
    }
    if output.exists():
        row.update(summarize(output))
    if proc.returncode != 0:
        row["error"] = f"exit {proc.returncode}"
    print(f"latency {row['latency_s']}s", flush=True)
    return row


def print_table(rows: list[dict[str, object]]) -> None:
    print("\n=== live latency (mode=fast, --refresh, no cache) ===", flush=True)
    print(
        "prior 5-city balanced-ish median 14.13s; prior 3-city fast Lagos 1.66 Tokyo 2.03 Sydney 1.84 (history only)",
        flush=True,
    )
    header = (
        f"{'group':<7} {'place':<22} {'closed':<8} {'verts':>6} {'km2':>10} "
        f"{'z':>3} {'extracted_at':<22} {'s':>7} error"
    )
    print(header, flush=True)
    print("-" * len(header), flush=True)
    times = []
    for row in rows:
        closed = row["closed"]
        closed_s = "—" if closed is None else ("yes" if closed else "no")
        verts = "—" if row["vertices"] is None else str(row["vertices"])
        km2 = "—" if row["km2"] is None else f"{row['km2']}"
        zoom = "—" if row["zoom"] is None else str(row["zoom"])
        stamp = row.get("extracted_at") or "—"
        err = row["error"] or ""
        label = str(row.get("name") or row["query"])
        print(
            f"{row['group']:<7} {label:<22} {closed_s:<8} {verts:>6} {km2:>10} "
            f"{zoom:>3} {stamp:<22} {row['latency_s']:>7.2f} {err}",
            flush=True,
        )
        times.append(float(row["latency_s"]))
    total = sum(times)
    median = statistics.median(times) if times else 0.0
    peak = max(times) if times else 0.0
    print("-" * len(header), flush=True)
    print(
        f"total {total:.2f}s   median {median:.2f}s   max {peak:.2f}s   n={len(rows)}",
        flush=True,
    )


def main() -> int:
    rows = [run_place(group, query, output) for group, query, output in PLACES]
    print_table(rows)
    sidecar = ROOT / "viewer" / "last_bench.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps({"rows": rows}, indent=2), encoding="utf-8")
    print(f"wrote {sidecar}", flush=True)
    return 0 if all(r["exit"] == 0 for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
