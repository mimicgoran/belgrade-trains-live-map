/*
 * "Kad stiže voz?" - web map.
 * Loads web/data/*, then moves a marker for every running train along its route
 * according to the timetable (see sim.js). All texts shown to users are in Serbian (Latin).
 */
(function () {
  "use strict";

  // Base map. Standard OSM tiles show the railway tracks clearly; see README before
  // switching providers (usage policies differ).
  const TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
  const TILE_ATTRIBUTION =
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> saradnici';
  // Opening view: central Belgrade (Novi Beograd - Beograd Centar - Karađorđev park - Zvezdara).
  const START_BOUNDS = [[44.768, 20.395], [44.825, 20.515]];
  const UPDATE_MS = 200;

  // Train symbol: front of a train, white on a rounded square in the line colour.
  function trainIcon(color) {
    const svg = `
      <svg viewBox="0 0 24 24" width="30" height="30" aria-hidden="true">
        <rect x="1" y="1" width="22" height="22" rx="6" fill="${color}" stroke="#fff" stroke-width="1.5"/>
        <rect x="7" y="4.5" width="10" height="12" rx="2.5" fill="#fff"/>
        <rect x="8.5" y="6.5" width="7" height="4" rx="1" fill="${color}"/>
        <circle cx="9.6" cy="13.4" r="1" fill="${color}"/>
        <circle cx="14.4" cy="13.4" r="1" fill="${color}"/>
        <path d="M9 16.5 7 19.5M15 16.5 17 19.5" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>
      </svg>`;
    return L.divIcon({ className: "train-icon", html: svg, iconSize: [30, 30], iconAnchor: [15, 15],
      popupAnchor: [0, -14] });
  }


  // ---------------------------------------------------------------- simulation clock
  // Live: real Belgrade time. Otherwise the user picked a day/time (time bar or
  // ?d=2026-11-12&t=08:30 in the URL) and the clock runs from there at `speed`.
  const clock = { live: true, ymd: null, minutes: 0, realAt: 0, speed: 1, paused: false };

  function nowSim() {
    if (clock.live) return Sim.belgradeTime(new Date());
    const elapsed = clock.paused ? 0 : ((performance.now() - clock.realAt) / 60000) * clock.speed;
    let minutes = clock.minutes + elapsed;
    let ymd = clock.ymd;
    while (minutes >= 1440) { minutes -= 1440; ymd = Sim.addDays(ymd, 1); }
    return { ymd, minutes };
  }

  function setSim(ymd, minutes) {
    clock.live = false;
    clock.ymd = ymd;
    clock.minutes = minutes;
    clock.realAt = performance.now();
  }

  function goLive() {
    clock.live = true;
    clock.speed = 1;
    clock.paused = false;
  }

  const params = new URLSearchParams(window.location.search);
  if (params.get("d") || params.get("t")) {
    const real = Sim.belgradeTime(new Date());
    const [hh, mm] = (params.get("t") || Sim.fmtTime(real.minutes)).split(":").map(Number);
    setSim(params.get("d") || real.ymd, hh * 60 + mm);
  }

  const map = L.map("map", { zoomControl: true, preferCanvas: true }).fitBounds(START_BOUNDS);
  L.tileLayer(TILE_URL, { maxZoom: 18, attribution: TILE_ATTRIBUTION }).addTo(map);
  // Always visible: positions are simulated, not GPS.
  map.attributionControl.addAttribution("Pozicije simulirane prema redu vožnje, ne GPS");

  const linesLayer = L.layerGroup();          // hidden by default
  const stationsLayer = L.layerGroup().addTo(map);
  const trainsLayer = L.featureGroup().addTo(map);

  // ---------------------------------------------------------------- info panel
  const info = L.control({ position: "topleft" });
  info.onAdd = function () {
    const div = L.DomUtil.create("div", "panel info");
    div.innerHTML = `
      <h1>Kad stiže voz?</h1>
      <p class="subtitle">Prati kretanje vozova na mapi. Pozicije vozova su simulirane na osnovu
      reda vožnje.</p>
      <p class="warning">Prikaz ne uključuje stvarne GPS lokacije, kašnjenja i otkazivanja.</p>
      <p class="count" id="count">Učitavanje podataka…</p>`;
    L.DomEvent.disableClickPropagation(div);
    return div;
  };
  // The info panel is hidden until the "i" button (under the zoom buttons) is pressed;
  // the button or a click on the map closes it again.
  let panelEl = null;
  const infoButton = L.control({ position: "topleft" });
  infoButton.onAdd = function () {
    const btn = L.DomUtil.create("button", "info-button leaflet-bar");
    btn.type = "button";
    btn.title = "O mapi";
    btn.setAttribute("aria-label", "O mapi");
    btn.textContent = "i";
    L.DomEvent.disableClickPropagation(btn);
    L.DomEvent.on(btn, "click", () => {
      panelEl.classList.toggle("hidden");
      // only one panel open at a time (they overlap on small screens)
      document.querySelectorAll(".layers").forEach((el) => { el.hidden = true; });
    });
    return btn;
  };
  infoButton.addTo(map);
  info.addTo(map);
  panelEl = info.getContainer();
  panelEl.classList.add("hidden");
  map.on("click", () => panelEl.classList.add("hidden"));

  // ---------------------------------------------------------------- data
  Promise.all([
    fetch("data/lines.geojson").then(checkJson),
    fetch("data/stations.geojson").then(checkJson),
    fetch("data/trains.json").then(checkJson),
  ]).then(([lines, stations, data]) => start(lines, stations, data))
    .catch((err) => {
      console.error(err);
      document.getElementById("count").textContent =
        "Podaci nisu učitani. Stranicu treba otvoriti preko veb servera, ne kao lokalni fajl.";
    });

  function checkJson(resp) {
    if (!resp.ok) throw new Error(`${resp.url}: HTTP ${resp.status}`);
    return resp.json();
  }

  function start(lines, stations, data) {
    const routes = {};
    for (const f of lines.features) {
      const p = f.properties;
      routes[p.route] = Sim.prepareRoute(f.geometry.coordinates, p.length_km);
      L.polyline(f.geometry.coordinates.map(([lon, lat]) => [lat, lon]), {
        color: p.color, weight: 4, opacity: 0.75, interactive: false,
      }).addTo(linesLayer);
    }

    for (const f of stations.features) {
      const [lon, lat] = f.geometry.coordinates;
      L.circleMarker([lat, lon], {
        radius: 5, color: "#333", weight: 2, fillColor: "#fff", fillOpacity: 1,
      }).bindTooltip(f.properties.name, { direction: "top", offset: [0, -4] }).addTo(stationsLayer);
    }

    // Layer panel (top right): route lines, stations, trains - trains can be filtered by line.
    const visibleLines = new Set(Object.keys(data.lines));
    const layersControl = L.control({ position: "topright" });
    layersControl.onAdd = function () {
      const wrap = L.DomUtil.create("div", "layers-wrap");
      const btn = L.DomUtil.create("button", "layers-button leaflet-bar", wrap);
      btn.type = "button";
      btn.title = "Prikaz: linije, stanice, vozovi";
      btn.setAttribute("aria-label", btn.title);
      btn.setAttribute("aria-expanded", "false");
      btn.innerHTML = `<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
        <path d="M12 3 2 8l10 5 10-5-10-5Z" fill="none" stroke="#333" stroke-width="2" stroke-linejoin="round"/>
        <path d="m2 12 10 5 10-5M2 16l10 5 10-5" fill="none" stroke="#333" stroke-width="2" stroke-linejoin="round"/></svg>`;
      const div = L.DomUtil.create("div", "panel layers", wrap);
      div.hidden = true;
      btn.addEventListener("click", () => {
        div.hidden = !div.hidden;
        if (!div.hidden && panelEl) panelEl.classList.add("hidden");
        btn.setAttribute("aria-expanded", String(!div.hidden));
      });
      const lineRows = Object.entries(data.lines).map(([id, l]) => `
        <label class="line-row"><input type="checkbox" data-line="${id}" checked>
          <span class="swatch" style="background:${l.color}"></span>${l.name}</label>`).join("");
      div.innerHTML = `
        <label><input type="checkbox" id="chk-lines"> Prikaži linije</label>
        <label><input type="checkbox" id="chk-stations" checked> Stanice</label>
        <div class="trains-row">
          <label><input type="checkbox" id="chk-trains" checked> Vozovi</label>
          <button type="button" class="expander" aria-expanded="false" title="Vozovi po linijama">▸</button>
        </div>
        <div class="line-list" hidden>${lineRows}</div>`;
      L.DomEvent.disableClickPropagation(wrap);
      L.DomEvent.disableScrollPropagation(wrap);
      return wrap;
    };
    layersControl.addTo(map);
    map.on("click", () => {
      const panel = layersControl.getContainer().querySelector(".layers");
      panel.hidden = true;
      layersControl.getContainer().querySelector(".layers-button").setAttribute("aria-expanded", "false");
    });
    const lc = layersControl.getContainer();
    const chkTrains = lc.querySelector("#chk-trains");
    const lineBoxes = [...lc.querySelectorAll("input[data-line]")];

    lc.querySelector("#chk-lines").addEventListener("change", (e) => {
      if (e.target.checked) linesLayer.addTo(map); else map.removeLayer(linesLayer);
    });
    lc.querySelector("#chk-stations").addEventListener("change", (e) => {
      if (e.target.checked) stationsLayer.addTo(map); else map.removeLayer(stationsLayer);
    });
    const expander = lc.querySelector(".expander");
    expander.addEventListener("click", () => {
      const list = lc.querySelector(".line-list");
      list.hidden = !list.hidden;
      expander.textContent = list.hidden ? "▸" : "▾";
      expander.setAttribute("aria-expanded", String(!list.hidden));
    });
    function syncTrainBoxes() {
      const on = lineBoxes.filter((b) => b.checked).length;
      chkTrains.checked = on > 0;
      chkTrains.indeterminate = on > 0 && on < lineBoxes.length;
      visibleLines.clear();
      lineBoxes.filter((b) => b.checked).forEach((b) => visibleLines.add(b.dataset.line));
      tick();
    }
    chkTrains.addEventListener("change", () => {
      lineBoxes.forEach((b) => { b.checked = chkTrains.checked; });
      syncTrainBoxes();
    });
    lineBoxes.forEach((b) => b.addEventListener("change", syncTrainBoxes));

    const markers = new Map(); // key: train id + service day

    function popupHtml(item) {
      const { train, state } = item;
      const title = train.category ? `${train.category} ${train.id}` : `Voz ${train.id}`;
      const first = train.stops[0].station;
      const last = train.stops[train.stops.length - 1].station;
      // trains that continue beyond the map (e.g. to Subotica or Vrsac)
      let shown = "";
      if (train.origin !== first) shown = `Na mapi prikazan od stanice ${first}`;
      if (train.destination !== last) shown = `Na mapi prikazan do stanice ${last}`;
      let where;
      if (state.standing) {
        where = `Na stanici: <b>${state.prev.station}</b> (polazak ${Sim.fmtTime(state.prev.dep)})`;
      } else {
        where = `Prethodna stanica: ${state.prev.station} (polazak ${Sim.fmtTime(state.prev.dep)})<br>
          Sledeća stanica: <b>${state.next.station}</b> (dolazak ${Sim.fmtTime(state.next.arr)})`;
      }
      return `<div class="train-popup"><b>${title}</b><br>
        ${train.origin} → ${train.destination}<br>${where}
        ${shown ? `<div class="shown">${shown}</div>` : ""}</div>`;
    }

    function tick() {
      const now = nowSim();
      const running = Sim.runningTrains(data.trains, now, data.holidays)
        .filter((item) => visibleLines.has(item.train.line));
      const seen = new Set();
      for (const item of running) {
        const key = `${item.train.id}|${item.serviceDay}`;
        seen.add(key);
        const pos = Sim.pointAlong(routes[item.train.route], item.state.km);
        let m = markers.get(key);
        if (!m) {
          const color = (data.lines[item.train.line] || {}).color || "#000";
          m = L.marker(pos, { icon: trainIcon(color), keyboard: false })
            .bindPopup("").addTo(trainsLayer);
          markers.set(key, m);
        } else {
          m.setLatLng(pos);
        }
        m.item = item;
        if (m.isPopupOpen()) m.setPopupContent(popupHtml(item));
      }
      for (const [key, m] of markers) {
        if (!seen.has(key)) {
          trainsLayer.removeLayer(m);
          markers.delete(key);
        }
      }
      updatePanel(now, running.length);
      timeBar.update(now);
    }

    // The popup pans the map only once, when it opens. Afterwards it just follows its
    // train, so the user can keep moving the map while the popup is open.
    trainsLayer.on("popupopen", (e) => {
      const m = e.layer;
      if (m && m.item) m.setPopupContent(popupHtml(m.item));
      e.popup.options.autoPan = false;
    });
    trainsLayer.on("popupclose", (e) => {
      e.popup.options.autoPan = true;
    });

    function updatePanel(now, count) {
      document.getElementById("count").textContent = `Vozova u vožnji: ${count}`;
    }

    const timeBar = createTimeBar(data.holidays);
    tick();
    // faster refresh when the clock runs faster, so trains still move smoothly
    let lastTick = 0;
    (function loop(ts) {
      const every = !clock.live && !clock.paused && clock.speed > 1 ? 40 : UPDATE_MS;
      if (ts - lastTick >= every) { lastTick = ts; tick(); }
      requestAnimationFrame(loop);
    })(0);
  }
  // ---------------------------------------------------------------- time bar
  // Bottom of the map: shows "Sada" or the simulated day/time; opens a panel with
  // day type, a time slider, speed and "Nazad na sada".
  const DAY_NAMES = ["Nedelja", "Ponedeljak", "Utorak", "Sreda", "Četvrtak", "Petak", "Subota"];

  function createTimeBar(holidays) {
    const bar = L.DomUtil.create("div", "timebar", map.getContainer());
    bar.innerHTML = `
      <div class="time-panel panel" hidden>
        <div class="tp-row tp-days" role="group" aria-label="Dan">
          <button type="button" data-day="radni">Radni dan</button>
          <button type="button" data-day="subota">Subota</button>
          <button type="button" data-day="nedelja">Nedelja</button>
        </div>
        <div class="tp-row tp-slider">
          <input type="range" min="0" max="1439" step="1" aria-label="Vreme">
          <div class="tp-ticks"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>
        </div>
        <div class="tp-row tp-controls">
          <button type="button" class="tp-play" aria-label="Pauza">⏸</button>
          <div class="tp-speeds" role="group" aria-label="Brzina">
            <button type="button" data-speed="1">1×</button>
            <button type="button" data-speed="10">10×</button>
            <button type="button" data-speed="60">60×</button>
          </div>
          <button type="button" class="tp-now">Nazad na sada</button>
        </div>
      </div>
      <button type="button" class="time-pill panel" aria-expanded="false" title="Vreme na mapi">
        <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><circle cx="12" cy="12" r="9"
          fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 7v5l3 2" fill="none"
          stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
        <span class="tp-label">Sada</span><span class="tp-caret">▴</span>
      </button>`;
    L.DomEvent.disableClickPropagation(bar);
    L.DomEvent.disableScrollPropagation(bar);

    const panel = bar.querySelector(".time-panel");
    const pill = bar.querySelector(".time-pill");
    const label = bar.querySelector(".tp-label");
    const slider = bar.querySelector("input[type=range]");
    const playBtn = bar.querySelector(".tp-play");
    let dragging = false;

    pill.addEventListener("click", () => {
      panel.hidden = !panel.hidden;
      pill.setAttribute("aria-expanded", String(!panel.hidden));
      bar.querySelector(".tp-caret").textContent = panel.hidden ? "▴" : "▾";
    });

    // day type: jump to the nearest date (from today) of that type, keep the time of day
    bar.querySelectorAll("[data-day]").forEach((b) => b.addEventListener("click", () => {
      const now = nowSim();
      let ymd = Sim.belgradeTime(new Date()).ymd;
      for (let i = 0; i < 14 && Sim.dayType(ymd, holidays) !== b.dataset.day; i++) ymd = Sim.addDays(ymd, 1);
      setSim(ymd, now.minutes);
    }));

    slider.addEventListener("pointerdown", () => { dragging = true; });
    slider.addEventListener("pointerup", () => { dragging = false; });
    slider.addEventListener("input", () => {
      setSim(nowSim().ymd, Number(slider.value));
    });

    bar.querySelectorAll("[data-speed]").forEach((b) => b.addEventListener("click", () => {
      const now = nowSim();
      setSim(now.ymd, now.minutes);
      clock.speed = Number(b.dataset.speed);
      clock.paused = false;
    }));

    playBtn.addEventListener("click", () => {
      const now = nowSim();
      setSim(now.ymd, now.minutes);
      clock.paused = !clock.paused;
    });

    bar.querySelector(".tp-now").addEventListener("click", goLive);

    map.on("click", () => {
      panel.hidden = true;
      pill.setAttribute("aria-expanded", "false");
      bar.querySelector(".tp-caret").textContent = "▴";
    });

    function update(now) {
      const time = Sim.fmtTime(Math.floor(now.minutes));
      if (clock.live) {
        label.textContent = `Sada · ${time}`;
      } else {
        const [y, mo, d] = now.ymd.split("-").map(Number);
        const day = DAY_NAMES[new Date(Date.UTC(y, mo - 1, d)).getUTCDay()];
        const state = clock.paused ? " · pauza" : (clock.speed > 1 ? ` · ${clock.speed}×` : "");
        label.textContent = `${day} ${time}${state}`;
      }
      bar.classList.toggle("is-sim", !clock.live);
      if (!dragging) slider.value = String(Math.floor(now.minutes));
      const dt = Sim.dayType(now.ymd, holidays);
      bar.querySelectorAll("[data-day]").forEach((b) => b.classList.toggle("active", b.dataset.day === dt));
      const speed = clock.live ? 1 : clock.speed;
      bar.querySelectorAll("[data-speed]").forEach((b) =>
        b.classList.toggle("active", Number(b.dataset.speed) === speed && !clock.paused));
      playBtn.textContent = clock.paused ? "▶" : "⏸";
      playBtn.setAttribute("aria-label", clock.paused ? "Pusti" : "Pauza");
      bar.querySelector(".tp-now").disabled = clock.live;
    }

    return { update };
  }
})();
