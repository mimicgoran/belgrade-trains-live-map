"""
Phase 4: export the data the web map loads (web/data/).

  - lines.geojson     one LineString per route: the track the train icons move along.
                      The map hides it by default and shows it on request.
  - stations.geojson  timetable stations as points on the track, with their km on each route
  - trains.json       every train with its stops: km along its route and times in minutes
                      from midnight (times after midnight continue above 1440)

Inputs: data_interim/ (stop_times.csv, train_routes.csv, routes.geojson,
route_stations.csv, stations_matched.csv).

Run from anywhere:  python data_prep/04_export.py
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
INTERIM = ROOT / "data_interim"
OUT = ROOT / "web" / "data"

# Display names (Serbian, Latin script) and colours of the lines.
LINES = {
    "batajnica-ovca": {"name": "BG voz: Batajnica – Ovča", "color": "#d62728"},
    "lazarevac-resnik-ovca": {"name": "BG voz: Lazarevac – Resnik – Ovča", "color": "#2ca02c"},
    "beograd-novi-sad": {"name": "Beograd – Novi Sad", "color": "#1f77b4"},
    "pancevo-vojlovica-zemun": {"name": "Pančevo Vojlovica – Zemun", "color": "#9467bd"},
    "vrsac-zemun": {"name": "Vršac – Zemun", "color": "#ff7f0e"},
}

# Public holidays (run as "nedelja"), as listed in the BG voz timetable
# rv_bg_voza_za_2025.pdf ("dan državnog praznika").
HOLIDAYS = [
    "2026-01-01", "2026-01-02", "2026-01-07",
    "2026-02-15", "2026-02-16", "2026-02-17",
    "2026-04-10", "2026-04-12", "2026-04-13",
    "2026-05-01", "2026-05-02",
    "2026-11-11",
]

to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32634", always_xy=True)
to_wgs = Transformer.from_crs("EPSG:32634", "EPSG:4326", always_xy=True)


def project_on_line(pt, line_xy):
    """Closest point of a polyline to pt (all in metres)."""
    best, best_d = None, math.inf
    px, py = pt
    for (x1, y1), (x2, y2) in zip(line_xy, line_xy[1:]):
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
        qx, qy = x1 + t * dx, y1 + t * dy
        d = math.hypot(px - qx, py - qy)
        if d < best_d:
            best, best_d = (qx, qy), d
    return best, best_d


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def main() -> int:
    stop_rows = read_csv(INTERIM / "stop_times.csv")
    train_routes = {r["train_id"]: r for r in read_csv(INTERIM / "train_routes.csv")}
    route_st = read_csv(INTERIM / "route_stations.csv")
    matched = {r["station"]: r for r in read_csv(INTERIM / "stations_matched.csv")}
    routes = json.load(open(INTERIM / "routes.geojson", encoding="utf-8"))["features"]

    errors = []
    missing = {f["properties"]["line"] for f in routes} - set(LINES)
    if missing:
        errors.append(f"No display name/colour for lines: {sorted(missing)}")

    km = {(r["route_id"], r["station"]): float(r["km"]) for r in route_st}

    # lines.geojson
    line_feats = []
    route_xy = {}
    for f in routes:
        p = f["properties"]
        route_xy[p["route_id"]] = [to_utm.transform(*c) for c in f["geometry"]["coordinates"]]
        info = LINES.get(p["line"], {})
        line_feats.append({"type": "Feature",
                           "properties": {"route": p["route_id"], "line": p["line"],
                                          "name": info.get("name", p["line"]), "color": info.get("color"),
                                          "length_km": p["length_km"]},
                           "geometry": f["geometry"]})

    # stations.geojson: OSM station point projected onto the nearest of its routes
    st_routes = defaultdict(dict)
    st_lines = defaultdict(set)
    route_line = {f["properties"]["route_id"]: f["properties"]["line"] for f in routes}
    for (rid, st), k in km.items():
        st_routes[st][rid] = round(k, 3)
        st_lines[st].add(route_line[rid])
    st_feats = []
    for st in sorted(st_routes):
        m = matched[st]
        pt = to_utm.transform(float(m["lon"]), float(m["lat"]))
        best = min((project_on_line(pt, route_xy[rid]) for rid in st_routes[st]), key=lambda t: t[1])
        lon, lat = to_wgs.transform(*best[0])
        st_feats.append({"type": "Feature",
                         "properties": {"name": st, "lines": sorted(st_lines[st]),
                                        "km": st_routes[st], "osm_id": m["osm_id"]},
                         "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]}})

    # trains.json
    by_train = defaultdict(list)
    for r in stop_rows:
        by_train[r["train_id"]].append(r)
    trains = []
    for tid, rows in by_train.items():
        rows.sort(key=lambda r: int(r["seq"]))
        tr = train_routes[tid]
        rid = tr["route_id"]
        stops = []
        for r in rows:
            if r["stop_type"] != "stop":
                continue
            stops.append({"station": r["station"], "km": round(km[(rid, r["station"])], 3),
                          "arr": int(r["arr_min"]) if r["arr_min"] else None,
                          "dep": int(r["dep_min"]) if r["dep_min"] else None})
        first = rows[0]
        # checks: times and km must move consistently
        t_prev, k_prev = None, None
        sign = 1 if tr["direction"] == "forward" else -1
        for s in stops:
            for t in (s["arr"], s["dep"]):
                if t is not None:
                    if t_prev is not None and t < t_prev:
                        errors.append(f"Train {tid}: time goes back at {s['station']}")
                    t_prev = t
            if k_prev is not None and (s["km"] - k_prev) * sign <= 0:
                errors.append(f"Train {tid}: km does not progress at {s['station']}")
            k_prev = s["km"]
        if stops[0]["arr"] is not None or stops[-1]["dep"] is not None or len(stops) < 2:
            errors.append(f"Train {tid}: bad first/last stop")
        trains.append({
            "id": tid,
            "line": first["line"],
            "route": rid,
            "category": first["category"] or None,
            "day_types": first["day_types"].split(";"),
            "origin": first["origin"],
            "destination": first["destination"],
            "stops": stops,
        })
    trains.sort(key=lambda t: (t["line"], t["stops"][0]["dep"], t["id"]))

    sources = {}
    for r in stop_rows:
        sources.setdefault(r["source"], {"valid_from": r["valid_from"], "valid_to": r["valid_to"] or None})

    if errors:
        for e in errors:
            print("ERROR:", e)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "lines.geojson", "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": line_feats}, f, ensure_ascii=False, separators=(",", ":"))
    with open(OUT / "stations.geojson", "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": st_feats}, f, ensure_ascii=False, separators=(",", ":"))
    with open(OUT / "trains.json", "w", encoding="utf-8") as f:
        json.dump({
            "generated": date.today().isoformat(),
            "timezone": "Europe/Belgrade",
            "time_unit": "minutes from midnight of the service day; values above 1440 are after midnight",
            "day_types": {"radni": "Monday-Friday", "subota": "Saturday",
                          "nedelja": "Sunday and public holidays"},
            "holidays": HOLIDAYS,
            "sources": sources,
            "lines": {k: v for k, v in LINES.items()},
            "trains": trains,
        }, f, ensure_ascii=False, separators=(",", ":"))

    print(f"lines.geojson:    {len(line_feats)} routes")
    print(f"stations.geojson: {len(st_feats)} stations")
    print(f"trains.json:      {len(trains)} trains, {sum(len(t['stops']) for t in trains)} stops")
    for name in ("lines.geojson", "stations.geojson", "trains.json"):
        print(f"  {name}: {(OUT / name).stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
