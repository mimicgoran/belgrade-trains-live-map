"""
Phase 2a: download railway stations and tracks from OpenStreetMap (Overpass API).

Outputs (data_raw/osm/):
  - osm_stations.geojson  railway=station / railway=halt (points; areas are reduced to their centre)
  - osm_rail.geojson      railway=rail / light_rail tracks (LineStrings), with their OSM tags
  - osm_meta.json         query, bounding box, Overpass server and OSM data timestamp

The bounding box covers everything shown on the map: Novi Sad (west/north),
Lazarevac (south) and Pancevo Vojlovica (east). Vrsac and Subotica are not shown,
so they are not downloaded.

Data (c) OpenStreetMap contributors, ODbL 1.0.

Run from anywhere:  python data_prep/02_fetch_osm.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data_raw" / "osm"

# south, west, north, east (WGS84)
BBOX = (44.35, 19.75, 45.32, 20.75)

OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
HEADERS = {"User-Agent": "belgrade-trains-live-map (https://github.com/mimicgoran/belgrade-trains-live-map)"}

QUERY = """
[out:json][timeout:300][bbox:{s},{w},{n},{e}];
nwr["railway"~"^(station|halt)$"];
out tags center;
way["railway"~"^(rail|light_rail)$"];
out body geom;
""".format(s=BBOX[0], w=BBOX[1], n=BBOX[2], e=BBOX[3])

COORD_DECIMALS = 6  # ~0.1 m, plenty for this map


def fetch() -> tuple[dict, str]:
    last_error = None
    for url in OVERPASS_SERVERS:
        for attempt in range(1, 3):
            try:
                print(f"Querying {url} (attempt {attempt}) ...")
                r = requests.post(url, data={"data": QUERY}, headers=HEADERS, timeout=360)
                if r.status_code == 200:
                    return r.json(), url
                last_error = f"HTTP {r.status_code}: {r.text[:200]}"
            except (requests.RequestException, ValueError) as exc:
                last_error = str(exc)
            print(f"  failed: {last_error}")
            time.sleep(10 * attempt)
    raise RuntimeError(f"All Overpass servers failed. Last error: {last_error}")


def rnd(x: float) -> float:
    return round(x, COORD_DECIMALS)


def to_geojson(data: dict) -> tuple[dict, dict]:
    stations, rails = [], []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        props = {"osm_type": el["type"], "osm_id": el["id"], **tags}
        railway = tags.get("railway")
        if railway in ("station", "halt"):
            if el["type"] == "node":
                lon, lat = el["lon"], el["lat"]
            elif "center" in el:
                lon, lat = el["center"]["lon"], el["center"]["lat"]
            else:
                continue
            stations.append({"type": "Feature", "properties": props,
                             "geometry": {"type": "Point", "coordinates": [rnd(lon), rnd(lat)]}})
        elif railway in ("rail", "light_rail") and el["type"] == "way" and el.get("geometry"):
            coords = [[rnd(p["lon"]), rnd(p["lat"])] for p in el["geometry"] if p]
            if len(coords) >= 2:
                props["node_ids"] = el.get("nodes", [])
                rails.append({"type": "Feature", "properties": props,
                              "geometry": {"type": "LineString", "coordinates": coords}})
    fc = lambda feats: {"type": "FeatureCollection", "features": feats}
    return fc(stations), fc(rails)


def main() -> int:
    data, server = fetch()
    stations, rails = to_geojson(data)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, fc in (("osm_stations.geojson", stations), ("osm_rail.geojson", rails)):
        with open(OUT_DIR / name, "w", encoding="utf-8") as f:
            json.dump(fc, f, ensure_ascii=False, separators=(",", ":"))
    meta = {
        "source": "OpenStreetMap via Overpass API",
        "license": "ODbL 1.0, (c) OpenStreetMap contributors",
        "server": server,
        "osm_base_timestamp": data.get("osm3s", {}).get("timestamp_osm_base"),
        "downloaded_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bbox_south_west_north_east": BBOX,
        "query": QUERY.strip(),
        "counts": {"stations": len(stations["features"]), "rail_ways": len(rails["features"])},
    }
    with open(OUT_DIR / "osm_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"Stations: {meta['counts']['stations']}, rail ways: {meta['counts']['rail_ways']}")
    print(f"OSM data timestamp: {meta['osm_base_timestamp']}")
    print(f"Written to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
