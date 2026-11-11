"""
Phase 2b: link every station used in the timetable to an OpenStreetMap station.

Inputs:
  - data_interim/stop_times.csv            (from 01_parse_timetable.py)
  - data_raw/osm/osm_stations.geojson      (from 02_fetch_osm.py)
  - data_prep/config/station_osm_overrides.csv   manual links, used first

Output:
  - data_interim/stations_matched.csv  station -> OSM id, OSM name, lat/lon, how it was matched

Automatic matching compares the timetable name with the OSM names (name:sr-Latn,
int_name, alt_name:sr-Latn and a transliteration of the Cyrillic name / alt_name),
case-insensitive. A station that does not match exactly one OSM location stops the
script, so it has to be added to the overrides file.

Check at the end: straight-line speed between consecutive stops of every train.
Implausible speeds usually mean a wrong match.

Run from anywhere:  python data_prep/02b_match_stations.py
"""

from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STOP_TIMES = ROOT / "data_interim" / "stop_times.csv"
OSM_STATIONS = ROOT / "data_raw" / "osm" / "osm_stations.geojson"
OVERRIDES = Path(__file__).resolve().parent / "config" / "station_osm_overrides.csv"
OUT = ROOT / "data_interim" / "stations_matched.csv"

SAME_PLACE_M = 200      # OSM duplicates closer than this are treated as one station
MAX_SPEED_KMH = 160     # straight-line speed above this is suspicious
MIN_SPEED_KMH = 10      # ... and below this (only for hops longer than 1 km)

CYR2LAT = {
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Ђ": "Đ", "Е": "E", "Ж": "Ž",
    "З": "Z", "И": "I", "Ј": "J", "К": "K", "Л": "L", "Љ": "Lj", "М": "M", "Н": "N",
    "Њ": "Nj", "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T", "Ћ": "Ć", "У": "U",
    "Ф": "F", "Х": "H", "Ц": "C", "Ч": "Č", "Џ": "Dž", "Ш": "Š",
}
CYR2LAT.update({k.lower(): v.lower() for k, v in CYR2LAT.items()})


def translit(text: str) -> str:
    return "".join(CYR2LAT.get(ch, ch) for ch in text)


def norm(text: str) -> str:
    return " ".join(translit(text).lower().replace("-", " ").split())


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def osm_key(p: dict) -> str:
    return f"{p['osm_type']}/{p['osm_id']}"


def osm_names(p: dict) -> set[str]:
    names = set()
    for k in ("name:sr-Latn", "int_name", "alt_name:sr-Latn", "name", "alt_name", "official_name"):
        if p.get(k):
            for part in p[k].split(";"):
                names.add(norm(part))
    return names


def pick_one(cands: list[dict]) -> dict:
    """Several OSM objects for one station (node + relation, stop position + station node):
    prefer a node tagged public_transport=station, then any node, then the rest."""
    def rank(f):
        p = f["properties"]
        return (p["osm_type"] != "node", p.get("public_transport") != "station", p["osm_id"])
    return sorted(cands, key=rank)[0]


def main() -> int:
    with open(STOP_TIMES, encoding="utf-8", newline="") as f:
        stop_rows = list(csv.DictReader(f))
    stations = sorted({r["station"] for r in stop_rows})

    osm = json.load(open(OSM_STATIONS, encoding="utf-8"))["features"]
    by_key = {osm_key(f["properties"]): f for f in osm}

    overrides = {}
    if OVERRIDES.exists():
        with open(OVERRIDES, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                overrides[row["station"].strip()] = (row["osm_id"].strip(), row["note"].strip())

    results, problems = [], []
    for st in stations:
        if st in overrides:
            key, note = overrides[st]
            if key not in by_key:
                problems.append(f"{st}: override {key} not found in OSM data")
                continue
            results.append((st, by_key[key], "manual", note))
            continue
        cands = [f for f in osm if norm(st) in osm_names(f["properties"])]
        if not cands:
            problems.append(f"{st}: no OSM station with this name - add it to {OVERRIDES.name}")
            continue
        first = cands[0]["geometry"]["coordinates"]
        spread = max(haversine_m(first[1], first[0], c["geometry"]["coordinates"][1],
                                 c["geometry"]["coordinates"][0]) for c in cands)
        if spread > SAME_PLACE_M:
            names = ", ".join(osm_key(c["properties"]) for c in cands)
            problems.append(f"{st}: several OSM stations far apart ({names}) - add it to {OVERRIDES.name}")
            continue
        chosen = pick_one(cands)
        note = ""
        if chosen["properties"].get("disused:railway") or chosen["properties"].get("disused"):
            note = "OSM marks this station as disused"
        results.append((st, chosen, "name", note))

    for msg in problems:
        print("ERROR:", msg)
    if problems:
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    coords = {}
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["station", "osm_id", "osm_name", "osm_name_latn", "osm_railway", "lat", "lon",
                    "match_method", "note"])
        for st, feat, method, note in results:
            p = feat["properties"]
            lon, lat = feat["geometry"]["coordinates"]
            coords[st] = (lat, lon)
            w.writerow([st, osm_key(p), p.get("name", ""), p.get("name:sr-Latn", ""), p.get("railway", ""),
                        lat, lon, method, note])
    print(f"Matched {len(results)} of {len(stations)} stations "
          f"({sum(1 for r in results if r[2] == 'manual')} manual). Written: {OUT}")
    for st, feat, method, note in results:
        if note:
            print(f"  NOTE {st}: {note}")

    # plausibility: straight-line speed between consecutive timed stops
    by_train: dict[str, list[dict]] = {}
    for r in stop_rows:
        if r["stop_type"] == "stop":
            by_train.setdefault(r["train_id"], []).append(r)
    seg_stats: dict[tuple[str, str], list[float]] = {}
    for tid, rows in by_train.items():
        rows.sort(key=lambda r: int(r["seq"]))
        for a, b in zip(rows, rows[1:]):
            t0 = int(a["dep_min"]) if a["dep_min"] else int(a["arr_min"])
            t1 = int(b["arr_min"]) if b["arr_min"] else int(b["dep_min"])
            d_km = haversine_m(*coords[a["station"]], *coords[b["station"]]) / 1000
            if t1 <= t0:
                seg_stats.setdefault((a["station"], b["station"]), []).append(float("inf"))
                continue
            seg_stats.setdefault((a["station"], b["station"]), []).append(d_km / ((t1 - t0) / 60))
    suspicious = []
    for (a, b), speeds in sorted(seg_stats.items()):
        d_km = haversine_m(*coords[a], *coords[b]) / 1000
        if max(speeds) > MAX_SPEED_KMH or (d_km > 1 and min(speeds) < MIN_SPEED_KMH):
            suspicious.append(f"  {a} -> {b}: {d_km:.1f} km, {min(speeds):.0f}-{max(speeds):.0f} km/h")
    print(f"Segments checked: {len(seg_stats)}; suspicious: {len(suspicious)}")
    for line in suspicious:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
