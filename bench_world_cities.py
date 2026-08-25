#!/usr/bin/env python3
"""Sequential full-outline runs for five worldwide cities; print latency table."""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

CITIES = [
    ("Brooklyn, New York", Path("data/brooklyn.geojson")),
    ("London, United Kingdom", Path("data/london.geojson")),
    ("Lagos, Nigeria", Path("data/lagos.geojson")),
    ("Tokyo, Japan", Path("data/tokyo.geojson")),
    ("Sydney, Australia", Path("data/sydney.geojson")),
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
    }


def run_city(query: str, output: Path) -> dict[str, object]:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-u",
        "area_boundary.py",
        query,
        "--output",
        str(output),
        "--mode",
        "fast",
        "--refresh",
    ]
    print(f"\n--- {query} -> {output} ---", flush=True)
    started = time.perf_counter()
    proc = subprocess.run(cmd)
    elapsed = time.perf_counter() - started
    row: dict[str, object] = {
        "query": query,
        "output": str(output),
        "latency_s": round(elapsed, 2),
        "exit": proc.returncode,
        "closed": None,
        "vertices": None,
        "km2": None,
        "zoom": None,
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
    print("\n=== latency summary (mode=fast, cold --refresh) ===", flush=True)
    print("baseline (balanced-ish): Brooklyn 10.73s  London 29.56s  Lagos 14.09s  Tokyo 14.13s  Sydney 14.42s  median 14.13s", flush=True)
    header = f"{'city':<28} {'closed':<8} {'verts':>6} {'km2':>10} {'s':>8} error"
    print(header, flush=True)
    print("-" * len(header), flush=True)
    times = []
    for row in rows:
        closed = row["closed"]
        closed_s = "—" if closed is None else ("yes" if closed else "no")
        verts = "—" if row["vertices"] is None else str(row["vertices"])
        km2 = "—" if row["km2"] is None else f"{row['km2']}"
        err = row["error"] or ""
        label = str(row.get("name") or row["query"])
        print(
            f"{label:<28} {closed_s:<8} {verts:>6} {km2:>10} {row['latency_s']:>8.2f} {err}",
            flush=True,
        )
        times.append(float(row["latency_s"]))
    total = sum(times)
    median = statistics.median(times) if times else 0.0
    print("-" * len(header), flush=True)
    print(f"total {total:.2f}s   median {median:.2f}s   n={len(rows)}", flush=True)


def main() -> int:
    rows = [run_city(query, output) for query, output in CITIES]
    print_table(rows)
    return 0 if all(r["exit"] == 0 for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
