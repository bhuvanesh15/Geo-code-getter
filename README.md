# Geo-code-getter

Turn a place name into **lat/lng coordinates** as GeoJSON.

This is an unofficial replay of Google Maps `preview/place` + `maps/vt` tiles. It is **not** the official Geocoding or Places API (those only give a box, not the dashed region).

| Command | What you get | Typical time |
|---|---|---|
| `maps_area_bounds.py` | Axis-aligned **bounding box** | ~1 request |
| `area_boundary.py --mode fast` | Coarse **polygon** (one zoom, one kernel) | ~2 s |
| `area_boundary.py --mode balanced` | Better neighborhood/city ring | ~6–15 s |
| `area_boundary.py --mode hi-fi --zoom 15` | High-detail city ring (many tiles) | tens of seconds |
| `viewer_server.py` | Local map UI: search → fill outline, latency + JSON | same as extract |

---

## Install

```bash
pip install -r requirements.txt
```

---

## Quick run

```bash
# Bounding box
python maps_area_bounds.py "HSR Layout, Bengaluru"

# Outline (default is --mode fast). --refresh skips a cached .geojson
python area_boundary.py "Tokyo, Japan" --refresh --output data/tokyo.geojson

# Correct neighborhood layout (slower)
python area_boundary.py "Anna Nagar West, Chennai" --mode balanced --refresh --output data/anna_nagar_west.geojson

# High accuracy city (wait for tiles)
python area_boundary.py "Tiruchirappalli, Tamil Nadu" --mode hi-fi --zoom 15 --refresh --output data/tiruchirappalli.geojson
```

Open [geojson.io](https://geojson.io/) → **Open → File**. Do not paste large files (e.g. Trichy ~2400 lines) into a geojson.io URL.

`closed_ring: true` means an enclosed interior. `false` is a fragment — still a GeoJSON loop (first point = last), but not the full region.

---

## Modes

| `--mode` | Zoom cap | Tiles | Close retry | Use when |
|---|---|---|---|---|
| `fast` (default) | 10 | ≤24 pairs, no margin | **None** (one kernel; open ring is written and stop) | Latency SLO |
| `balanced` | 13 | more tiles + margin | Several gap sizes, then step zoom | Neighborhoods / cities that must close |
| `hi-fi` | 13 (or `--zoom 15`) | up to ~420 pairs | Same search as balanced | Max vertex detail |

Other flags: `--output`, `--refresh`, `--keep-tiles`, `--workers`, `--max-tiles`, `--target-px`.

Western longitudes (e.g. Delaware) must be encoded as Maps **uint32 E7**. Signed negatives on spotlit tiles return **HTTP 400**. 4xx is not retried (except 429).

---

## Local map viewer

```bash
python viewer_server.py
```

Open http://127.0.0.1:8000/viewer/

- Search runs `--mode fast --refresh` and fills **Leaflet** + OSM (not Google map tiles).
- HUD: closed/fragment, km², vertices, zoom, `extracted_at`, latency.
- Current / previous JSON panels.
- Recent list (up to 12) in `viewer/recent.json`.

Leaflet is free (BSD). OSM tiles are for light local use.

### Public sample UI (Vercel)

Static HTML + GeoJSON (no live Maps extract). Import this GitHub repo in Vercel, framework **Other**, no build. Root URL rewrites to `/viewer/index.html`.

Catalog: Anna Nagar West, Tiruchirappalli, Koramangala, Lagos, Tokyo, Sydney.

---

## Bench

```bash
python -u bench_region_ux.py
```

Live `--mode fast --refresh` for Koramangala, Lagos, Tokyo, Sydney. Does not time a cache hit.

---

## Samples in `data/`

| File | Notes |
|---|---|
| `anna_nagar_west.geojson` | balanced, closed, z13, ~9 km² |
| `tiruchirappalli.geojson` | hi-fi z15, closed, ~590 verts |
| `tokyo.geojson` / `lagos.geojson` / `sydney.geojson` | fast catalog cities |
| `koramangala.geojson` | fast can be a fragment at z10 |

---

## How it works

```
place name
    → maps/preview/place     (id, gcid, bbox, E7)
    → maps/vt spotlit + base  (PNG tiles, parallel GETs, TLS session reuse)
    → difference mask + morph-close
    → GeoJSON Polygon
```

There is no public vector outline. The dashed line is raster tiles.

**Cost ladder:** HTTP replay → ScrapingBee `custom_google=true` if blocked → Playwright HAR only to recapture a drifted `pb` / tile epoch.

Key in `.cursor/env/scrapingbee.env` (`SCRAPINGBEE_API_KEY=`). Do not commit it.

---

## License / use

Unofficial public-data extraction. Endpoints can change (HTTP 400). Not a production SLA or official Google geometry. Be polite to the tile endpoint.
