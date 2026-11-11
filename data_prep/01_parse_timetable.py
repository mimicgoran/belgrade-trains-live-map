"""
Phase 1: parse the raw timetables into one normalized stop-times table.

Inputs (data_raw/timetable/):
  - rv_bg_voza_za_2025.pdf                 BG voz (Batajnica-Ovca, Lazarevac-Resnik-Ovca)
  - bgcent_ns_su.pdf                       Beograd Centar - Novi Sad - Subotica (Srbijavoz)
  - bgdc_pv.pdf                            Zemun - Beograd Centar - Pancevo - Pancevo Vojlovica
  - red_voznje_vrsac_zemun_2025_2026.csv   Vrsac - Zemun

Outputs (data_interim/):
  - stop_times.csv      one row per stop (or pass-through) of every included train
  - trains_summary.csv  one row per train in the sources, incl. excluded ones and why

PDF tables are read cell by cell from word coordinates (pdfplumber): every time
value is assigned to the train-number column it sits under.

Display clipping (decided by the project owner):
  - Vrsac - Zemun trains are kept only on the Pancevo - Zemun section.
  - Beograd - Novi Sad - Subotica trains are kept only on the Beograd - Novi Sad section.
The raw files are never modified; clipping happens only in the output.

Run from anywhere:  python data_prep/01_parse_timetable.py
"""

from __future__ import annotations

import csv
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data_raw" / "timetable"
OUT_DIR = ROOT / "data_interim"
ALIASES_CSV = Path(__file__).resolve().parent / "config" / "station_aliases.csv"

DAY_TYPES_ALL = ["radni", "subota", "nedelja"]

# --------------------------------------------------------------------------
# Source configuration
# --------------------------------------------------------------------------

