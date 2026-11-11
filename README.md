# Belgrade Trains Live Map

Unofficial interactive map of commuter and regional trains around Belgrade (BG Voz, Pančevo Vojlovica–Zemun, Vršac–Zemun shown to Pančevo, Belgrade–Novi Sad), animated in real time from published timetables.

**Live map:** https://mimicgoran.github.io/belgrade-trains-live-map/

> Train positions are **simulated from the timetable**, not GPS. Delays and cancellations are not shown.

Site title (Serbian): **Kad stiže voz?** ("When does the train arrive?").

**Status:** first public version. The map is published to GitHub Pages by `.github/workflows/pages.yml` on every push to `main` (after the simulation tests pass).

## Repository layout

```
data_raw/timetable/   original timetables, unchanged
data_raw/osm/         OpenStreetMap stations and tracks (downloaded by 02_fetch_osm.py)
data_prep/            Python scripts that prepare the data
  config/station_aliases.csv          source station name -> station name used on the map
  config/station_osm_overrides.csv    manual station -> OSM links
data_interim/         normalized intermediate data (generated)
web/                  static web map (GitHub Pages): index.html, app.js, sim.js, style.css, vendor/leaflet
tests/                checks of the simulation logic (Node.js)
```

## Timetable sources

| Source file | Service | Valid |
| --- | --- | --- |
| `rv_bg_voza_za_2025.pdf` | BG voz: Batajnica–Ovča, Lazarevac–Resnik–Ovča (Beograd Centar/Zemun) | 09.09.2026 – 12.12.2026 |
| `bgcent_ns_su.pdf` | Beograd Centar – Petrovaradin – Subotica | 19.10.2026 – 12.12.2026 |
| `bgdc_pv.pdf` | Pančevo Vojlovica – Zemun | 14.12.2025 – 12.12.2026 |
| `red_voznje_vrsac_zemun_2025_2026.csv` | Vršac – Zemun | from 14.12.2025 |

## Displayed sections

The map focuses on the Belgrade area, so long-distance trains are shown only on part of their route:

- **Vršac – Zemun** trains are shown only between **Pančevo** (Pančevo Varoš, Pančevo Glavna) and Zemun.
- **Beograd – Subotica** trains are shown only between **Beograd Centar and Novi Sad**.
- From the Beograd – Novi Sad – Subotica timetable only trains of that direction are kept; trains to/from Šid, Sremska Mitrovica, Ruma, Šabac and Zvornik are left out.
- From the Pančevo Vojlovica – Zemun timetable only trains 6000–6019 are used. The Vršac trains printed in it come from the Vršac CSV instead, and the Zrenjanin trains are left out.

Trains on a displayed section still carry their real origin and destination (e.g. "to Subotica").

## Running the data preparation

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows; on Linux/macOS: source .venv/bin/activate
pip install -r data_prep/requirements.txt
python data_prep/01_parse_timetable.py   # timetables -> data_interim/stop_times.csv
python data_prep/02_fetch_osm.py         # OSM download (Overpass API) -> data_raw/osm/
python data_prep/02b_match_stations.py   # timetable stations -> OSM stations
python data_prep/03_build_lines.py       # route geometry along OSM tracks, km of every station
python data_prep/04_export.py            # -> web/data/ (lines.geojson, stations.geojson, trains.json)
```

Outputs in `data_interim/`:

- `stop_times.csv` – one row per stop of every included train. `stop_type` is `stop` or `pass` (the train runs through without stopping; kept for building the route geometry). Times are `HH:MM` and minutes from midnight; trains running past midnight continue above 24:00 / 1440.
- `trains_summary.csv` – one row per train in the sources, including excluded trains and the reason.
- `stations_matched.csv` – every timetable station with its OSM object and coordinates.
- `routes.geojson`, `route_stations.csv`, `train_routes.csv` – one track geometry per physical route, km of each station along it, and the route of every train. Between two listed stations the route follows the shortest OSM track; no stations are added to what the timetable lists.

Files loaded by the map (`web/data/`):

- `lines.geojson` – the track of every route; train icons move along it. Hidden on the map by default, shown on request.
- `stations.geojson` – stations placed on the track, with their km on every route.
- `trains.json` – every train with its stops (`km` along its route, `arr`/`dep` in minutes from midnight, above 1440 after midnight), day types, public holidays and timetable validity.

## Running the map locally

The page loads its data with `fetch`, so it has to be served over HTTP (opening `index.html` as a file does not work):

```bash
cd web
python -m http.server 8000
# open http://localhost:8000
```

For testing at a fixed moment add `?d=YYYY-MM-DD&t=HH:MM`, e.g. `http://localhost:8000/?d=2026-11-12&t=07:30`.

Tests of the simulation logic: `node tests/test_sim.js` (from the repository root).

How it works: `sim.js` finds, for the current Belgrade time and day type, every train that is running and its km along its route (a train arrives at every stop at its timetable time and stands there at least 20 seconds, or longer if the timetable says so; between stops it moves at constant speed, so it makes up the 20 seconds on the way and still arrives on time); `app.js` places the marker at that km on the route geometry and refreshes it 5 times per second.

Base map: standard OpenStreetMap tiles (`TILE_URL` in `app.js`). Their [usage policy](https://operations.osmfoundation.org/policies/tiles/) allows normal website use with attribution but not heavy use; if traffic grows, switch to a provider such as CARTO (free API key required).

## Data notes

- Day types: `radni` (weekday), `subota` (Saturday), `nedelja` (Sunday / public holiday).
- BG voz does not run on Saturdays, Sundays and public holidays (note in the source).
- Pančevo Vojlovica – Zemun: trains marked ❶ do not run on Sundays and public holidays.
- Beograd–Novi Sad: trains without a calendar mark run daily; marks ❶ and ❷ are applied as stated in the source legend. Trains marked ❸/❹ (summer 2026 only) are excluded because they do not run in this timetable period.
- Vršac–Zemun: the source CSV has no calendar, so these trains are currently assumed to run daily (to be verified). It also has one time per station, so arrival and departure are equal. Novi Beograd is closed for reconstruction (9 Sep 2026 – 1 Jan 2027), so its stop in the CSV is turned into a pass-through and the train stops at Tošin bunar instead (Novi Beograd time + 2 min towards Zemun, − 2 min towards Vršac).
- Train 6015 (Pančevo Vojlovica line) needs 21 minutes between Pančevo Glavna and Pančevo Varoš; this is how it is printed in the source and is kept as is.
- Some times at Petrovaradin are marked `*` in the source without a legend; the mark is kept in `source_flag`.
- "Novi Sad TPS" is the Technical-passenger station Novi Sad (OSM: *TPS Novi Sad*), not the main Novi Sad station.

## Data and licences

Map data © OpenStreetMap contributors (ODbL). Timetables: BG voz / Srbijavoz published timetables. This project is not affiliated with BG voz or Srbijavoz.
