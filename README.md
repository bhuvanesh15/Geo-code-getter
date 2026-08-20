# Geo-code-getter

Turn a place name into **lat/lng coordinates** as GeoJSON.

Two outputs:

| Command | What you get | Speed |
|---|---|---|
| `maps_area_bounds.py` | Axis-aligned **bounding box** (4 corners) | ~1 request, a few seconds |
| `area_boundary.py` | **Polygon outline** traced from Google Maps tiles | tens of seconds to a couple of minutes |

This is an unofficial replay of Google Maps' `preview/place` + `maps/vt` tile protocol. It is not the official Geocoding API.

---

## Quick run

```bash
pip install -r requirements.txt

# 1) Bounding box (fast)
python maps_area_bounds.py "HSR Layout, Bengaluru"

# 2) Real outline → GeoJSON + geojson.io link
python area_boundary.py "Koramangala, Bengaluru"
```

Open the printed `geojson.io` URL, **or** drag the `.geojson` file onto [geojson.io](https://geojson.io/).

Sample already in the repo: `data/bengaluru_boundary.geojson`.

---

## View coordinates (Bengaluru sample)

`data/bengaluru_boundary.geojson` is **large** (~900 vertices, ~3.7k lines). Do **not** paste it into a geojson.io URL — the link will be too long for the browser.

Use one of these:

1. Open [geojson.io](https://geojson.io/) → **Open → File** → pick `data/bengaluru_boundary.geojson`
2. Copy the file contents and paste into the JSON panel on the right of geojson.io
3. Smaller samples (`data/koramangala.geojson`, `data/hsr_layout.geojson`) are short enough that the script's printed URL usually works

---

## Commands

### Bounding box

```bash
python maps_area_bounds.py "HSR Layout, Bengaluru"
python maps_area_bounds.py "Brooklyn, New York" --json
python maps_area_bounds.py "Koramangala" --geojson out.json
python maps_area_bounds.py "Anna Nagar, Chennai" --urls
```

`--urls` prints Google Maps links for the SW/NE corners so you can eyeball the box.

`--scrapingbee` routes the fetch through ScrapingBee if Google blocks you. Put `SCRAPINGBEE_API_KEY=` in `.cursor/env/scrapingbee.env` (not committed).

### Polygon outline

```bash
python area_boundary.py "Koramangala, Bengaluru"
python area_boundary.py "HSR Layout, Bengaluru" --output data/hsr_layout.geojson
python area_boundary.py "Tamil Nadu" --output data/tamil_nadu.geojson
python area_boundary.py "Bengaluru, Karnataka" --keep-tiles
```

| Flag | Meaning |
|---|---|
| *(no `--zoom`)* | Auto-picks zoom from the place size, then steps down if the dashed outline does not close |
| `--zoom 14` | Force a tile zoom. **Do not reuse Bengaluru's 14 for a whole state** |
| `--output file.geojson` | Output path (default: `<place>.geojson`) |
| `--keep-tiles` | Keep the stitched mosaic for debugging |
| `--workers 8` | Parallel tile downloads |

**Zoom vs area size**

- City like Bengaluru → zoom ~14, ~1–2 minutes, detailed outline
- Neighborhood like HSR / Koramangala → omit `--zoom`; forcing 16–17 often fails to close the dashed ring
- State like Tamil Nadu → omit `--zoom` (script picks ~9–10). `--zoom 14` would request tens of thousands of tiles

Each new place is a **new run**. Nothing is reused from `bengaluru_boundary.geojson`.

---

## System design

Reads never crawl Google. A lookup is: cache (if you add one) → else one extractor job.

```
place name
    │
    ▼
maps/preview/place     ← unofficial XHR (pb template, E7 ints)
    │  name, feature_id, mid, gcid, center, bbox
    ▼
maps/vt spotlit tiles  ← PNG outline, not a vector API
    │
    ▼
stitch + trace dashes  ← CPU; GeoJSON Polygon in WGS84
    │
    ▼
.geojson + geojson.io URL
```

**Why a rectangle first?** `preview/place` only returns the viewport box (`[[sw],[ne]]` as E7 integers). The red dashed outline on Maps is **rasterized into map tiles**. There is no public polygon payload. The outline path downloads those tiles and traces the red pixels back to lat/lng.

**Cost ladder**

1. HTTP replay of `preview/place` (`curl_cffi`) — bbox
2. Same session + `maps/vt` PNGs — polygon
3. ScrapingBee `custom_google=true` — if Google blocks
4. Browser HAR capture — only to recapture a drifted `pb` / tile template

**Hot vs cold**

| Path | What | Typical time |
|---|---|---|
| Hot | Serve a saved `.geojson` | milliseconds |
| Warm miss | `preview/place` bbox | ~0.5–2 s |
| Cold | tile fetch + trace | 30 s – 2 min (auto zoom) |

**Failure modes**

- HTTP 400 on `preview/place` → `pb` template drifted; recapture from a live Maps load
- Open ring (`closed_ring: false`) → dashed outline did not join; file is still written (largest fragment). Inspect `*.trace.png` if you passed `--keep-tiles`
- Soft block (200 + captcha HTML) → step up transport, do not parse garbage

**What this is not:** OSM administrative polygons, Google's official Geocoding API, or a street-accurate cadastral boundary. Pixel resolution at the chosen Web Mercator zoom is the accuracy ceiling.

---

## Repo layout

```
area_boundary.py          # outline driver (use this)
maps_area_bounds.py       # bbox-only
trace_maps_boundary.py    # dash linker + GeoJSON writer
fetch_boundary_tiles.py   # optional: replay tiles from a HAR
capture_maps_boundary.py  # optional: Playwright HAR capture
extract_maps_tiles.py     # optional: stitch tiles out of a HAR
data/                     # sample GeoJSON outlines
  bengaluru_boundary.geojson
  koramangala.geojson
  hsr_layout.geojson
```

---

## License / use

Public-data extraction for your own pipelines. Be polite to Google's tile endpoint (the script already caps tile count). Do not treat unofficial endpoints as a stable SLA.