SOURCES = {
    "bgvoz": {
        "file": "rv_bg_voza_za_2025.pdf",
        "valid_from": "2026-09-09",
        "valid_to": "2026-12-12",
        # The note under every BG voz table: "ne saobraca u dan 6,7 i drzavni praznik"
        # (does not run on days 6 and 7 of the week, i.e. Saturday and Sunday, or on public holidays).
        "required_text": ["не саобраћа у дан 6,7 и државни празник"],
        "default_day_types": ["radni"],
        "default_calendar_note": "Does not run on Saturdays, Sundays and public holidays (source note).",
        # table title (substring, upper case, Cyrillic) -> line id
        "lines": {
            "БАТАЈНИЦА - ОВЧА": "batajnica-ovca",
            "ОВЧА - БАТАЈНИЦА": "batajnica-ovca",
            "ЛАЗАРЕВАЦ - РЕСНИК - ОВЧА": "lazarevac-resnik-ovca",
            "ОВЧА - РЕСНИК - ЛАЗАРЕВАЦ": "lazarevac-resnik-ovca",
        },
        "clip": None,
    },
    "novisad": {
        "file": "bgcent_ns_su.pdf",
        "valid_from": "2026-10-19",
        "valid_to": "2026-12-12",
        "required_text": [
            "ne saobraća subotom, nedeljom i državnim praznikom",
            "ne saobraća subotom i dan pre državnog praznika",
            "saobraća do 12. VI 2026",
            "saobraća od 13.VI do 14. IX 2026",
        ],
        "default_day_types": DAY_TYPES_ALL,
        "default_calendar_note": "",
        # Legend symbols in the "Kalendar saobracaja" row.
        # ❸ and ❹ only run in periods that lie before this timetable's validity
        # (19.10.2026 - 12.12.2026), so those trains are excluded.
        "calendar": {
            "❶": {"day_types": ["radni"],
                  "note": "Does not run on Saturdays, Sundays and public holidays (source note 1)."},
            "❷": {"day_types": ["radni", "nedelja"],
                  "note": "Does not run on Saturdays and on the day before a public holiday (source note 2)."},
            "❸": {"exclude": "Runs only until 11/12 June 2026 (source note 3), before this timetable period."},
            "❹": {"exclude": "Runs only 12/13 June - 13/14 September 2026 (source note 4), outside this timetable period."},
        },
        "lines": {
            "BEOGRAD CENTAR - PETROVARADIN - SUBOTICA": "beograd-novi-sad",
            "SUBOTICA - PETROVARADIN - BEOGRAD CENTAR": "beograd-novi-sad",
        },
        # Displayed section Beograd Centar - Novi Sad TPS (stations of this table on that section).
        # Trains coming from / going to another line (printed as "iz"/"za" in the table)
        # are not shown: only the Beograd - Novi Sad (- Subotica) direction is kept.
        "exclude_off_line": ["Šid", "S.Mitrovica", "Ruma", "Šabac", "Zvornik", "Užice", "Bar"],
        "clip": ["Beograd Centar", "Novi Beograd", "Tošin bunar", "Zemun", "Zemunsko polje",
                 "Batajnica", "Nova Pazova", "Stara Pazova", "Inđija", "Beška",
                 "Sremski Karlovci", "Petrovaradin", "Novi Sad TPS"],
    },
    "pancevo": {
        "file": "bgdc_pv.pdf",
        "valid_from": "2025-12-14",
        "valid_to": "2026-12-12",
        "required_text": ["ne saobraća nedeljom i državnim praznikom", "14. XII 2025 - 12. XII 2026"],
        "default_day_types": DAY_TYPES_ALL,
        "default_calendar_note": "",
        "calendar": {
            "❶": {"day_types": ["radni", "subota"],
                  "note": "Does not run on Sundays and public holidays (source note 1)."},
        },
        "lines": {
            "ZEMUN - BEOGRAD CENTAR - PANČEVO GLAVNA STANICA - PANČEVO VOJLOVICA": "pancevo-vojlovica-zemun",
            "PANČEVO VOJLOVICA - PANČEVO GLAVNA STANICA - BEOGRAD CENTAR - ZEMUN": "pancevo-vojlovica-zemun",
        },
        # Vrsac trains are also printed in this table (Pancevo - Zemun section), but they
        # are taken from the Vrsac - Zemun CSV, so they are skipped here.
        "skip_endpoints": {"Vršac": "Taken from red_voznje_vrsac_zemun_2025_2026.csv instead."},
        # Zrenjanin trains appear here only between Ovca and Pancevo; not shown.
        "exclude_off_line": ["Zrenjanin"],
        "clip": None,
    },
    "vrsac": {
        "file": "red_voznje_vrsac_zemun_2025_2026.csv",
        "valid_from": "2025-12-14",
        "valid_to": "",
        "default_day_types": DAY_TYPES_ALL,
        "default_calendar_note": "Source CSV has no calendar column; assumed to run daily - TO BE VERIFIED.",
        "line": "vrsac-zemun",
        # Novi Beograd is closed for reconstruction (9 Sep 2026 - 1 Jan 2027); trains run through
        # it without stopping. The CSV predates the closure, so (decided by the project owner) the
        # Novi Beograd stop becomes a pass-through and the train stops at Tosin bunar instead,
        # 2 minutes after Novi Beograd towards Zemun / 2 minutes before it towards Vrsac.
        "closed_stop_fix": {"closed": "Novi Beograd", "moved_to": "Tošin bunar",
                            "towards": "Zemun", "minutes": 2},
        # Displayed section Pancevo - Zemun. Both Pancevo stations are kept
        # (Pancevo Varos is the first one coming from Vrsac).
        "clip": ["Pančevo Varoš", "Pančevo Glavna", "Pančevački most", "Vukov spomenik",
                 "Karađorđev park", "Beograd Centar", "Novi Beograd", "Tošin bunar", "Zemun"],
    },
}

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

CYR2LAT = {
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Ђ": "Đ", "Е": "E", "Ж": "Ž",
    "З": "Z", "И": "I", "Ј": "J", "К": "K", "Л": "L", "Љ": "Lj", "М": "M", "Н": "N",
    "Њ": "Nj", "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T", "Ћ": "Ć", "У": "U",
    "Ф": "F", "Х": "H", "Ц": "C", "Ч": "Č", "Џ": "Dž", "Ш": "Š",
}
CYR2LAT.update({k.lower(): v.lower() for k, v in CYR2LAT.items()})


def translit(text: str) -> str:
    return "".join(CYR2LAT.get(ch, ch) for ch in text)


def load_aliases() -> dict[str, str]:
    with open(ALIASES_CSV, encoding="utf-8", newline="") as f:
        return {row["raw_name"].strip(): row["station"].strip() for row in csv.DictReader(f)}


TIME_RE = re.compile(r"^(\d{1,2})[,:.](\d{2})(\*?)$")
HOUR_RE = re.compile(r"^\d{1,2}$")
MIN_RE = re.compile(r"^(\d{2})(\*?)$")
TRAIN_NO_RE = re.compile(r"^\d{3,5}$")
PASS_TOKENS = {"│", "|", "┃"}
CATEGORIES = {"Re", "IR", "IC", "REx", "EC", "R", "BD"}


