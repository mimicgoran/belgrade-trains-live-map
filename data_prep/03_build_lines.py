"""
Phase 3: build the track geometry of every route and the kilometre position of each station.

Inputs:
  - data_interim/stop_times.csv        (01_parse_timetable.py)
  - data_interim/stations_matched.csv  (02b_match_stations.py)
  - data_raw/osm/osm_rail.geojson      (02_fetch_osm.py)

Outputs (data_interim/):
  - routes.geojson       one LineString per physical route (WGS84)
  - route_stations.csv   every timetable station on a route with its km from the route start
  - train_routes.csv     which route each train runs on, and in which direction

Method
  1. A route is the longest station sequence of a line; every train of the line must be a
     part of one route (stations it skips are allowed). Only stations that appear in the
     timetable (stops and pass-throughs) are used - nothing is added.
  2. The OSM tracks become a graph in UTM 34N (metres). Side tracks (yard/siding/spur) get a
     higher cost so main lines are preferred.
  3. Every station gets candidate graph nodes on the tracks within STATION_BUFFER_M.
     For each route, the combination of candidates with the shortest total track length is
     chosen (Viterbi over the station sequence), so the path never jumps or doubles back
     at a station. Between two listed stations the path simply follows the shortest track.
  4. km of a station = track length from the start of the route to its chosen node.

Checks printed at the end: detour factor between consecutive stations, speed of every
train between consecutive stops, and OSM stations the route passes that are not listed
for it in the timetable (for information only).

Run from anywhere:  python data_prep/03_build_lines.py
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import OrderedDict, defaultdict
from pathlib import Path

import networkx as nx
import numpy as np
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
INTERIM = ROOT / "data_interim"
STOP_TIMES = INTERIM / "stop_times.csv"
STATIONS = INTERIM / "stations_matched.csv"
OSM_RAIL = ROOT / "data_raw" / "osm" / "osm_rail.geojson"
OSM_STATIONS = ROOT / "data_raw" / "osm" / "osm_stations.geojson"

STATION_BUFFER_M = 200       # a station may be up to this far from its track
MAX_CANDIDATE_TRACKS = 12    # nearest tracks considered per station
DENSIFY_M = 50               # max vertex spacing used for station candidates
SIDE_TRACK_COST = 3.0        # cost multiplier for service=yard/siding/spur
DETOUR_WARN = 1.6            # track length / straight line between consecutive stations
MAX_SPEED_KMH = 160
MIN_SPEED_KMH = 15
PASSED_STATION_M = 100       # OSM stations this close to a route are reported as passed

to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32634", always_xy=True)
to_wgs = Transformer.from_crs("EPSG:32634", "EPSG:4326", always_xy=True)


# --------------------------------------------------------------------------
# Routes from the timetable
# --------------------------------------------------------------------------

def is_subseq(a, b) -> bool:
    it = iter(b)
    return all(x in it for x in a)


def slug(text: str) -> str:
    table = str.maketrans("čćžšđČĆŽŠĐ", "cczsdCCZSD")
    return "-".join(text.translate(table).lower().split())


def build_routes(stop_rows: list[dict]):
    seqs: "OrderedDict[tuple[str, str], list[str]]" = OrderedDict()
    for r in stop_rows:
        seqs.setdefault((r["line"], r["train_id"]), []).append(r["station"])
    by_line: dict[str, list[tuple]] = defaultdict(list)
    for (line, _), s in seqs.items():
        t = tuple(s)
        if t not in by_line[line] and tuple(reversed(t)) not in by_line[line]:
            by_line[line].append(t)
    routes = {}
    for line, pats in by_line.items():
        pats.sort(key=len, reverse=True)
        maximal = []
        for p in pats:
            if not any(is_subseq(p, m) or is_subseq(p, tuple(reversed(m))) for m in maximal):
                maximal.append(p)
        for m in maximal:
            rid = line if len(maximal) == 1 else f"{slug(m[0])}-{slug(m[-1])}"
            routes[rid] = {"line": line, "stations": list(m)}
    train_route = {}
    for (line, tid), s in seqs.items():
        for rid, rt in routes.items():
            if rt["line"] != line:
                continue
            if is_subseq(s, rt["stations"]):
                train_route[tid] = (rid, "forward")
                break
            if is_subseq(s, list(reversed(rt["stations"]))):
                train_route[tid] = (rid, "reverse")
                break
        else:
            raise RuntimeError(f"Train {tid}: its stations do not fit any route of line {line}")
    return routes, train_route


# --------------------------------------------------------------------------
# Track graph
# --------------------------------------------------------------------------

def build_graph():
    fc = json.load(open(OSM_RAIL, encoding="utf-8"))
    G = nx.Graph()
    xy: dict = {}
    node_way: dict = {}
    for f in fc["features"]:
        p = f["properties"]
        coords = f["geometry"]["coordinates"]
        ids = p.get("node_ids") or []
        if len(ids) != len(coords):
            ids = [("c", round(c[0], 7), round(c[1], 7)) for c in coords]
        cost = SIDE_TRACK_COST if p.get("service") in ("yard", "siding", "spur") else 1.0
        way = p["osm_id"]
        pts = [to_utm.transform(c[0], c[1]) for c in coords]
        for nid, pt in zip(ids, pts):
            xy[nid] = pt
            node_way.setdefault(nid, way)
        for i in range(len(ids) - 1):
            a, b = ids[i], ids[i + 1]
            (x1, y1), (x2, y2) = pts[i], pts[i + 1]
            length = math.hypot(x2 - x1, y2 - y1)
            if length == 0:
                continue
            k = max(1, math.ceil(length / DENSIFY_M))
            chain = [a]
            for j in range(1, k):
                vid = ("v", way, i, j)
                xy[vid] = (x1 + (x2 - x1) * j / k, y1 + (y2 - y1) * j / k)
                node_way[vid] = way
                chain.append(vid)
            chain.append(b)
            for u, v in zip(chain, chain[1:]):
                seg = math.hypot(xy[v][0] - xy[u][0], xy[v][1] - xy[u][1])
                w = seg * cost
                if not G.has_edge(u, v) or G[u][v]["w"] > w:
                    G.add_edge(u, v, w=w, len=seg)
    return G, xy, node_way


def station_candidates(st_xy, node_ids, node_xy, node_way):
    d = np.hypot(node_xy[:, 0] - st_xy[0], node_xy[:, 1] - st_xy[1])
    idx = np.where(d <= STATION_BUFFER_M)[0]
    best_per_way = {}
    for i in idx:
        way = node_way[node_ids[i]]
        if way not in best_per_way or d[i] < best_per_way[way][1]:
            best_per_way[way] = (node_ids[i], float(d[i]))
    cands = sorted(best_per_way.values(), key=lambda t: t[1])[:MAX_CANDIDATE_TRACKS]
    return cands


def route_path(G, stations, cands):
    """Viterbi over station candidates: minimal total weighted track length."""
    n = len(stations)
    cost = [{u: 0.0 for u, _ in cands[0]}]
    back = [{}]
    for i in range(1, n):
        cost.append({})
        back.append({})
        targets = {v for v, _ in cands[i]}
        for u, cu in cost[i - 1].items():
            dist = _dijkstra_to_targets(G, u, targets)
            for v, dv in dist.items():
                c = cu + dv
                if v not in cost[i] or c < cost[i][v]:
                    cost[i][v] = c
                    back[i][v] = u
        if not cost[i]:
            raise RuntimeError(f"No track connection between {stations[i - 1]} and {stations[i]}")
    end = min(cost[-1], key=cost[-1].get)
    chosen = [end]
    for i in range(n - 1, 0, -1):
        chosen.append(back[i][chosen[-1]])
    chosen.reverse()
    return chosen


def _dijkstra_to_targets(G, source, targets):
    """Shortest weighted distance from source to each target (stops when all are found)."""
    import heapq
    dist = {source: 0.0}
    found = {}
    counter = 0  # tie-breaker: node ids are of mixed types
    heap = [(0.0, counter, source)]
    remaining = set(targets)
    while heap and remaining:
        d, _, u = heapq.heappop(heap)
        if d > dist.get(u, math.inf):
            continue
        if u in remaining:
            found[u] = d
            remaining.discard(u)
        for v, attr in G[u].items():
            nd = d + attr["w"]
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                counter += 1
                heapq.heappush(heap, (nd, counter, v))
    return found


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    stop_rows = list(csv.DictReader(open(STOP_TIMES, encoding="utf-8", newline="")))
    st_rows = {r["station"]: r for r in csv.DictReader(open(STATIONS, encoding="utf-8", newline=""))}
    routes, train_route = build_routes(stop_rows)
    print(f"Routes: {len(routes)}")

    G, xy, node_way = build_graph()
    print(f"Track graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    node_ids = list(G.nodes)
    node_xy = np.array([xy[n] for n in node_ids])

    st_xy = {s: to_utm.transform(float(r["lon"]), float(r["lat"])) for s, r in st_rows.items()}
    cands = {}
    for s in {s for rt in routes.values() for s in rt["stations"]}:
        c = station_candidates(st_xy[s], node_ids, node_xy, node_way)
        if not c:
            raise RuntimeError(f"Station {s}: no track within {STATION_BUFFER_M} m")
        cands[s] = c

    warnings: list[str] = []
    features, rs_rows = [], []
    route_km: dict[str, dict[str, float]] = {}
    for rid, rt in routes.items():
        stations = rt["stations"]
        chosen = route_path(G, stations, [cands[s] for s in stations])
        path = [chosen[0]]
        station_idx = [0]
        for u, v in zip(chosen, chosen[1:]):
            seg = nx.dijkstra_path(G, u, v, weight="w")
            path.extend(seg[1:])
            station_idx.append(len(path) - 1)
        cum = [0.0]
        for a, b in zip(path, path[1:]):
            cum.append(cum[-1] + G[a][b]["len"])
        kms = {}
        for i, s in enumerate(stations):
            km = cum[station_idx[i]] / 1000
            kms[s] = km
            off = dict(cands[s])[chosen[i]]
            rs_rows.append([rid, rt["line"], i + 1, s, f"{km:.3f}", f"{off:.0f}"])
        route_km[rid] = kms
        # detour check
        for a, b in zip(stations, stations[1:]):
            track = (kms[b] - kms[a]) * 1000
            straight = math.dist(st_xy[a], st_xy[b])
            if track <= 0:
                warnings.append(f"{rid}: km does not increase from {a} to {b}")
            elif straight > 500 and track / straight > DETOUR_WARN:
                warnings.append(f"{rid}: {a} -> {b} track {track / 1000:.1f} km vs straight "
                                f"{straight / 1000:.1f} km (x{track / straight:.2f})")
        # geometry (drop helper vertices that are not station nodes)
        keep = set(chosen)
        coords = []
        for n in path:
            if isinstance(n, tuple) and n[0] == "v" and n not in keep:
                continue
            lon, lat = to_wgs.transform(*xy[n])
            pt = [round(lon, 6), round(lat, 6)]
            if not coords or coords[-1] != pt:
                coords.append(pt)
        features.append({"type": "Feature",
                         "properties": {"route_id": rid, "line": rt["line"],
                                        "length_km": round(cum[-1] / 1000, 3),
                                        "stations": stations},
                         "geometry": {"type": "LineString", "coordinates": coords}})
        rt["path_xy"] = [xy[n] for n in path]
        print(f"  {rid:28s} {cum[-1] / 1000:7.2f} km, {len(stations)} stations")

    with open(INTERIM / "routes.geojson", "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f, ensure_ascii=False,
                  separators=(",", ":"))
    with open(INTERIM / "route_stations.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["route_id", "line", "seq", "station", "km", "station_to_track_m"])
        w.writerows(rs_rows)
    with open(INTERIM / "train_routes.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["train_id", "line", "route_id", "direction"])
        lines = {r["train_id"]: r["line"] for r in stop_rows}
        for tid, (rid, direction) in train_route.items():
            w.writerow([tid, lines[tid], rid, direction])

    # speed check per train
    by_train = defaultdict(list)
    for r in stop_rows:
        if r["stop_type"] == "stop":
            by_train[r["train_id"]].append(r)
    speed_issues = defaultdict(list)
    all_speeds = []
    for tid, rows in by_train.items():
        rid, _ = train_route[tid]
        kms = route_km[rid]
        rows.sort(key=lambda r: int(r["seq"]))
        for a, b in zip(rows, rows[1:]):
            t0 = int(a["dep_min"] or a["arr_min"])
            t1 = int(b["arr_min"] or b["dep_min"])
            dkm = abs(kms[b["station"]] - kms[a["station"]])
            if t1 <= t0:
                speed_issues[(a["station"], b["station"])].append(f"{tid}: 0 min")
                continue
            v = dkm / ((t1 - t0) / 60)
            all_speeds.append(v)
            if v > MAX_SPEED_KMH or (dkm > 1 and v < MIN_SPEED_KMH):
                speed_issues[(a["station"], b["station"])].append(f"{tid}: {dkm:.1f} km in {t1 - t0} min = {v:.0f} km/h")
    print(f"Speeds between stops: min {min(all_speeds):.0f}, median {sorted(all_speeds)[len(all_speeds) // 2]:.0f}, "
          f"max {max(all_speeds):.0f} km/h")
    for (a, b), items in speed_issues.items():
        warnings.append(f"Speed {a} -> {b}: " + "; ".join(items[:4]) + (" ..." if len(items) > 4 else ""))

    # OSM stations passed by a route but not listed for it (information)
    osm_st = json.load(open(OSM_STATIONS, encoding="utf-8"))["features"]
    for rid, rt in routes.items():
        pts = np.array(rt["path_xy"])
        passed = []
        listed_names = {s.lower() for s in rt["stations"]}
        for f in osm_st:
            p = f["properties"]
            name = p.get("name:sr-Latn") or p.get("name")
            sx, sy = to_utm.transform(*f["geometry"]["coordinates"])
            if np.min(np.hypot(pts[:, 0] - sx, pts[:, 1] - sy)) <= PASSED_STATION_M:
                same_obj = any(st_rows[s]["osm_id"] == f"{p['osm_type']}/{p['osm_id']}" for s in rt["stations"])
                # OSM sometimes has two objects for one station (node + relation); skip those
                if not same_obj and (name or "").lower() not in listed_names:
                    passed.append(name)
        if passed:
            print(f"  INFO {rid}: also passes OSM stations not listed in the timetable: {', '.join(sorted(set(passed)))}")

    for msg in warnings:
        print("WARNING:", msg)
    print(f"Written: routes.geojson, route_stations.csv, train_routes.csv in {INTERIM}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
