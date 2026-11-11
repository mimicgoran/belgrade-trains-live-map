// Checks of the timetable simulation (web/sim.js) against web/data/.
// Run from the repository root:  node tests/test_sim.js
"use strict";
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const Sim = require("../web/sim.js");

const dataDir = path.join(__dirname, "..", "web", "data");
const data = JSON.parse(fs.readFileSync(path.join(dataDir, "trains.json"), "utf8"));
const lines = JSON.parse(fs.readFileSync(path.join(dataDir, "lines.geojson"), "utf8"));
const routes = {};
for (const f of lines.features) routes[f.properties.route] = Sim.prepareRoute(f.geometry.coordinates, f.properties.length_km);

let checks = 0;

// 1. At every scheduled arrival/departure the train is exactly at that station's km.
for (const t of data.trains) {
  t.stops.forEach((s, i) => {
    for (const time of [s.arr, s.dep]) {
      if (time === null) continue;
      const st = Sim.trainState(t, time);
      assert(st, `train ${t.id} not running at ${Sim.fmtTime(time)} (${s.station})`);
      assert(Math.abs(st.km - s.km) < 1e-9, `train ${t.id} at ${s.station}: km ${st.km} != ${s.km}`);
      checks++;
    }
  });
  // stands at every intermediate stop for at least 20 s (or its timetable dwell) after arriving
  for (let i = 1; i < t.stops.length - 1; i++) {
    const s = t.stops[i];
    const leave = Sim.leaveTime(s, t.stops[i + 1]);
    assert(leave - s.arr >= Math.min(Sim.DWELL_MIN, (t.stops[i + 1].arr - s.arr) / 2) - 1e-9);
    const mid = Sim.trainState(t, (s.arr + leave) / 2);
    assert(mid.standing && Math.abs(mid.km - s.km) < 1e-9, `train ${t.id}: not standing at ${s.station}`);
    checks++;
  }
  // not running one minute before the first departure or after the last arrival
  assert.strictEqual(Sim.trainState(t, t.stops[0].dep - 1), null);
  assert.strictEqual(Sim.trainState(t, t.stops[t.stops.length - 1].arr + 1), null);
  // km moves monotonically during the trip
  let prev = null;
  for (let m = t.stops[0].dep; m <= t.stops[t.stops.length - 1].arr; m += 0.25) {
    const km = Sim.trainState(t, m).km;
    if (prev !== null) {
      const dir = t.stops[t.stops.length - 1].km > t.stops[0].km ? 1 : -1;
      assert((km - prev) * dir >= -1e-9, `train ${t.id}: moves backwards at ${Sim.fmtTime(m)}`);
    }
    prev = km;
  }
}

// 2. Route ends: km 0 and full length map to the first/last coordinate.
for (const f of lines.features) {
  const r = routes[f.properties.route];
  const [lat0, lon0] = Sim.pointAlong(r, 0);
  const [latN, lonN] = Sim.pointAlong(r, f.properties.length_km);
  const c = f.geometry.coordinates;
  assert(Math.abs(lat0 - c[0][1]) < 1e-9 && Math.abs(lon0 - c[0][0]) < 1e-9);
  assert(Math.abs(latN - c[c.length - 1][1]) < 1e-6 && Math.abs(lonN - c[c.length - 1][0]) < 1e-6);
  checks++;
}

// 3. Day types.
assert.strictEqual(Sim.dayType("2026-11-12", data.holidays), "radni");     // Thursday
assert.strictEqual(Sim.dayType("2026-11-14", data.holidays), "subota");
assert.strictEqual(Sim.dayType("2026-11-15", data.holidays), "nedelja");
assert.strictEqual(Sim.dayType("2026-11-11", data.holidays), "nedelja");   // public holiday (Wednesday)
checks += 4;

// 4. A train running past midnight is shown on the next calendar day.
const t32420 = data.trains.find((t) => t.id === "32420");
const after = Sim.runningTrains([t32420], { ymd: "2026-11-13", minutes: 10 }, data.holidays);
assert.strictEqual(after.length, 1);
assert.strictEqual(after[0].serviceDay, "2026-11-12");
checks++;

// 5. Belgrade local time from a UTC instant (CET = UTC+1 in November).
const b = Sim.belgradeTime(new Date("2026-11-12T07:30:00Z"));
assert.strictEqual(b.ymd, "2026-11-12");
assert(Math.abs(b.minutes - 510) < 1e-9);
checks++;

// Summary: trains running at a few moments
for (const [ymd, hhmm] of [["2026-11-12", "07:30"], ["2026-11-12", "17:00"], ["2026-11-14", "10:00"],
                           ["2026-11-15", "10:00"], ["2026-11-11", "10:00"], ["2026-11-13", "00:10"]]) {
  const [h, m] = hhmm.split(":").map(Number);
  const n = Sim.runningTrains(data.trains, { ymd, minutes: h * 60 + m }, data.holidays).length;
  console.log(`${ymd} ${hhmm} (${Sim.dayType(ymd, data.holidays)}): ${n} trains running`);
}
console.log(`OK - ${checks} checks passed`);