def fmt_min(m: int | None) -> str:
    if m is None:
        return ""
    return f"{m // 60:02d}:{m % 60:02d}"


@dataclass
class Cell:
    kind: str            # "time" or "pass"
    minutes: int | None = None
    flag: str = ""       # e.g. "*" printed next to a time in the source


@dataclass
class Train:
    source: str
    line: str
    train_id: str
    category: str = ""
    day_types: list[str] = field(default_factory=list)
    calendar_note: str = ""
    exclude_reason: str = ""
    origin: str = ""
    destination: str = ""
    # list of (station, station_raw, Cell) in table order, one per table row
    cells: list[tuple[str, str, Cell]] = field(default_factory=list)
    stops: list[dict] = field(default_factory=list)
    n_stops_full: int = 0


# --------------------------------------------------------------------------
# PDF grid reader
# --------------------------------------------------------------------------

def page_rows(page, ytol: float = 3.0) -> list[dict]:
    page = page.dedupe_chars(tolerance=1)
    words = page.extract_words(x_tolerance=1.5, y_tolerance=2)
    # drop duplicated words (some cells are printed twice on top of each other)
    seen, uniq = set(), []
    for w in words:
        key = (w["text"], round(w["x0"] / 2), round(w["top"] / 2))
        if key not in seen:
            seen.add(key)
            uniq.append(w)
    uniq.sort(key=lambda w: (w["top"], w["x0"]))
    rows: list[dict] = []
    for w in uniq:
        w["xc"] = (w["x0"] + w["x1"]) / 2
        if rows and abs(rows[-1]["top"] - w["top"]) <= ytol:
            rows[-1]["words"].append(w)
        else:
            rows.append({"top": w["top"], "words": [w]})
    for r in rows:
        r["words"].sort(key=lambda w: w["x0"])
        r["text"] = " ".join(w["text"] for w in r["words"])
    return rows


def cell_tokens(words: list[dict]) -> list[tuple[float, str, str]]:
    """Turn the words of the column zone into (x_center, kind, value) tokens.
    Handles both '6,00' (one word) and '6 00' (hour and minute as two words)."""
    out, i = [], 0
    while i < len(words):
        w = words[i]
        t = w["text"]
        if TIME_RE.match(t):
            out.append((w["xc"], "time", t))
            i += 1
            continue
        if HOUR_RE.match(t) and i + 1 < len(words) and MIN_RE.match(words[i + 1]["text"]) \
                and words[i + 1]["x0"] - w["x1"] < 6:
            nxt = words[i + 1]
            out.append(((w["x0"] + nxt["x1"]) / 2, "time", f"{t}:{nxt['text']}"))
            i += 2
            continue
        if t in PASS_TOKENS:
            out.append((w["xc"], "pass", t))
        else:
            out.append((w["xc"], "text", t))
        i += 1
    return out


def nearest_col(x: float, cols: list[tuple[float, str]], tol: float) -> str | None:
    best = min(cols, key=lambda c: abs(c[0] - x))
    return best[1] if abs(best[0] - x) <= tol else None


def parse_time(value: str) -> tuple[int, str]:
    m = TIME_RE.match(value)
    if not m:
        raise ValueError(value)
    return int(m.group(1)) * 60 + int(m.group(2)), m.group(3)


