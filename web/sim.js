/*
 * Timetable simulation: pure functions with no map code.
 * Used by the web page (window.Sim) and by tests in Node (module.exports).
 *
 * Times are minutes from midnight of the service day (Europe/Belgrade);
 * values above 1440 belong to trains that run past midnight.
 */
(function (root) {
  "use strict";

  const TZ = "Europe/Belgrade";
  // Minimum time a train stands at a station where it stops (minutes). The timetable
  // often gives a single time per station; the train then arrives on time, waits this
  // long and makes up the time on the way to the next stop, where it again arrives on time.
  const DWELL_MIN = 20 / 60;
  const WEEKDAYS = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };

  /** Local Belgrade date and time for a JS Date: {ymd: "2026-11-11", minutes: 1305.5}. */
  function belgradeTime(date) {
    const parts = {};
    new Intl.DateTimeFormat("en-GB", {
      timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }).formatToParts(date).forEach((p) => { parts[p.type] = p.value; });
    const minutes = Number(parts.hour) * 60 + Number(parts.minute) + Number(parts.second) / 60
      + (date.getMilliseconds() / 60000);
    return { ymd: `${parts.year}-${parts.month}-${parts.day}`, minutes };
  }

  function addDays(ymd, n) {
    const [y, m, d] = ymd.split("-").map(Number);
    const dt = new Date(Date.UTC(y, m - 1, d + n));
    return dt.toISOString().slice(0, 10);
  }

  function weekday(ymd) {
    const [y, m, d] = ymd.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d)).getUTCDay(); // 0 = Sunday
  }

  /** "radni" | "subota" | "nedelja" (public holidays count as "nedelja"). */
  function dayType(ymd, holidays) {
    if (holidays.includes(ymd)) return "nedelja";
    const wd = weekday(ymd);
    if (wd === 0) return "nedelja";
    if (wd === 6) return "subota";
    return "radni";
  }

  /**
   * When the train actually leaves stop `a` (next stop `b`): the timetable departure, but
   * not earlier than DWELL_MIN after arrival (and never using more than half of the time
   * to the next stop, so it can still arrive on time).
   */
  function leaveTime(a, b) {
    if (a.arr === null) return a.dep; // first stop: timetable departure
    const dwellEnd = a.arr + Math.min(DWELL_MIN, Math.max(0, (b.arr - a.arr) / 2));
    return Math.max(a.dep, dwellEnd);
  }

  /**
   * Where a train is at time t (minutes of its service day), following the timetable:
   * standing at a station from arrival until it leaves, otherwise moving between two
   * stops at constant speed, arriving at the next stop at its timetable time.
   * Returns null when the train is not running.
   */
  function trainState(train, t) {
    const s = train.stops;
    const last = s.length - 1;
    if (t < s[0].dep || t > s[last].arr) return null;
    for (let i = 0; i < last; i++) {
      const a = s[i];
      const b = s[i + 1];
      const leave = leaveTime(a, b);
      if (a.arr !== null && t >= a.arr && t < leave) {
        return { km: a.km, standing: true, prev: a, next: b, stopIndex: i };
      }
      const endInclusive = i + 1 === last;
      if (t >= leave && (t < b.arr || (endInclusive && t <= b.arr))) {
        const span = b.arr - leave;
        const f = span > 0 ? (t - leave) / span : 1;
        return { km: a.km + f * (b.km - a.km), standing: false, prev: a, next: b, stopIndex: i };
      }
    }
    return null;
  }

  /**
   * All trains running at a moment. Trains of the previous service day are included
   * while they are still running after midnight.
   */
  function runningTrains(trains, now, holidays) {
    const today = dayType(now.ymd, holidays);
    const yesterday = dayType(addDays(now.ymd, -1), holidays);
    const out = [];
    for (const train of trains) {
      if (train.day_types.includes(today)) {
        const st = trainState(train, now.minutes);
        if (st) out.push({ train, state: st, serviceDay: now.ymd });
      }
      if (train.day_types.includes(yesterday) && train.stops[train.stops.length - 1].arr > 1440) {
        const st = trainState(train, now.minutes + 1440);
        if (st) out.push({ train, state: st, serviceDay: addDays(now.ymd, -1) });
      }
    }
    return out;
  }

  /** Prepare a route LineString ([lon, lat] pairs) for pointAlong. */
  function prepareRoute(coords, lengthKm) {
    const cum = [0];
    for (let i = 1; i < coords.length; i++) {
      cum.push(cum[i - 1] + haversine(coords[i - 1], coords[i]));
    }
    // km values come from a metric projection; scale so both ends match exactly
    const scale = cum[cum.length - 1] / (lengthKm * 1000);
    return { coords, cum, scale };
  }

  /** [lat, lon] of the point `km` kilometres from the start of a prepared route. */
  function pointAlong(route, km) {
    const target = Math.max(0, Math.min(km * 1000 * route.scale, route.cum[route.cum.length - 1]));
    let lo = 0;
    let hi = route.cum.length - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (route.cum[mid] <= target) lo = mid; else hi = mid;
    }
    const seg = route.cum[hi] - route.cum[lo];
    const f = seg > 0 ? (target - route.cum[lo]) / seg : 0;
    const [lon1, lat1] = route.coords[lo];
    const [lon2, lat2] = route.coords[hi];
    return [lat1 + f * (lat2 - lat1), lon1 + f * (lon2 - lon1)];
  }

  function haversine(a, b) {
    const R = 6371000;
    const toRad = Math.PI / 180;
    const dLat = (b[1] - a[1]) * toRad;
    const dLon = (b[0] - a[0]) * toRad;
    const h = Math.sin(dLat / 2) ** 2
      + Math.cos(a[1] * toRad) * Math.cos(b[1] * toRad) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(h));
  }

  /** "HH:MM" for minutes (wraps past midnight). */
  function fmtTime(minutes) {
    const m = Math.round(minutes) % 1440;
    return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  }

  const api = {
    DWELL_MIN, belgradeTime, addDays, dayType, leaveTime, trainState, runningTrains, prepareRoute,
    pointAlong, fmtTime,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Sim = api;
})(typeof window !== "undefined" ? window : globalThis);