def read_pdf_tables(path: Path, cfg: dict, source: str, aliases: dict, warnings: list) -> list[Train]:
    trains: dict[str, Train] = {}
    order: list[str] = []
    with pdfplumber.open(path) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        for req in cfg.get("required_text", []):
            if req not in full_text:
                raise RuntimeError(f"{path.name}: expected legend text not found: {req!r}. "
                                   "The source changed - review the configuration.")
        for page_no, page in enumerate(pdf.pages, start=1):
            rows = page_rows(page)
            title = None
            cols: list[tuple[float, str]] = []
            col_trains: dict[str, Train] = {}
            label_limit = 0.0
            last_station = None
            pending_category_row = None  # "Vrsta voza" row is printed above the train numbers
            for r in rows:
                if translit(r["text"]).lower().startswith("vrsta voza"):
                    pending_category_row = r
                    continue
                nums = [w for w in r["words"] if TRAIN_NO_RE.match(w["text"])]
                has_time = any(TIME_RE.match(w["text"]) for w in r["words"])
                upper = r["text"].upper()
                # table title
                for key, line_id in cfg["lines"].items():
                    if key in upper and (" - " in r["text"]):
                        title = (key, line_id)
                # header row with train numbers
                if len(nums) >= 3 and not has_time:
                    if title is None:
                        raise RuntimeError(f"{path.name} p{page_no}: train header without a known table title")
                    # numbers that overlap (same column) are the same cell
                    cols = []
                    for w in nums:
                        if cols and abs(cols[-1][0] - w["xc"]) < 8:
                            continue
                        cols.append((w["xc"], w["text"]))
                    spacing = min(b[0] - a[0] for a, b in zip(cols, cols[1:]))
                    label_limit = cols[0][0] - spacing / 2
                    col_trains = {}
                    for _, num in cols:
                        if num in trains:
                            # same train printed in two tables (e.g. 8340): parse it again
                            # separately and keep the version with more stops later
                            key = f"{num}#{page_no}#{round(r['top'])}"
                        else:
                            key = num
                        t = Train(source=source, line=title[1], train_id=num,
                                  day_types=list(cfg["default_day_types"]),
                                  calendar_note=cfg["default_calendar_note"])
                        trains[key] = t
                        order.append(key)
                        col_trains[num] = t
                    if pending_category_row is not None:
                        for w in pending_category_row["words"]:
                            if w["text"] in CATEGORIES:
                                num = nearest_col(w["xc"], cols, spacing * 0.42)
                                if num and not col_trains[num].category:
                                    col_trains[num].category = w["text"]
                        pending_category_row = None
                    last_station = None
                    continue
                if not cols:
                    continue
                label_words = [w for w in r["words"] if w["x1"] <= label_limit]
                zone_words = [w for w in r["words"] if w["x1"] > label_limit]
                label = " ".join(w["text"] for w in label_words).strip()
                tol = spacing * 0.42
                tokens = cell_tokens(zone_words)
                low = translit(label).lower()
                if low.startswith("kalendar"):
                    for x, kind, val in tokens:
                        num = nearest_col(x, cols, tol)
                        if num is None:
                            warnings.append(f"{path.name} p{page_no}: calendar mark {val} not under a column")
                            continue
                        rule = cfg["calendar"].get(val)
                        if rule is None:
                            raise RuntimeError(f"Unknown calendar mark {val!r} in {path.name}")
                        t = col_trains[num]
                        if "exclude" in rule:
                            t.exclude_reason = rule["exclude"]
                        else:
                            t.day_types = list(rule["day_types"])
                            t.calendar_note = rule["note"]
                    continue
                label_tokens = low.replace("↓", " ").split()
                if "iz" in label_tokens or "za" in label_tokens:
                    attr = "origin" if "iz" in label_tokens else "destination"
                    for x, kind, val in tokens:
                        if kind != "text":
                            continue
                        num = nearest_col(x, cols, tol)
                        if num:
                            setattr(col_trains[num], attr, translit(val))
                    continue
                if not label or low.startswith(("broj", "kolski", "↓")):
                    continue
                # a station row must contain only times / pass marks in the column zone
                if not tokens or any(kind == "text" for _, kind, _ in tokens):
                    continue
                raw = translit(label)
                if raw not in aliases:
                    raise RuntimeError(f"{path.name} p{page_no}: station {raw!r} missing in {ALIASES_CSV.name}")
                station = aliases[raw]
                for x, kind, val in tokens:
                    num = nearest_col(x, cols, tol)
                    if num is None:
                        warnings.append(f"{path.name} p{page_no} {raw}: value {val} at x={x:.0f} not under a column")
                        continue
                    t = col_trains[num]
                    if kind == "time":
                        minutes, flag = parse_time(val)
                        t.cells.append((station, raw, Cell("time", minutes, flag)))
                    else:
                        t.cells.append((station, raw, Cell("pass")))
                last_station = station
    # merge duplicated trains: keep the parse with more timed cells
    result: dict[str, Train] = {}
    for key in order:
        t = trains[key]
        n = sum(1 for c in t.cells if c[2].kind == "time")
        prev = result.get(t.train_id)
        if prev is None:
            result[t.train_id] = t
        else:
            n_prev = sum(1 for c in prev.cells if c[2].kind == "time")
            warnings.append(f"{path.name}: train {t.train_id} appears in two tables; "
                            f"kept the one with {max(n, n_prev)} timed stops")
            if n > n_prev:
                if not t.destination:
                    t.destination = prev.destination
                result[t.train_id] = t
            elif not prev.destination and t.destination:
                prev.destination = t.destination
    return list(result.values())


# --------------------------------------------------------------------------
# CSV reader (Vrsac - Zemun)
# --------------------------------------------------------------------------

def read_vrsac_csv(path: Path, cfg: dict, aliases: dict) -> list[Train]:
    by_train: dict[str, list[tuple[int, str]]] = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            m, _ = parse_time(row["Vreme"].strip())
            by_train.setdefault(row["Broj voza"].strip(), []).append((m, row["Stanica"].strip()))
    trains = []
    for num, rows in by_train.items():
        # rows of the Zemun -> Vrsac direction are listed from Vrsac backwards; order by time
        rows.sort(key=lambda r: r[0])
        t = Train(source="vrsac", line=cfg["line"], train_id=num,
                  day_types=list(cfg["default_day_types"]), calendar_note=cfg["default_calendar_note"])
        for minutes, raw in rows:
            if raw not in aliases:
                raise RuntimeError(f"{path.name}: station {raw!r} missing in {ALIASES_CSV.name}")
            t.cells.append((aliases[raw], raw, Cell("time", minutes)))
        if cfg.get("closed_stop_fix"):
            apply_closed_stop_fix(t, cfg["closed_stop_fix"])
        trains.append(t)
    return trains


def apply_closed_stop_fix(t: Train, fix: dict) -> None:
    """Turn the stop at a closed station into a pass-through and add the replacement stop."""
    names = [c[0] for c in t.cells]
    closed, moved_to, towards = fix["closed"], fix["moved_to"], fix["towards"]
    if closed not in names or moved_to in names or towards not in names:
        return
    i = names.index(closed)
    _, raw, cell = t.cells[i]
    going_towards = names.index(towards) > i
    minutes = cell.minutes + (fix["minutes"] if going_towards else -fix["minutes"])
    new_stop = (moved_to, f"{moved_to} (added: {closed} {fmt_min(cell.minutes)} "
                          f"{'+' if going_towards else '-'}{fix['minutes']} min)", Cell("time", minutes))
    passed = (closed, raw, Cell("pass"))
    if going_towards:
        t.cells[i:i + 1] = [passed, new_stop]
    else:
        t.cells[i:i + 1] = [new_stop, passed]


# --------------------------------------------------------------------------
# Cells -> stops, midnight handling, clipping
# --------------------------------------------------------------------------

def build_stops(t: Train) -> list[dict]:
    """Merge consecutive rows of the same station (arrival row + departure row)."""
    stops: list[dict] = []
    for station, raw, cell in t.cells:
        if cell.kind == "pass":
            stops.append({"station": station, "station_raw": raw, "stop_type": "pass",
                          "arr": None, "dep": None, "flag": ""})
            continue
        if stops and stops[-1]["station"] == station and stops[-1]["stop_type"] == "stop":
            stops[-1]["dep"] = cell.minutes
            stops[-1]["flag"] += cell.flag
            continue
        stops.append({"station": station, "station_raw": raw, "stop_type": "stop",
                      "arr": cell.minutes, "dep": cell.minutes, "flag": cell.flag})
    # times after midnight continue past 1440 so they keep increasing
    offset, last = 0, None
    for s in stops:
        for k in ("arr", "dep"):
            if s[k] is None:
                continue
            v = s[k] + offset
            if last is not None and v < last - 6 * 60:
                offset += 1440
                v += 1440
            s[k] = v
            last = v
    return stops


def finalize_ends(stops: list[dict]) -> list[dict]:
    """Drop pass-through rows outside the first/last real stop and
    clear the arrival of the first stop and the departure of the last one."""
    idx = [i for i, s in enumerate(stops) if s["stop_type"] == "stop"]
    if not idx:
        return []
    stops = stops[idx[0]: idx[-1] + 1]
    stops[0]["arr"] = None
    stops[-1]["dep"] = None
    return stops


def clip(stops: list[dict], keep: list[str], train_id: str) -> list[dict]:
    """Keep only the stops on the displayed section (stations listed in `keep`).
    The kept part must be one contiguous block of the trip."""
    keep_set = set(keep)
    idx = [i for i, s in enumerate(stops) if s["station"] in keep_set]
    if not idx:
        return []
    if idx != list(range(idx[0], idx[-1] + 1)):
        raise RuntimeError(f"Train {train_id}: displayed section is not contiguous - check the clip list")
    return stops[idx[0]: idx[-1] + 1]


def main() -> int:
    aliases = load_aliases()
    warnings: list[str] = []
    trains: list[Train] = []
    for source, cfg in SOURCES.items():
        if not cfg.get("enabled", True):
            continue
        path = RAW_DIR / cfg["file"]
        if source == "vrsac":
            ts = read_vrsac_csv(path, cfg, aliases)
        else:
            ts = read_pdf_tables(path, cfg, source, aliases, warnings)
        for t in ts:
            full = finalize_ends(build_stops(t))
            t.n_stops_full = sum(1 for s in full if s["stop_type"] == "stop")
            if full:
                if not t.origin:
                    t.origin = full[0]["station"]
                if not t.destination:
                    t.destination = full[-1]["station"]
            for place, reason in cfg.get("skip_endpoints", {}).items():
                if not t.exclude_reason and place in (t.origin, t.destination):
                    t.exclude_reason = reason
            off_line = set(cfg.get("exclude_off_line", []))
            if not t.exclude_reason and (t.origin in off_line or t.destination in off_line):
                t.exclude_reason = (f"Other direction, not shown (from {t.origin} to {t.destination}).")
            stops = full
            if cfg["clip"] and full:
                stops = finalize_ends(clip([dict(s) for s in full], cfg["clip"], t.train_id))
            if not t.exclude_reason and sum(1 for s in stops if s["stop_type"] == "stop") < 2:
                t.exclude_reason = "Fewer than two stops on the displayed section."
            t.stops = stops
        trains.extend(ts)

    # validation
    errors: list[str] = []
    ids = {}
    for t in trains:
        if t.exclude_reason:
            continue
        key = t.train_id
        if key in ids:
            errors.append(f"Duplicate train id {key} in {ids[key]} and {t.source}")
        ids[key] = t.source
        last = None
        for s in t.stops:
            for k in ("arr", "dep"):
                v = s[k]
                if v is None:
                    continue
                if last is not None and v < last:
                    errors.append(f"Train {t.train_id}: time goes back at {s['station']} ({fmt_min(v)})")
                last = v

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cfg_by_source = SOURCES
    with open(OUT_DIR / "stop_times.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["source", "line", "train_id", "category", "day_types", "origin", "destination",
                    "seq", "station", "station_raw", "stop_type", "arr", "dep", "arr_min", "dep_min",
                    "source_flag", "valid_from", "valid_to"])
        for t in trains:
            if t.exclude_reason:
                continue
            c = cfg_by_source[t.source]
            for seq, s in enumerate(t.stops, start=1):
                w.writerow([t.source, t.line, t.train_id, t.category, ";".join(t.day_types),
                            t.origin, t.destination, seq, s["station"], s["station_raw"], s["stop_type"],
                            fmt_min(s["arr"]), fmt_min(s["dep"]),
                            "" if s["arr"] is None else s["arr"], "" if s["dep"] is None else s["dep"],
                            s["flag"], c["valid_from"], c["valid_to"]])
    with open(OUT_DIR / "trains_summary.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["source", "line", "train_id", "category", "included", "exclude_reason", "day_types",
                    "calendar_note", "origin", "destination", "first_station", "first_dep",
                    "last_station", "last_arr", "n_stops_shown", "n_stops_full"])
        for t in trains:
            st = [s for s in t.stops if s["stop_type"] == "stop"]
            w.writerow([t.source, t.line, t.train_id, t.category, "no" if t.exclude_reason else "yes",
                        t.exclude_reason, ";".join(t.day_types), t.calendar_note, t.origin, t.destination,
                        st[0]["station"] if st else "", fmt_min(st[0]["dep"]) if st else "",
                        st[-1]["station"] if st else "", fmt_min(st[-1]["arr"]) if st else "",
                        len(st), t.n_stops_full])

    # console report
    print("Trains per source / line / included:")
    counts: dict[tuple, int] = {}
    for t in trains:
        k = (t.source, t.line, "excluded" if t.exclude_reason else "included")
        counts[k] = counts.get(k, 0) + 1
    for k in sorted(counts):
        print(f"  {k[0]:8s} {k[1]:24s} {k[2]:9s} {counts[k]:4d}")
    for msg in warnings:
        print("WARNING:", msg)
    for msg in errors:
        print("ERROR:", msg)
    print(f"Written: {OUT_DIR / 'stop_times.csv'}")
    print(f"Written: {OUT_DIR / 'trains_summary.csv'}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
