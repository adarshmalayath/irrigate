/* ═══════════════════════════════════════════════
   Irrigate — app.js
═══════════════════════════════════════════════ */
"use strict";

// ─── State ───────────────────────────────────────
const state = {
  user:       null,
  gardens:    [],
  tankSettings: {},
  activeGardenId: null,
  plantType:  null,
  soilType:   null,
  weather:    null,
  sensors:    null,
  controller: null,
  decision:   null,
  notifications: [],
  connected:  false,
  gpsActive:  false,
  gpsLat:     null,
  gpsLon:     null,
  gpsAcc:     null,
  homeLat:    null,
  homeLon:    null,
  homeLabel:  "Garden location not set",
  modeLocked: false,
};

// ─── DOM helpers ─────────────────────────────────
const $  = id  => document.getElementById(id);
const $$ = sel => document.querySelectorAll(sel);
let socket = null;
let authMode = "login";

// ─── Socket ──────────────────────────────────────
const API_BASE = window.IRRIGATE_API_BASE || window.location.origin;
// Render currently starts this service with the gevent worker, which supports
// Socket.IO polling but not WebSocket upgrades. Stay on polling to avoid failed
// upgrade requests and keep live state updates connected.
function connectDashboardSocket() {
  if (!socket) {
    socket = io(API_BASE, { transports: ["polling"], autoConnect: false, withCredentials: true });
    socket.on("connect", () => {
      state.connected = true;
      updateConnDot(true);
      if (state.gpsLat != null && state.gpsLon != null) {
        socket.emit("location_update", { lat: state.gpsLat, lon: state.gpsLon });
      }
      const garden = state.gardens.find(item => item.id === state.activeGardenId);
      if (garden) socket.emit("set_home", { lat: garden.latitude, lon: garden.longitude, label: garden.location_label });
      showToast("🌿 Connected to Irrigate", "success");
    });
    socket.on("disconnect", () => {
      state.connected = false;
      updateConnDot(false);
      showToast("⚠️ Server disconnected", "warning");
    });
    socket.on("state_update", data => {
      if (data.weather)    state.weather    = data.weather;
      if (data.sensors)    state.sensors    = data.sensors;
      if (data.controller) {
        state.controller = data.controller;
        if (data.controller.home_lat != null && data.controller.home_lon != null) {
          state.homeLat = data.controller.home_lat;
          state.homeLon = data.controller.home_lon;
          state.homeLabel = data.controller.home_label || state.weather?.city || `Garden ${Number(state.homeLat).toFixed(4)}, ${Number(state.homeLon).toFixed(4)}`;
        }
        state.modeLocked = data.controller.mode_locked || false;
      }
      if (data.decision)   state.decision   = data.decision;
      if (data.notifications?.length) handleNotifications(data.notifications);
      renderAll();
    });
    socket.on("notification", n => handleNotifications([n]));
  }
  if (!socket.connected) socket.connect();
}

// ─── Render pipeline ─────────────────────────────

function renderAll() {
  if (state.weather)                        renderWeatherCards();
  if (state.sensors && state.controller)    renderZoneCards();
  if (state.sensors && state.controller)    renderSensorSection();
  if (state.sensors && state.controller)    renderZoneDetailSection();
  if (state.weather)                        renderWeatherDetail();
  if (state.controller)                     renderMode();
  renderGeoCard();
  renderSettingsSection();
}

/* ── Weather summary cards ── */
function renderWeatherCards() {
  const w = state.weather;
  const weatherAvailable = w.available !== false;
  const humidityAvailable = weatherAvailable
    && typeof w.humidity === "number" && Number.isFinite(w.humidity);
  const uvAvailable = w.uvi_available === true
    && typeof w.uvi === "number" && Number.isFinite(w.uvi);
  const precipitationAvailable = weatherAvailable && w.precipitation_available === true
    && typeof w.precipitation_mm_24h === "number" && Number.isFinite(w.precipitation_mm_24h);
  $("temp-val").textContent     = weatherAvailable ? `${w.temperature}°C` : "--°C";
  $("weather-desc").textContent = w.weather_desc;
  $("humidity-val").textContent = humidityAvailable ? `${w.humidity}%` : "--%";
  $("humidity-sub").textContent = humidityAvailable ? "Relative humidity" : "Unavailable";
  $("wind-val").textContent     = weatherAvailable ? `${w.wind_speed_kmh} km/h` : "-- km/h";
  $("wind-sub").textContent     = weatherAvailable ? (w.wind_speed > 7 ? "⚠️ High wind" : "Normal") : "Unavailable";
  $("uv-val").textContent        = uvAvailable ? Number(w.uvi).toFixed(1) : "--";
  $("uv-sub").textContent        = uvAvailable ? uvLabel(Number(w.uvi)) : "Unavailable";
  $("precip-val").textContent    = precipitationAvailable ? `${Number(w.precipitation_mm_24h).toFixed(1)} mm` : "-- mm";
  $("precip-sub").textContent    = precipitationAvailable ? "Next 24h forecast" : "Unavailable";
  $("card-wind").className      = "card " + (weatherAvailable && w.wind_speed > 7 ? "warn" : "");

  const tank = state.sensors?.tank;
  if (tank) {
    $("tank-pct").textContent   = `${tank.level}%`;
    $("tank-litres").textContent= `${tank.litres} L / ${tank.capacity} L`;
    const captured = Number(tank.rainwater_collected_litres || 0).toFixed(1);
    $("tank-source").textContent = tank.is_raining === true
      ? `Collecting rainwater · ${captured} L caught`
      : tank.is_raining === false
        ? `${tank.awaiting_rain ? "Waiting for rain" : `Last fill: ${tank.last_fill_source || "Not recorded"}`} · ${captured} L rain caught`
        : "Rain status unavailable";
    $("tank-cost").textContent = tank.water_tariff_gbp_per_m3 == null
      ? "Mains tariff not set"
      : `${tank.water_supplier ? `${tank.water_supplier} · ` : ""}£${Number(tank.water_tariff_gbp_per_m3).toFixed(4)}/m³ · estimated mains £${Number(tank.mains_cost_gbp_total || 0).toFixed(2)}`;
    $("card-tank").className    = "card " + (tank.level < 10 ? "danger" : tank.level < 25 ? "warn" : "");
  }
}

function uvLabel(value) {
  if (value < 3) return "Low";
  if (value < 6) return "Moderate";
  if (value < 8) return "High";
  if (value < 11) return "Very high";
  return "Extreme";
}

/* Zone readings in the order the user set for the active garden
   (HTTP responses sort object keys alphabetically). */
function orderedZoneEntries(zones) {
  const order = state.controller?.zones || [];
  const rank = name => { const i = order.indexOf(name); return i === -1 ? order.length : i; };
  return Object.entries(zones || {}).sort(([a], [b]) => rank(a) - rank(b));
}

/* ── Zone grid (dashboard) ── */
function renderZoneCards() {
  const grid       = $("zones-grid");
  const zones      = state.sensors.zones;
  const zoneStates = state.controller.zone_states || {};
  grid.innerHTML   = "";

  for (const [name, data] of orderedZoneEntries(zones)) {
    const label = displayZoneLabel(name);
    const isOn  = zoneStates[name] || false;
    const m     = data.moisture;
    const dryAt = state.controller.zone_levels?.[name]?.dry ?? 40;
    const mCls  = m < 25 ? "critical" : m < dryAt ? "dry" : "";
    const arc   = buildArc(m);

    const card = document.createElement("div");
    card.className = ["zone-card", isOn ? "irrigating" : "", mCls].filter(Boolean).join(" ");
    card.dataset.zone = name;
    card.innerHTML = `
      <div class="zone-header">
        <div class="zone-title">
          <div class="zone-name">${escapeHtml(label)}</div>
          ${zoneTypeText(name) ? `<div class="zone-types">${escapeHtml(zoneTypeText(name))}</div>` : ""}
          ${state.controller.zone_levels?.[name] ? `<div class="zone-types">Auto waters below ${Math.round(state.controller.zone_levels[name].dry)}%, stops at ${Math.round(state.controller.zone_levels[name].stop)}%</div>` : ""}
        </div>
        <div class="zone-badge ${isOn ? "on" : m < 25 ? "critical" : "off"}">
          ${isOn ? "ON" : m < 25 ? "DRY" : "OFF"}
        </div>
      </div>
      <div class="moisture-arc">
        <svg viewBox="0 0 80 80">
          <circle class="arc-bg"   cx="40" cy="40" r="30"/>
          <circle class="arc-fill ${mCls}" cx="40" cy="40" r="30"
            stroke-dasharray="${arc.dash}" stroke-dashoffset="${arc.offset}"/>
        </svg>
        <div class="moisture-label">
          <span class="moisture-pct">${m}%</span>
          <span class="moisture-icon">${isOn ? "💧" : "🌱"}</span>
        </div>
      </div>
      <div class="zone-footer">
        <div class="zone-status-text">${data.status}</div>
        <div class="drip-container">
          <div class="drip"></div><div class="drip"></div><div class="drip"></div>
        </div>
      </div>`;
    card.addEventListener("click", () => {
      if (state.controller?.mode === "auto") {
        showToast("🤖 Switch to Manual mode to control zones manually", "info");
        return;
      }
      toggleZone(name, !isOn);
    });
    grid.appendChild(card);
  }
}

/* ── Futuristic garden panel: one pod per zone, animated only while watering ── */
function heroPodMarkup(name, index) {
  return `
    <g class="pod" data-pod="${index}">
      <ellipse class="pod-ring" cx="0" cy="0" rx="70" ry="15" fill="none" stroke="#34f5ff" stroke-opacity=".35"/>
      <ellipse cx="0" cy="0" rx="62" ry="12" fill="url(#podGlow)" stroke="#34f5ff" stroke-opacity=".7"/>
      <polygon class="pod-beam" points="-6,-193 6,-193 32,-58 -32,-58" fill="url(#beamGrad)"/>
      <g class="pod-plant" filter="url(#neonGlow)">
        <path d="M0,-54 C2,-80 -2,-100 0,-126" stroke="#6dff9e" stroke-width="3" fill="none" stroke-linecap="round"/>
        <path d="M0,-80 C-25,-85 -40,-100 -48,-118 C-28,-114 -10,-100 0,-82Z" fill="url(#leafGrad)" stroke="#c4ffd8" stroke-width=".8"/>
        <path d="M0,-95 C25,-100 40,-115 48,-134 C28,-130 10,-115 0,-97Z" fill="url(#leafGrad)" stroke="#c4ffd8" stroke-width=".8"/>
        <path d="M0,-110 C-18,-118 -26,-132 -30,-148 C-14,-142 -4,-128 0,-112Z" fill="url(#leafGrad)" stroke="#c4ffd8" stroke-width=".8"/>
        <path d="M0,-125 C-8,-140 -6,-156 2,-168 C10,-154 10,-138 0,-125Z" fill="url(#leafGrad)" stroke="#c4ffd8" stroke-width=".8"/>
      </g>
      <path d="M-34,-50 L34,-50 L26,-4 L-26,-4 Z" fill="#0c1a23" stroke="#34f5ff" stroke-opacity=".8"/>
      <rect x="-38" y="-57" width="76" height="8" rx="3" fill="#10303a" stroke="#34f5ff"/>
      <ellipse cx="0" cy="-53" rx="32" ry="3.5" fill="#3a2a1d"/>
      <ellipse class="pod-ripple" cx="0" cy="-53" rx="9" ry="2" fill="none" stroke="#7fe9ff" stroke-width="1.2"/>
      <rect x="-20" y="-30" width="40" height="5" rx="2.5" fill="#0a2630"/>
      <rect class="pod-moist" x="-20" y="-30" width="0" height="5" rx="2.5" fill="#34f5ff"/>
      <g class="pod-drops" fill="#9cf0ff">
        <ellipse cx="-12" cy="-186" rx="1.6" ry="4.5"/><ellipse cx="-4" cy="-186" rx="1.6" ry="4.5"/>
        <ellipse cx="5" cy="-186" rx="1.6" ry="4.5"/><ellipse cx="13" cy="-186" rx="1.6" ry="4.5"/>
        <ellipse cx="-8" cy="-186" rx="1.4" ry="4"/><ellipse cx="9" cy="-186" rx="1.4" ry="4"/>
      </g>
      <rect x="-26" y="-212" width="52" height="13" rx="6.5" fill="#0d222c" stroke="#34f5ff" stroke-opacity=".9"/>
      <rect x="-5" y="-200" width="10" height="7" rx="2" fill="#123543" stroke="#34f5ff" stroke-opacity=".7"/>
      <circle class="drone-light" cx="-18" cy="-205.5" r="2.4"/><circle class="drone-light" cx="18" cy="-205.5" r="2.4"/>
      <text class="pod-name" x="0" y="28" text-anchor="middle">${escapeHtml(name)}</text>
      <text class="pod-moist-text" x="0" y="44" text-anchor="middle">--</text>
    </g>`;
}

function renderGardenHero(activeZones) {
  const hero = $("garden-hero");
  const pods = $("hero-pods");
  if (!hero || !pods || !state.sensors?.zones) return;
  const zones = orderedZoneEntries(state.sensors.zones);
  // Pick the rows/columns that make the pods largest for the panel's current shape.
  const n = zones.length || 1;
  const cellW = 230, cellH = 300, HUD_SPACE = 40; // room above the pods for the status pill
  const W = hero.clientWidth || 1200, H = hero.clientHeight || 300;
  let best = { rows: 1, cols: n, scale: 0 };
  for (let rows = 1; rows <= n; rows++) {
    const cols = Math.ceil(n / rows);
    const scale = Math.min(W / (cols * cellW), H / (rows * cellH + HUD_SPACE));
    if (scale > best.scale + 0.01) best = { rows, cols, scale };
  }
  const key = `${zones.map(([name]) => name).join("\u0000")}|${best.rows}x${best.cols}`;
  if (pods.dataset.key !== key) {
    const vbW = Math.max(best.cols * cellW, 600), vbH = best.rows * cellH;
    const offsetX = (vbW - best.cols * cellW) / 2;
    hero.querySelector(".garden-hero-svg").setAttribute("viewBox", `0 ${-HUD_SPACE} ${vbW} ${vbH + HUD_SPACE}`);
    hero.querySelector(".hero-grid").setAttribute("transform", `translate(${vbW / 2 - 600} ${vbH - 300})`);
    pods.innerHTML = zones.map(([name], i) => {
      const r = Math.floor(i / best.cols);
      const inRow = Math.min(best.cols, n - r * best.cols);
      const rowOffset = offsetX + (best.cols - inRow) * cellW / 2;
      const x = rowOffset + cellW * ((i % best.cols) + 0.5);
      return `<g transform="translate(${x.toFixed(1)} ${r * cellH + 246})">${heroPodMarkup(name, i)}</g>`;
    }).join("");
    pods.dataset.key = key;
  }
  const active = new Set(activeZones);
  zones.forEach(([name, data], i) => {
    const pod = pods.querySelector(`[data-pod="${i}"]`);
    if (!pod) return;
    const m = Math.max(0, Math.min(100, Number(data.moisture) || 0));
    pod.classList.toggle("watering", active.has(name));
    pod.querySelector(".pod-moist").setAttribute("width", (40 * m / 100).toFixed(1));
    pod.querySelector(".pod-moist-text").textContent = `${active.has(name) ? "WATERING" : "MOISTURE"} ${m.toFixed(0)}%`;
  });
  const watering = active.size > 0;
  hero.classList.toggle("watering", watering);
  $("garden-hero-status").textContent = watering ? "WATERING" : "STANDBY";
  $("garden-hero-sub").textContent = watering
    ? Array.from(active).map(displayZoneLabel).join(" · ")
    : `${zones.length} zone${zones.length === 1 ? "" : "s"} monitored`;
  hero.setAttribute("aria-label", watering ? `Watering ${Array.from(active).join(", ")}` : "No zones watering");
}

// Re-lay out the pods when the panel changes size (window resize, phone rotation).
if (window.ResizeObserver) {
  document.addEventListener("DOMContentLoaded", () => {
    const hero = document.getElementById("garden-hero");
    if (hero) new ResizeObserver(() => {
      if (state.sensors) renderGardenHero(state.controller?.active_zones || []);
    }).observe(hero);
  });
}

/* ── Irrigation banner ── */
function renderBanner() {
  const d         = state.decision;
  const indicator = $("banner-indicator");
  const controllerActive = state.controller?.active_zones || [];
  const active    = controllerActive.length ? controllerActive : (d?.zones || []);
  renderGardenHero(active);

  if (active.length) {
    indicator.className  = "banner-indicator irrigating";
    $("banner-title").textContent = `Irrigating ${active.length} zone${active.length > 1 ? "s" : ""}`;
    $("banner-sub").textContent   = [active.map(displayZoneLabel).join(", "), d?.skip_reason].filter(Boolean).join(" · ");
  } else if (d?.skip_reason) {
    indicator.className  = "banner-indicator";
    $("banner-title").textContent = "Irrigation paused";
    $("banner-sub").textContent   = d.skip_reason;
  } else {
    indicator.className  = "banner-indicator";
    $("banner-title").textContent = state.controller?.mode === "auto"
      ? "Auto mode — monitoring" : "Manual mode";
    $("banner-sub").textContent = d?.reason || "All conditions normal";
  }
}

/* ── Mode pill + banner toggle ── */
function renderMode() {
  const mode   = state.controller?.mode || "manual";
  const locked = state.modeLocked;
  const pill   = $("mode-pill");

  if (mode === "auto") {
    pill.className        = "mode-pill auto";
    $("mode-icon").textContent  = "🤖";
    const autoHold = Number(state.controller?.auto_mode_hold_seconds_remaining) || 0;
    $("mode-label").textContent = autoHold > 0 ? "Auto (1h hold)" : (locked ? "Auto (pinned)" : "Auto");
  } else {
    pill.className        = "mode-pill";
    $("mode-icon").textContent  = "🔧";
    $("mode-label").textContent = locked ? "Manual (pinned)" : "Manual";
  }

  // Sync both toggles without triggering change events
  const cb1 = $("mode-checkbox");
  const cb2 = $("mode-checkbox-settings");
  if (cb1) cb1.checked = (mode === "auto");
  if (cb2) cb2.checked = (mode === "auto");

  // Highlight active mode option
  $("opt-manual")?.classList.toggle("active",      mode === "manual");
  $("opt-auto")?.classList.toggle("active-auto",   mode === "auto");

  // Label colours
  const l = $("mode-toggle-left");
  const r = $("mode-toggle-right");
  if (l) l.classList.toggle("active", mode === "manual");
  if (r) r.classList.toggle("active", mode === "auto");

  // All ON / All OFF — only usable in manual mode
  const isManual = mode === "manual";
  const allOn  = $("btn-all-on");
  const allOff = $("btn-all-off");
  if (allOn)  { allOn.disabled  = !isManual; allOn.title  = isManual ? "" : "Switch to Manual mode to control zones"; }
  if (allOff) { allOff.disabled = !isManual; allOff.title = isManual ? "" : "Switch to Manual mode to control zones"; }

  // Restore-GPS button
  const restoreBtn = $("restore-gps-mode-btn");
  if (restoreBtn) restoreBtn.style.display = locked ? "inline-flex" : "none";

  if (state.decision) renderBanner();
}

/* ── Geo card ── */
function renderGeoCard() {
  const dist = state.controller?.user_distance_miles;
  const mode = state.controller?.mode || "manual";
  const radius = Number(state.controller?.geofence_radius_miles) || 1;

  if (dist != null) {
    $("geo-distance").textContent = `${dist.toFixed(1)} mi`;

    if (dist > radius) {
      $("geo-distance").style.color = "var(--info)";
      $("geo-sub").textContent = `Away — ${mode === "auto" ? "Auto" : "Manual"} mode active (${dist.toFixed(1)} mi from home)`;
    } else {
      $("geo-distance").style.color = "var(--accent)";
      const hold = Number(state.controller?.auto_mode_hold_seconds_remaining) || 0;
      const modeDescription = mode === "auto" && hold > 0
        ? "Auto held for 1 hour"
        : `${mode === "auto" ? "Auto" : "Manual"} mode active`;
      $("geo-sub").textContent = `You're home — ${modeDescription}`;

      // Auto-fix: if GPS says you're home but toggle still shows Auto, silently restore manual
      if (mode === "auto") {
        restoreGpsMode(true);
      }
    }
  } else {
    $("geo-distance").textContent = "-- mi";
    $("geo-sub").textContent = state.gpsActive ? "Calculating distance…" : "GPS not yet active";
  }

  if (state.gpsLat != null) {
    $("geo-coords").textContent = `${state.gpsLat.toFixed(5)}, ${state.gpsLon.toFixed(5)}`;
  }
}

/* ── Settings section ── */
function renderSettingsSection() {
  // Live GPS (user position)
  $("sett-gps-status").textContent = state.gpsActive ? "✅ Active" : "⏳ Waiting…";
  $("sett-gps-status").style.color = state.gpsActive ? "var(--success)" : "var(--text-muted)";
  $("sett-lat").textContent = state.gpsLat != null ? state.gpsLat.toFixed(6) : "--";
  $("sett-lon").textContent = state.gpsLon != null ? state.gpsLon.toFixed(6) : "--";
  $("sett-acc").textContent = state.gpsAcc != null ? `±${state.gpsAcc.toFixed(0)} m` : "--";

  // Saved garden (home) location
  $("sett-home").textContent = state.homeLabel || "Not set";
  $("sett-home-lat").textContent = state.homeLat != null ? state.homeLat.toFixed(6) : "--";
  $("sett-home-lon").textContent = state.homeLon != null ? state.homeLon.toFixed(6) : "--";

  // Distance
  const dist = state.controller?.user_distance_miles;
  $("sett-distance").textContent = dist != null ? `${dist.toFixed(2)} mi` : "--";

  // Notification permission
  const perm = "Notification" in window ? Notification.permission : "unsupported";
  const permMap = { granted: "✅ Allowed", denied: "❌ Blocked", default: "⏳ Not asked", unsupported: "Not supported" };
  $("sett-notif-perm").textContent = permMap[perm] || perm;
  $("sett-notif-btn").style.display = perm === "granted" ? "none" : "inline-flex";
}

/* ── Sensor section ── */
function appendSensorReading(grid, icon, title, value, detail) {
  const card = document.createElement("div");
  card.className = "detail-card sensor-reading-card";

  const heading = document.createElement("div");
  heading.className = "detail-card-title";
  heading.textContent = `${icon} ${title}`;

  const reading = document.createElement("div");
  reading.className = "sensor-reading-value";
  reading.textContent = value;

  const description = document.createElement("div");
  description.className = "sensor-reading-detail";
  description.textContent = detail;

  card.append(heading, reading, description);
  grid.appendChild(card);
}

function sensorNumber(value, suffix = "", digits = 1) {
  if (value === null || value === undefined || value === "") return "Unavailable";
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(digits)}${suffix}` : "Unavailable";
}

function renderWeatherSensorCards(grid) {
  grid.innerHTML = "";
  const weather = state.weather || {};
  const available = weather.available === true;
  const uviAvailable = available && weather.uvi_available === true;
  const precipitationAvailable = available && weather.precipitation_available === true;

  appendSensorReading(
    grid, "🌡️", "Temperature",
    available ? sensorNumber(weather.temperature, "°C") : "Unavailable",
    available ? `Feels like ${sensorNumber(weather.feels_like, "°C")}` : (weather.error || "Garden weather unavailable")
  );
  appendSensorReading(
    grid, "💧", "Air Humidity",
    available ? sensorNumber(weather.humidity, "%", 0) : "Unavailable",
    "Relative humidity"
  );
  appendSensorReading(
    grid, "💨", "Wind Speed",
    available ? sensorNumber(weather.wind_speed_kmh, " km/h") : "Unavailable",
    available ? `${sensorNumber(weather.wind_speed, " m/s")} · ${weather.wind_speed > 7 ? "High wind" : "Normal"}` : "Current garden conditions"
  );
  appendSensorReading(
    grid, "☀️", "UV Index",
    uviAvailable ? sensorNumber(weather.uvi) : "Unavailable",
    uviAvailable ? uvLabel(Number(weather.uvi)) : "Open-Meteo UV reading"
  );
  appendSensorReading(
    grid, "☁️", "Cloud Cover",
    available ? sensorNumber(weather.clouds, "%", 0) : "Unavailable",
    "Cloud coverage over the garden"
  );
  appendSensorReading(
    grid, "🌧️", "Precipitation Forecast",
    precipitationAvailable ? sensorNumber(weather.precipitation_mm_24h, " mm") : "Unavailable",
    precipitationAvailable ? "Forecast total for the next 24 hours" : "24-hour forecast unavailable"
  );
  appendSensorReading(
    grid, "🌦️", "Rain Outlook",
    available && typeof weather.rain_next_24h === "boolean"
      ? (weather.rain_next_24h ? "Rain expected" : "No rain expected")
      : "Unavailable",
    "Next 24 hours"
  );
  appendSensorReading(
    grid, "🧭", "Air Pressure",
    available ? sensorNumber(weather.pressure, " hPa", 0) : "Unavailable",
    "Sea-level pressure"
  );
  appendSensorReading(
    grid, "🌤️", "Conditions",
    available ? (weather.weather_desc || weather.weather_main || "Unavailable") : "Unavailable",
    available ? `Garden location · ${weather.city || state.homeLabel}` : (weather.error || "Garden weather unavailable")
  );
}

function renderGpsSensorCards(grid) {
  grid.innerHTML = "";
  const controller = state.controller || {};
  const distance = controller.user_distance_miles;
  const radius = controller.geofence_radius_miles ?? 1;
  const deviceCoordinates = state.gpsLat != null && state.gpsLon != null
    ? `${state.gpsLat.toFixed(5)}, ${state.gpsLon.toFixed(5)}`
    : "Unavailable";
  const homeCoordinates = controller.home_lat != null && controller.home_lon != null
    ? `${Number(controller.home_lat).toFixed(5)}, ${Number(controller.home_lon).toFixed(5)}`
    : "Not set";

  appendSensorReading(
    grid, "📡", "Device GPS",
    state.gpsActive ? "Active" : "Off",
    state.gpsActive ? "Browser location permission granted" : "Enable browser location to share device GPS"
  );
  appendSensorReading(
    grid, "📍", "Device Coordinates",
    deviceCoordinates,
    state.gpsAcc != null ? `GPS accuracy ±${state.gpsAcc.toFixed(0)} m` : "Waiting for a GPS fix"
  );
  appendSensorReading(
    grid, "🏡", "Garden Location",
    state.homeLabel || "Garden location not set",
    homeCoordinates
  );
  appendSensorReading(
    grid, "🗺️", "Distance from Garden",
    distance != null && Number.isFinite(Number(distance)) ? `${Number(distance).toFixed(2)} mi` : "Unavailable",
    `${controller.mode || "manual"} mode · ${radius} mi geofence`
  );
}

function renderSensorSection() {
  const grid = $("sensor-grid");
  const moistureGrid = $("moisture-sensor-grid");
  renderWeatherSensorCards($("weather-sensor-grid"));
  renderGpsSensorCards($("gps-sensor-grid"));
  const zones = state.sensors.zones;
  const tank  = state.sensors.tank;
  grid.innerHTML = "";
  moistureGrid.innerHTML = "";

  // Tank
  const tankCard = document.createElement("div");
  tankCard.className = "detail-card";
  const tariffText = tank.water_tariff_gbp_per_m3 == null
    ? "Not set"
    : `£${Number(tank.water_tariff_gbp_per_m3).toFixed(4)} / m³`;
  const lastCostText = tank.last_fill_cost_gbp == null ? "Unknown — set the meter rate" : `£${Number(tank.last_fill_cost_gbp).toFixed(4)}`;
  tankCard.innerHTML = `
    <div class="detail-card-title">🪣 Water Tank</div>
    <div class="tank-visual">
      <div class="tank-fill" style="height:${tank.level}%">
        <div class="tank-wave"></div>
      </div>
      <div class="tank-pct-overlay">${tank.level}%</div>
    </div>
    <div class="detail-row"><span class="detail-key">Volume</span><span class="detail-val">${tank.litres} L / ${tank.capacity} L</span></div>
    <div class="detail-row"><span class="detail-key">Status</span><span class="detail-val">${tank.status}</span></div>
    <div class="detail-row"><span class="detail-key">Active zones</span><span class="detail-val">${tank.active_zones}</span></div>
    <div class="detail-row"><span class="detail-key">Last fill source</span><span class="detail-val">${escapeHtml(tank.last_fill_source || "Not recorded")}</span></div>
    <div class="detail-row"><span class="detail-key">Last fill volume</span><span class="detail-val">${Number(tank.last_fill_litres || 0).toFixed(1)} L</span></div>
    <div class="detail-row"><span class="detail-key">Rainwater caught</span><span class="detail-val">${Number(tank.rainwater_collected_litres || 0).toFixed(1)} L</span></div>
    <div class="detail-row"><span class="detail-key">Last mains cost estimate</span><span class="detail-val">${lastCostText}</span></div>
    <div class="detail-row"><span class="detail-key">Mains rate</span><span class="detail-val">${tariffText}</span></div>
    <div class="detail-row"><span class="detail-key">Mains cost estimate this session</span><span class="detail-val">${tank.water_tariff_gbp_per_m3 == null ? "Unknown — tariff not set" : `£${Number(tank.mains_cost_gbp_total || 0).toFixed(2)}`}</span></div>
    <br/><button class="btn btn-sm btn-outline" onclick="refillTank()">↺ Top up from mains</button>`;
  grid.appendChild(tankCard);

  for (const [name, data] of orderedZoneEntries(zones)) {
    const label = displayZoneLabel(name);
    const cl = data.moisture < 25 ? "critical" : data.moisture < 40 ? "low" : "";
    const card = document.createElement("div");
    card.className = "detail-card";
    card.innerHTML = `
      <div class="detail-card-title">🌱 ${escapeHtml(label)} Moisture Sensor</div>
      <div class="progress-wrap" style="margin-bottom:6px">
        <div class="progress"><div class="progress-fill ${cl}" style="width:${data.moisture}%"></div></div>
        <div class="progress-pct">${data.moisture}%</div>
      </div>
      <div class="detail-row"><span class="detail-key">Status</span><span class="detail-val">${data.status}</span></div>
      <div class="detail-row"><span class="detail-key">Irrigating</span><span class="detail-val">${data.is_irrigating ? "✅ Yes" : "❌ No"}</span></div>
      <div class="detail-row"><span class="detail-key">Needs water</span><span class="detail-val">${data.needs_water ? "⚠️ Yes" : "✅ No"}</span></div>`;
    moistureGrid.appendChild(card);
  }
}

/* ── Zone detail section ── */
function renderZoneDetailSection() {
  const grid       = $("zones-detail-grid");
  const zones      = state.sensors.zones;
  const zoneStates = state.controller.zone_states || {};
  grid.innerHTML   = "";

  for (const [name, data] of orderedZoneEntries(zones)) {
    const label = displayZoneLabel(name);
    const isOn = zoneStates[name] || false;
    const cl   = data.moisture < 25 ? "critical" : data.moisture < 40 ? "low" : "";
    const card = document.createElement("div");
    card.className = "detail-card";
    card.innerHTML = `
      <div class="detail-card-title">🌿 ${escapeHtml(label)}</div>
      <div class="detail-row">
        <span class="detail-key">Irrigation</span>
        <div class="toggle-wrap">
          <label class="toggle">
            <input type="checkbox" ${isOn ? "checked" : ""} onchange="toggleZone('${name}', this.checked)"/>
            <span class="toggle-slider"></span>
          </label>
          <span class="toggle-label">${isOn ? "ON 💧" : "OFF"}</span>
        </div>
      </div>
      <div class="detail-row"><span class="detail-key">Soil Moisture</span><span class="detail-val">${data.moisture}%</span></div>
      <div class="progress-wrap">
        <div class="progress"><div class="progress-fill ${cl}" style="width:${data.moisture}%"></div></div>
        <div class="progress-pct">${data.moisture}%</div>
      </div>
      <div class="detail-row"><span class="detail-key">Status</span><span class="detail-val">${data.status}</span></div>
      <div class="detail-row"><span class="detail-key">Needs water</span><span class="detail-val">${data.needs_water ? "⚠️ Yes" : "✅ No"}</span></div>`;
    grid.appendChild(card);
  }
}

/* ── Weather detail ── */
function renderWeatherDetail() {
  const w    = state.weather;
  const grid = $("weather-detail-grid");
  grid.innerHTML = "";
  const weatherAvailable = w.available !== false;
  const uvAvailable = w.uvi_available === true
    && typeof w.uvi === "number" && Number.isFinite(w.uvi);
  const precipitationAvailable = weatherAvailable && w.precipitation_available === true
    && typeof w.precipitation_mm_24h === "number" && Number.isFinite(w.precipitation_mm_24h);
  const show = value => weatherAvailable ? value : "Unavailable";

  const sections = [
    ["🌡️ Temperature", [
      ["Current",     weatherAvailable ? `${w.temperature}°C` : "--"],
      ["Feels like",  weatherAvailable ? `${w.feels_like}°C` : "--"],
      ["Humidity",    weatherAvailable ? `${w.humidity}%` : "--"],
      ["Pressure",    weatherAvailable ? `${w.pressure} hPa` : "--"],
    ]],
    ["💨 Wind & Sky", [
      ["Wind speed",     weatherAvailable ? `${w.wind_speed_kmh} km/h` : "--"],
      ["Cloud cover",    weatherAvailable ? `${w.clouds}%` : "--"],
    ]],
    ["☀️ UV & Precipitation", [
      ["Current UV index", uvAvailable ? `${Number(w.uvi).toFixed(1)} (${uvLabel(Number(w.uvi))})` : "Unavailable"],
      ["Condition",       show(w.weather_main)],
      ["Description",     w.weather_desc],
      ["Forecast next 24h", precipitationAvailable ? `${Number(w.precipitation_mm_24h).toFixed(1)} mm` : "Unavailable"],
      ["Currently raining", weatherAvailable ? (w.is_rainy ? "🌧️ Yes" : "No") : "--"],
      ["Rain in 24h",     weatherAvailable ? (w.rain_next_24h ? "⚠️ Expected" : "No") : "--"],
    ]],
    ["📍 Location", [
      ["Garden location", w.city],
      ["Coordinates", `${Number(w.latitude).toFixed(5)}, ${Number(w.longitude).toFixed(5)}`],
      ["Weather source", w.source === "openweathermap" ? "OpenWeather" : "Weather unavailable"],
      ["UV data source", '<a href="https://open-meteo.com/" target="_blank" rel="noopener noreferrer">Open-Meteo</a> · CC BY 4.0'],
      ["Updated",     new Date(w.timestamp).toLocaleTimeString()],
    ]],
  ];

  for (const [title, rows] of sections) {
    const card = document.createElement("div");
    card.className = "detail-card";
    card.innerHTML = `
      <div class="detail-card-title">${title}</div>
      ${rows.map(([k,v]) => `
        <div class="detail-row">
          <span class="detail-key">${k}</span>
          <span class="detail-val">${v}</span>
        </div>`).join("")}`;
    grid.appendChild(card);
  }
}

// ─── Mode toggle ─────────────────────────────────

function setMode(auto) {
  const mode = auto ? "auto" : "manual";
  state.modeLocked = true;
  socket.emit("set_mode", { mode, use_gps: false });
  // Optimistic UI update
  if (state.controller) state.controller.mode = mode;
  renderMode();
}

function restoreGpsMode(silent = false) {
  state.modeLocked = false;
  socket.emit("set_mode", { use_gps: true, force: true });
  if (!silent) showToast("📍 Mode control restored to GPS", "info");
  renderMode();
}

// ─── Zone controls ───────────────────────────────

function toggleZone(zone, on) {
  socket.emit("zone_toggle", { zone, on });
}

function toggleAllZones(on) {
  socket.emit("all_zones_toggle", { on });
}

function refillTank() {
  fetch(`${API_BASE}/api/tank/refill`, { method: "POST", credentials: "include" })
    .then(async response => {
      const body = await response.json();
      if (!response.ok || !body.ok) throw new Error(body.error || "Could not top up the tank.");
      if (state.sensors) state.sensors.tank = body.tank;
      if (state.weather) renderWeatherCards();
      if (state.sensors && state.controller) renderSensorSection();
      if (body.tank.last_fill_cost_gbp == null) {
        showToast("Tank topped up from mains. Add your metered tariff in Settings to estimate the cost.", "info");
      } else {
        showToast(`Mains top-up cost estimate: £${Number(body.tank.last_fill_cost_gbp).toFixed(2)}`, "success");
      }
    })
    .catch(error => showToast(error.message || "Could not top up the tank.", "warning"));
}

window.toggleZone   = toggleZone;
window.toggleAllZones = toggleAllZones;
window.refillTank   = refillTank;

// ─── Postcode lookup (browser → postcodes.io directly, CORS-safe) ────

async function lookupPostcode(raw) {
  const pc = raw.replace(/\s/g, "").toUpperCase();
  if (!pc) return;

  const resultEl = $("postcode-result");
  const btn      = $("set-postcode-btn");
  if (resultEl) { resultEl.className = "postcode-result loading"; resultEl.textContent = "Looking up…"; }
  if (btn) btn.disabled = true;

  try {
    // Call postcodes.io directly from the browser — fully CORS-enabled, no key needed
    const r = await fetch(`https://api.postcodes.io/postcodes/${pc}`);
    const d = await r.json();
    if (d.status === 200) {
      const { latitude: lat, longitude: lon, postcode, admin_ward, admin_district } = d.result;
      const label = `${postcode}, ${admin_ward}, ${admin_district}`;
      setHomeLocation(lat, lon, label);
      if (resultEl) { resultEl.className = "postcode-result ok"; resultEl.textContent = `✅ Home set to ${label}`; }
    } else {
      if (resultEl) { resultEl.className = "postcode-result error"; resultEl.textContent = `❌ Postcode not found`; }
    }
  } catch (e) {
    if (resultEl) { resultEl.className = "postcode-result error"; resultEl.textContent = "❌ Could not reach postcodes.io"; }
  }
  if (btn) btn.disabled = false;
}

function setHomeLocation(lat, lon, label) {
  state.homeLat  = lat;
  state.homeLon  = lon;
  state.homeLabel = label;
  socket?.emit("set_home", { lat, lon, label });
  showToast(`🏡 Home set to ${label}`, "success");
  $("sett-home").textContent = label;
}

// ─── GPS ─────────────────────────────────────────

let _gpsWatchId = null;

function seedGardenFromGPS() {
  if (!state.gpsActive || state.gpsLat == null || state.gpsLon == null) return false;
  if (state.homeLat != null && state.homeLon != null) return false;
  if (!state.controller || (state.controller.home_lat != null && state.controller.home_lon != null)) return false;

  const label = `Garden GPS ${state.gpsLat.toFixed(4)}, ${state.gpsLon.toFixed(4)}`;
  setHomeLocation(state.gpsLat, state.gpsLon, label);
  return true;
}

function startGPS() {
  if (!navigator.geolocation) {
    setGPSStatus(false, "Not supported");
    return;
  }

  const opts = { enableHighAccuracy: true, maximumAge: 0, timeout: 20000 };

  function onPos(pos) {
    const { latitude: lat, longitude: lon, accuracy: acc } = pos.coords;
    state.gpsLat  = lat;
    state.gpsLon  = lon;
    state.gpsAcc  = acc;
    state.gpsActive = true;
    setGPSStatus(true, `${lat.toFixed(4)}, ${lon.toFixed(4)}`);

    // Set a real garden point on first use, while preserving any configured
    // garden location. Emit the live fix afterward so distance is calculated.
    seedGardenFromGPS();
    socket.emit("location_update", { lat, lon });

    renderGeoCard();
    renderSettingsSection();
  }

  function onErr(err) {
    state.gpsActive = false;
    setGPSStatus(false, "Unavailable");
    showToast(`📍 Location unavailable: ${err.message || "check browser permission"}`, "warning");
    renderSettingsSection();
  }

  // Use watchPosition for continuous tracking
  if (_gpsWatchId !== null) navigator.geolocation.clearWatch(_gpsWatchId);
  _gpsWatchId = navigator.geolocation.watchPosition(onPos, onErr, opts);
}

function setGPSStatus(active, text) {
  const dot  = $("gps-dot");
  const txt  = $("gps-text");
  if (dot) dot.className = "gps-dot " + (active ? "active" : "error");
  if (txt) txt.textContent = active ? text : "GPS off";
}

// ─── Notifications ───────────────────────────────

function handleNotifications(notifications) {
  for (const n of notifications) {
    const isDupe = state.notifications.some(
      x => x.tag === n.tag && x.timestamp === n.timestamp
    );
    if (!isDupe) state.notifications.unshift(n);
    showToast(n.message, n.level);
    sendWebPush(n);
  }
  state.notifications = state.notifications.slice(0, 100);
  renderNotifList();
  updateBadge();
}

function renderNotifList() {
  const list = $("notif-list");
  if (!state.notifications.length) {
    list.innerHTML = `<div class="notif-empty">No notifications yet</div>`;
    return;
  }
  list.innerHTML = state.notifications.map(n => `
    <div class="notif-item ${n.level}">
      <div class="notif-body">
        <div class="notif-msg">${bold(n.message)}</div>
        <div class="notif-time">${new Date(n.timestamp).toLocaleTimeString()}</div>
      </div>
    </div>`).join("");
}

function updateBadge() {
  const badge = $("notif-badge");
  const count = state.notifications.length;
  badge.style.display = count ? "inline-flex" : "none";
  badge.textContent   = count > 99 ? "99+" : count;
  $("alerts-btn").setAttribute("aria-label", count ? `Open alerts, ${count} notifications` : "Open alerts");
}

// ─── Toast ───────────────────────────────────────

const _recentToasts = new Set();

function showToast(message, level = "info") {
  const key = `${message}|${level}`;
  if (_recentToasts.has(key)) return;
  _recentToasts.add(key);
  setTimeout(() => _recentToasts.delete(key), 5000);

  const icons = { danger: "🚨", warning: "⚠️", success: "✅", info: "ℹ️" };
  const toast = document.createElement("div");
  toast.className = `toast ${level}`;
  toast.innerHTML = `
    <span class="toast-icon">${icons[level] || "ℹ️"}</span>
    <span>${bold(message)}</span>
    <button class="toast-close" onclick="dismissToast(this.parentElement)">✕</button>`;

  $("toast-container").prepend(toast);
  setTimeout(() => dismissToast(toast), 5000);
}

function dismissToast(toast) {
  if (!toast?.parentElement) return;
  toast.classList.add("fade-out");
  toast.addEventListener("animationend", () => toast.remove(), { once: true });
}

// ─── Web Push ────────────────────────────────────

async function requestNotifPermission() {
  if (!("Notification" in window)) return;
  const perm = await Notification.requestPermission();
  renderSettingsSection();
  return perm;
}

function sendWebPush(n) {
  if (window.Notification?.permission === "granted" && document.hidden) {
    try {
      new Notification("🌿 Irrigate", {
        body: n.message.replace(/\*\*/g, ""),
        tag:  n.tag,
      });
    } catch (_) {}
  }
}

// ─── Navigation ──────────────────────────────────

function navigate(section) {
  $$(".nav-item").forEach(el =>
    el.classList.toggle("active", el.dataset.section === section)
  );
  $("alerts-btn").classList.toggle("active", section === "notifications");
  $$(".section").forEach(el =>
    el.classList.toggle("active", el.id === `section-${section}`)
  );
  const titles = {
    dashboard: "Dashboard", zones: "Garden Zones",
    weather: "Weather", sensors: "Sensors",
    profile: "My Profile",
    settings: "Settings", notifications: "Alerts",
  };
  $("page-title").textContent = titles[section] || section;
  if (section === "notifications") { state.notifications; updateBadge(); }
  if (window.innerWidth < 768) $("sidebar").classList.remove("open");
}

// ─── Theme ───────────────────────────────────────

function toggleTheme() {
  document.body.classList.toggle("dark");
  $("theme-btn").textContent = document.body.classList.contains("dark") ? "🌙" : "☀️";
  localStorage.setItem("theme", document.body.classList.contains("dark") ? "dark" : "light");
}

// ─── Helpers ─────────────────────────────────────

function buildArc(pct) {
  const r = 30, c = 2 * Math.PI * r;
  return { dash: c.toFixed(2), offset: (c - (pct / 100) * c).toFixed(2) };
}

function bold(str) {
  return str.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
}

function updateConnDot(on) {
  const d = $("conn-dot");
  d.className = "connection-dot" + (on ? " connected" : "");
  d.title = on ? "Connected" : "Disconnected";
}

// ─── Account and first-run garden setup ─────────────────

function showAuth(message = "") {
  document.body.classList.add("auth-locked");
  $("auth-shell").classList.remove("hidden");
  $("onboarding-shell").classList.add("hidden");
  $("auth-error").textContent = message;
  $("auth-email").focus();
}

function showOnboarding() {
  document.body.classList.add("auth-locked");
  $("auth-shell").classList.add("hidden");
  $("onboarding-shell").classList.remove("hidden");
  $("onboarding-error").textContent = "";
  if (!$("onboarding-gardens").children.length) addOnboardingGarden({ name: "Home garden" });
}

function setAuthMode(mode) {
  authMode = mode;
  const signup = mode === "signup";
  $("auth-login-tab").classList.toggle("active", !signup);
  $("auth-signup-tab").classList.toggle("active", signup);
  $("auth-login-tab").setAttribute("aria-selected", String(!signup));
  $("auth-signup-tab").setAttribute("aria-selected", String(signup));
  $("auth-name-field").classList.toggle("hidden", !signup);
  $("auth-name").required = signup;
  $("auth-password").autocomplete = signup ? "new-password" : "current-password";
  $("auth-title").textContent = signup ? "Create your account" : "Welcome to your garden";
  $("auth-submit").textContent = signup ? "Create account" : "Log in";
  $("auth-error").textContent = "";
}

function addOnboardingGarden(initial = {}) {
  const list = $("onboarding-gardens");
  if (list.children.length >= 10) {
    $("onboarding-error").textContent = "You can add up to 10 garden locations.";
    return;
  }
  const fragment = $("onboarding-garden-template").content.cloneNode(true);
  const row = fragment.querySelector("[data-garden-row]");
  const index = list.children.length + 1;
  row.querySelector("[data-garden-name]").value = initial.name || `Garden ${index}`;
  row.querySelector("[data-garden-title]").textContent = `Garden ${index}`;
  row.querySelector("[data-garden-remove]").addEventListener("click", () => {
    row.remove();
    renumberOnboardingGardens();
  });
  row.querySelector("[data-garden-name]").addEventListener("input", event => {
    row.querySelector("[data-garden-title]").textContent = event.target.value.trim() || `Garden ${Array.from(list.children).indexOf(row) + 1}`;
  });
  row.querySelector("[data-garden-postcode]").addEventListener("input", () => {
    delete row.dataset.latitude;
    delete row.dataset.longitude;
    delete row.dataset.locationLabel;
    delete row.dataset.postcode;
    row.querySelector("[data-garden-coordinate]").textContent = "Choose a postcode or use GPS";
    row.querySelector("[data-garden-result]").textContent = "";
  });
  row.querySelector("[data-garden-find]").addEventListener("click", () => findOnboardingPostcode(row));
  row.querySelector("[data-garden-postcode]").addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      findOnboardingPostcode(row);
    }
  });
  row.querySelector("[data-garden-gps]").addEventListener("click", () => setOnboardingGardenGPS(row));
  buildZoneEditor(row.querySelector("[data-zone-editor]"), initial.zones || DEFAULT_ZONES);
  list.appendChild(fragment);
  renumberOnboardingGardens();
}

function renumberOnboardingGardens() {
  const rows = Array.from($("onboarding-gardens").querySelectorAll("[data-garden-row]"));
  rows.forEach((row, index) => {
    row.querySelector("[data-garden-title]").textContent = row.querySelector("[data-garden-name]").value.trim() || `Garden ${index + 1}`;
    row.querySelector("[data-garden-remove]").disabled = rows.length === 1;
  });
  $("onboarding-location-count").textContent = `${rows.length} location${rows.length === 1 ? "" : "s"}`;
}

async function findOnboardingPostcode(row) {
  const input = row.querySelector("[data-garden-postcode]");
  const result = row.querySelector("[data-garden-result]");
  const postcode = input.value.trim().replace(/\s+/g, "").toUpperCase();
  if (!postcode) {
    result.className = "garden-result error";
    result.textContent = "Enter a UK postcode first.";
    return;
  }
  result.className = "garden-result loading";
  result.textContent = "Finding location…";
  row.querySelector("[data-garden-find]").disabled = true;
  try {
    const response = await fetch(`https://api.postcodes.io/postcodes/${encodeURIComponent(postcode)}`);
    const body = await response.json();
    if (body.status !== 200 || !body.result) throw new Error("Postcode not found. Check it and try again.");
    const found = body.result;
    const label = [found.postcode, found.admin_ward, found.admin_district].filter(Boolean).join(", ");
    row.dataset.latitude = found.latitude;
    row.dataset.longitude = found.longitude;
    row.dataset.locationLabel = label;
    row.dataset.postcode = found.postcode;
    row.querySelector("[data-garden-coordinate]").textContent = `${Number(found.latitude).toFixed(5)}, ${Number(found.longitude).toFixed(5)}`;
    result.className = "garden-result ok";
    result.textContent = `Location found: ${label}`;
  } catch (error) {
    result.className = "garden-result error";
    result.textContent = error.message || "Could not look up that postcode.";
  } finally {
    row.querySelector("[data-garden-find]").disabled = false;
  }
}

function setOnboardingGardenGPS(row) {
  const result = row.querySelector("[data-garden-result]");
  if (!navigator.geolocation) {
    result.className = "garden-result error";
    result.textContent = "GPS is not available in this browser.";
    return;
  }
  result.className = "garden-result loading";
  result.textContent = "Waiting for GPS permission…";
  navigator.geolocation.getCurrentPosition(position => {
    const { latitude, longitude } = position.coords;
    const label = `GPS ${latitude.toFixed(5)}, ${longitude.toFixed(5)}`;
    row.dataset.latitude = latitude;
    row.dataset.longitude = longitude;
    row.dataset.locationLabel = label;
    row.dataset.postcode = "";
    row.querySelector("[data-garden-postcode]").value = "";
    row.querySelector("[data-garden-coordinate]").textContent = `${latitude.toFixed(5)}, ${longitude.toFixed(5)}`;
    result.className = "garden-result ok";
    result.textContent = "Current GPS location saved for this garden.";
  }, error => {
    result.className = "garden-result error";
    result.textContent = error.code === 1 ? "GPS permission was denied. Allow location access and try again." : "Could not get a GPS fix. Try again or enter a postcode.";
  }, { enableHighAccuracy: true, maximumAge: 0, timeout: 20000 });
}

async function submitAccount(event) {
  event.preventDefault();
  const button = $("auth-submit");
  button.disabled = true;
  button.textContent = authMode === "signup" ? "Creating account…" : "Signing in…";
  $("auth-error").textContent = "";
  try {
    const response = await fetch(`${API_BASE}/api/auth/${authMode}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({
        name: $("auth-name").value.trim(),
        email: $("auth-email").value.trim(),
        password: $("auth-password").value,
      }),
    });
    const body = await response.json();
    if (!response.ok || !body.ok) throw new Error(body.error || "Could not sign in. Try again.");
    if (body.needs_onboarding) {
      state.user = body.user;
      showOnboarding();
    } else {
      launchDashboard(body.user);
    }
  } catch (error) {
    $("auth-error").textContent = error.message || "Could not reach Irrigate. Check your connection and try again.";
    button.textContent = authMode === "signup" ? "Create account" : "Log in";
  } finally {
    button.disabled = false;
  }
}

async function submitOnboarding(event) {
  event.preventDefault();
  const button = $("onboarding-submit");
  const rows = Array.from($("onboarding-gardens").querySelectorAll("[data-garden-row]"));
  const gardens = rows.map(row => ({
    name: row.querySelector("[data-garden-name]").value.trim(),
    postcode: row.dataset.postcode || "",
    location_label: row.dataset.locationLabel || "",
    latitude: row.dataset.latitude,
    longitude: row.dataset.longitude,
    zones: readZoneEditor(row.querySelector("[data-zone-editor]")),
  }));
  $("onboarding-error").textContent = "";
  button.disabled = true;
  button.textContent = "Saving your garden…";
  try {
    const response = await fetch(`${API_BASE}/api/onboarding`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ plant_type: $("onboarding-plant").value, soil_type: $("onboarding-soil").value, gardens }),
    });
    const body = await response.json();
    if (!response.ok || !body.ok) throw new Error(body.error || "Could not save your garden profile.");
    launchDashboard(body.user);
  } catch (error) {
    $("onboarding-error").textContent = error.message || "Could not save your garden profile. Try again.";
  } finally {
    button.disabled = false;
    button.innerHTML = 'Save and open dashboard <span aria-hidden="true">→</span>';
  }
}

function renderGardenProfile(user) {
  state.user = user;
  state.gardens = user.gardens || [];
  state.tankSettings = user.tank_settings || {};
  state.activeGardenId = user.active_garden_id ?? state.gardens[0]?.id ?? null;
  state.plantType = user.plant_type;
  state.soilType = user.soil_type;
  const active = state.gardens.find(garden => garden.id === state.activeGardenId) || state.gardens[0];
  if (active) {
    state.activeGardenId = active.id;
    state.homeLat = Number(active.latitude);
    state.homeLon = Number(active.longitude);
    state.homeLabel = active.location_label || active.name;
  }
  const displayName = (user.display_name || "Garden owner").trim();
  $("profile-avatar").textContent = displayName.charAt(0).toUpperCase();
  $("profile-name").textContent = displayName;
  $("profile-email").textContent = user.email || "Email not available";
  $("profile-plant-type").textContent = user.plant_type || "Not set";
  $("profile-soil-type").textContent = user.soil_type || "Not set";
  $("profile-garden-count").textContent = `${state.gardens.length} ${state.gardens.length === 1 ? "garden" : "gardens"}`;
  $("profile-garden-heading-count").textContent = `${state.gardens.length} ${state.gardens.length === 1 ? "location" : "locations"}`;
  $("account-avatar").textContent = (user.display_name || user.email || "A").trim().charAt(0).toUpperCase();
  renderProfileGardens();
  const wrap = $("garden-switcher-wrap");
  const select = $("active-garden-select");
  select.replaceChildren(...state.gardens.map(garden => {
    const option = document.createElement("option");
    option.value = garden.id;
    option.textContent = garden.name;
    return option;
  }));
  select.value = state.activeGardenId == null ? "" : String(state.activeGardenId);
  wrap.classList.toggle("hidden", state.gardens.length < 2);
  renderTankSettingsEditor();
  renderSettingsSection();
}

function displayZoneLabel(zoneId) {
  return zoneId;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function renderTankSettingsEditor() {
  const settings = state.tankSettings || {};
  $("water-supplier-input").value = settings.water_supplier || "";
  $("catchment-area-input").value = settings.catchment_area_m2 ?? 20;
  $("water-tariff-input").value = settings.water_tariff_gbp_per_m3 ?? "";
}

async function saveTankSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const feedback = $("tank-settings-feedback");
  const rawRate = $("water-tariff-input").value.trim();
  const settings = {
    water_supplier: $("water-supplier-input").value.trim(),
    catchment_area_m2: Number($("catchment-area-input").value),
    water_tariff_gbp_per_m3: rawRate === "" ? null : Number(rawRate),
  };
  button.disabled = true;
  feedback.className = "loc-feedback loading";
  feedback.textContent = "Saving tank settings…";
  try {
    const response = await fetch(`${API_BASE}/api/tank/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(settings),
    });
    const body = await response.json();
    if (!response.ok || !body.ok) throw new Error(body.error || "Could not save tank settings.");
    if (state.sensors) state.sensors.tank = body.tank;
    renderGardenProfile(body.user);
    if (state.weather) renderWeatherCards();
    if (state.sensors && state.controller) renderSensorSection();
    feedback.className = "loc-feedback ok";
    feedback.textContent = "Tank and tariff settings saved to your account.";
    showToast("Tank settings saved", "success");
  } catch (error) {
    feedback.className = "loc-feedback error";
    feedback.textContent = error.message || "Could not save tank settings.";
  } finally {
    button.disabled = false;
  }
}

// ─── Per-garden zone editor ─────────────────────
const DEFAULT_ZONES = [
  { name: "Front Garden", sheltered: false },
  { name: "Back Garden",  sheltered: false },
  { name: "Side Bed",     sheltered: true  },
  { name: "Greenhouse",   sheltered: true  },
];
const MAX_ZONES = 8;
const PLANT_TYPES = ["Vegetables", "Herbs", "Flowers", "Lawn", "Fruit trees", "Mixed garden", "Other"];
const SOIL_TYPES = ["Loamy", "Clay", "Sandy", "Silty", "Peaty", "Chalky", "Other"];

function makeTypeSelect(kind, options, value) {
  const select = document.createElement("select");
  select.className = "loc-input zone-type-select";
  select.dataset[kind] = "";
  select.setAttribute("aria-label", kind === "zonePlant" ? "Plant type" : "Soil type");
  const inherit = document.createElement("option");
  inherit.value = "";
  inherit.textContent = kind === "zonePlant" ? "Plant: same as profile" : "Soil: same as profile";
  select.appendChild(inherit);
  for (const option of options) {
    const el = document.createElement("option");
    el.value = option;
    el.textContent = option;
    select.appendChild(el);
  }
  select.value = options.includes(value) ? value : "";
  return select;
}

/* Plant / soil details for a zone of the active garden. */
function zoneTypeText(zoneName) {
  const zone = activeZoneInfo(zoneName);
  return zone ? [zone.plant_type, zone.soil_type].filter(Boolean).join(" · ") : "";
}

function activeZoneInfo(zoneName) {
  const garden = state.gardens.find(g => g.id === state.activeGardenId);
  return garden?.zones?.find(z => z.name === zoneName) || null;
}

/* Fill `container` with editable rows (name + sheltered + remove) and an "Add zone" button. */
function buildZoneEditor(container, zones) {
  container.replaceChildren();
  const rows = document.createElement("div");
  rows.className = "zone-list-editor";
  const footer = document.createElement("div");
  footer.className = "zone-edit-footer";
  const add = document.createElement("button");
  add.type = "button";
  add.className = "text-action";
  add.textContent = "＋ Add zone";
  const count = document.createElement("span");
  count.className = "zone-edit-count";
  footer.append(add, count);

  const refresh = () => {
    const all = rows.querySelectorAll(".zone-edit-row");
    all.forEach(r => { r.querySelector("[data-zone-remove]").disabled = all.length === 1; });
    add.disabled = all.length >= MAX_ZONES;
    count.textContent = `${all.length} of ${MAX_ZONES} zones`;
  };
  const addRow = (zone = { name: "", sheltered: false }) => {
    const row = document.createElement("div");
    row.className = "zone-edit-row";
    const name = document.createElement("input");
    name.type = "text";
    name.className = "loc-input";
    name.maxLength = 40;
    name.required = true;
    name.placeholder = "e.g. Veg patch";
    name.value = zone.name || "";
    name.dataset.zoneName = "";
    name.setAttribute("aria-label", "Zone name");
    const shelter = document.createElement("label");
    shelter.className = "zone-sheltered";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = Boolean(zone.sheltered);
    box.dataset.zoneSheltered = "";
    shelter.append(box, document.createTextNode("Sheltered"));
    shelter.title = "Rain can’t reach this zone (e.g. a greenhouse), so Auto mode may still water it while it rains.";
    const plant = makeTypeSelect("zonePlant", PLANT_TYPES, zone.plant_type);
    const soil = makeTypeSelect("zoneSoil", SOIL_TYPES, zone.soil_type);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-button remove-garden-btn";
    remove.dataset.zoneRemove = "";
    remove.setAttribute("aria-label", "Remove zone");
    remove.textContent = "×";
    remove.addEventListener("click", () => { row.remove(); refresh(); });
    const types = document.createElement("div");
    types.className = "zone-edit-types";
    types.append(plant, soil);
    row.append(name, shelter, remove, types);
    rows.appendChild(row);
    refresh();
    return name;
  };
  add.addEventListener("click", () => addRow().focus());
  (zones && zones.length ? zones : DEFAULT_ZONES).forEach(zone => addRow(zone));
  container.append(rows, footer);
}

function readZoneEditor(container) {
  return Array.from(container.querySelectorAll(".zone-edit-row")).map(row => ({
    name: row.querySelector("[data-zone-name]").value.trim(),
    sheltered: row.querySelector("[data-zone-sheltered]").checked,
    plant_type: row.querySelector("[data-zone-plant]").value || null,
    soil_type: row.querySelector("[data-zone-soil]").value || null,
  }));
}

async function saveGardenZones(garden, editorEl, feedback, button) {
  button.disabled = true;
  feedback.className = "loc-feedback loading";
  feedback.textContent = "Saving zones…";
  try {
    const response = await fetch(`${API_BASE}/api/gardens/${garden.id}/zones`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ zones: readZoneEditor(editorEl) }),
    });
    const body = await response.json();
    if (!response.ok || !body.ok) throw new Error(body.error || "Could not save zones.");
    renderGardenProfile(body.user);
    if (garden.id === state.activeGardenId) {
      const statusResponse = await fetch(`${API_BASE}/api/status`, { credentials: "include" });
      if (statusResponse.ok) {
        const status = await statusResponse.json();
        state.weather = status.weather;
        state.sensors = status.sensors;
        state.controller = status.controller;
        renderAll();
      }
    }
    showToast(`Zones saved for ${garden.name}`, "success");
  } catch (error) {
    feedback.className = "loc-feedback error";
    feedback.textContent = error.message || "Could not save zones.";
    button.disabled = false;
  }
}

function buildProfileZones(garden) {
  const wrap = document.createElement("div");
  wrap.className = "profile-garden-zones";
  const chips = document.createElement("div");
  chips.className = "profile-zone-chips";
  for (const zone of garden.zones || []) {
    const chip = document.createElement("span");
    chip.className = "profile-zone-chip";
    const parts = [zone.name, [zone.plant_type, zone.soil_type].filter(Boolean).join(" / ")];
    if (zone.sheltered) parts.push("sheltered");
    chip.textContent = parts.filter(Boolean).join(" · ");
    chips.appendChild(chip);
  }
  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "btn btn-sm btn-outline";
  edit.textContent = "Edit zones";
  const panel = document.createElement("div");
  panel.className = "hidden";
  edit.addEventListener("click", () => {
    const editorEl = document.createElement("div");
    buildZoneEditor(editorEl, garden.zones);
    const actions = document.createElement("div");
    actions.className = "profile-zone-actions";
    const save = document.createElement("button");
    save.type = "button";
    save.className = "btn btn-sm btn-primary";
    save.textContent = "Save zones";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn btn-sm btn-ghost";
    cancel.textContent = "Cancel";
    const feedback = document.createElement("div");
    feedback.className = "loc-feedback";
    feedback.setAttribute("role", "status");
    save.addEventListener("click", () => saveGardenZones(garden, editorEl, feedback, save));
    cancel.addEventListener("click", () => {
      panel.classList.add("hidden");
      panel.replaceChildren();
      chips.classList.remove("hidden");
      edit.classList.remove("hidden");
    });
    actions.append(save, cancel, feedback);
    panel.replaceChildren(editorEl, actions);
    panel.classList.remove("hidden");
    chips.classList.add("hidden");
    edit.classList.add("hidden");
    editorEl.querySelector("[data-zone-name]")?.focus();
  });
  wrap.append(chips, edit, panel);
  return wrap;
}

function renderProfileGardens() {
  const list = $("profile-gardens");
  list.replaceChildren();
  for (const garden of state.gardens) {
    const active = garden.id === state.activeGardenId;
    const card = document.createElement("article");
    card.className = `profile-garden-card${active ? " active" : ""}`;

    const icon = document.createElement("div");
    icon.className = "profile-garden-icon";
    icon.textContent = "📍";

    const details = document.createElement("div");
    details.className = "profile-garden-details";
    const name = document.createElement("h4");
    name.textContent = garden.name;
    const location = document.createElement("p");
    location.textContent = garden.location_label || "Garden location saved";
    const coordinates = document.createElement("span");
    coordinates.className = "profile-garden-coordinates";
    coordinates.textContent = `${Number(garden.latitude).toFixed(5)}, ${Number(garden.longitude).toFixed(5)}`;
    details.append(name, location, coordinates);

    const status = document.createElement("div");
    status.className = "profile-garden-status";
    if (active) {
      status.classList.add("selected");
      status.textContent = "In use";
    } else {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-sm btn-outline";
      button.textContent = "Use this garden";
      button.addEventListener("click", () => changeActiveGarden(garden.id));
      status.appendChild(button);
    }
    card.append(icon, details, status, buildProfileZones(garden));
    list.appendChild(card);
  }
}

function launchDashboard(user) {
  $("auth-shell").classList.add("hidden");
  $("onboarding-shell").classList.add("hidden");
  document.body.classList.remove("auth-locked");
  renderGardenProfile(user);
  connectDashboardSocket();
  const gpsChoice = localStorage.getItem("gps_ok");
  if (gpsChoice === "1") {
    startGPS();
    requestNotifPermission();
  } else {
    setTimeout(() => $("gps-modal").classList.remove("hidden"), 700);
  }
}

async function bootstrapAccount() {
  try {
    const response = await fetch(`${API_BASE}/api/auth/me`, { credentials: "include" });
    if (response.status === 401) return showAuth();
    const body = await response.json();
    if (!response.ok || !body.ok) throw new Error(body.error || "Could not load your account.");
    if (body.needs_onboarding) {
      state.user = body.user;
      return showOnboarding();
    }
    launchDashboard(body.user);
  } catch (_) {
    showAuth("Irrigate could not be reached. Check your connection, then try again.");
  }
}

async function logout() {
  try {
    await fetch(`${API_BASE}/api/auth/logout`, { method: "POST", credentials: "include" });
  } finally {
    if (socket) socket.disconnect();
    window.location.reload();
  }
}

async function changeActiveGarden(gardenId) {
  try {
    const response = await fetch(`${API_BASE}/api/gardens/active`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ garden_id: gardenId }),
    });
    const body = await response.json();
    if (!response.ok || !body.ok) throw new Error(body.error || "Could not switch gardens.");
    renderGardenProfile(body.user);
    const garden = body.garden;
    socket?.emit("set_home", { lat: garden.latitude, lon: garden.longitude, label: garden.location_label });
    const statusResponse = await fetch(`${API_BASE}/api/status`, { credentials: "include" });
    if (statusResponse.ok) {
      const status = await statusResponse.json();
      state.weather = status.weather;
      state.sensors = status.sensors;
      state.controller = status.controller;
      renderAll();
    }
    showToast(`Garden switched to ${garden.name}`, "success");
  } catch (error) {
    showToast(error.message || "Could not switch gardens.", "warning");
  }
}

// ─── Boot ────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {

  // Theme
  if (localStorage.getItem("theme") === "light") document.body.classList.remove("dark");

  // Navigation
  $$(".nav-item").forEach(el =>
    el.addEventListener("click", e => { e.preventDefault(); navigate(el.dataset.section); })
  );

  // Hamburger
  $("hamburger").addEventListener("click", () => $("sidebar").classList.toggle("open"));
  document.addEventListener("click", e => {
    const sb = $("sidebar");
    if (window.innerWidth < 768 && sb.classList.contains("open")
        && !sb.contains(e.target) && !$("hamburger").contains(e.target))
      sb.classList.remove("open");
  });

  // Theme toggle
  $("theme-btn").addEventListener("click", toggleTheme);
  $("alerts-btn").addEventListener("click", () => navigate("notifications"));
  $("auth-login-tab").addEventListener("click", () => setAuthMode("login"));
  $("auth-signup-tab").addEventListener("click", () => setAuthMode("signup"));
  $("auth-form").addEventListener("submit", submitAccount);
  $("onboarding-form").addEventListener("submit", submitOnboarding);
  $("edit-zones-profile-btn").addEventListener("click", () => navigate("profile"));
  $("tank-settings-form").addEventListener("submit", saveTankSettings);
  $("add-onboarding-garden").addEventListener("click", () => addOnboardingGarden());
  $("profile-btn").addEventListener("click", () => navigate("profile"));
  $("sidebar-logout-btn").addEventListener("click", logout);
  $("profile-logout-btn").addEventListener("click", logout);
  $("active-garden-select").addEventListener("change", event => changeActiveGarden(event.target.value));

  // Zone all-on / all-off
  $("btn-all-on").addEventListener("click",  () => toggleAllZones(true));
  $("btn-all-off").addEventListener("click", () => toggleAllZones(false));

  // Refill button (dashboard card)
  $("refill-btn").addEventListener("click", refillTank);

  // ── Mode toggle (dashboard banner) ──
  $("mode-checkbox").addEventListener("change", function () {
    setMode(this.checked);
  });

  // ── Mode toggle (settings page) ──
  $("mode-checkbox-settings").addEventListener("change", function () {
    setMode(this.checked);
    // keep both in sync
    $("mode-checkbox").checked = this.checked;
  });

  // Restore GPS mode button
  $("restore-gps-mode-btn").addEventListener("click", restoreGpsMode);

  // Clear alerts
  $("clear-notifs-btn").addEventListener("click", () => {
    state.notifications = [];
    renderNotifList();
    updateBadge();
  });

  // ── Location tabs ──
  $$(".loc-tab").forEach(tab => {
    tab.addEventListener("click", () => {
      $$(".loc-tab").forEach(t => t.classList.remove("active"));
      $$(".loc-tab-panel").forEach(p => p.classList.remove("active"));
      tab.classList.add("active");
      $(`tab-${tab.dataset.tab}`)?.classList.add("active");
    });
  });

  // ── Tab: Postcode ──
  $("postcode-input").addEventListener("input", e => {
    e.target.value = e.target.value.toUpperCase();
  });
  $("postcode-input").addEventListener("keydown", e => {
    if (e.key === "Enter") lookupPostcode($("postcode-input").value);
  });
  $("set-postcode-btn").addEventListener("click", () => {
    lookupPostcode($("postcode-input").value);
  });

  // ── Tab: Lat / Lon ──
  $("set-latlon-btn").addEventListener("click", () => {
    const lat = parseFloat($("manual-lat").value);
    const lon = parseFloat($("manual-lon").value);
    const fb  = $("latlon-result");
    if (isNaN(lat) || isNaN(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) {
      fb.className = "loc-feedback error";
      fb.textContent = "❌ Enter valid latitude (−90 to 90) and longitude (−180 to 180)";
      return;
    }
    fb.className = "loc-feedback loading";
    fb.textContent = "Setting home…";
    setHomeLocation(lat, lon, `${lat.toFixed(5)}, ${lon.toFixed(5)}`);
    fb.className = "loc-feedback ok";
    fb.textContent = `✅ Home set to ${lat.toFixed(5)}, ${lon.toFixed(5)}`;
  });
  // Allow Enter key in either lat/lon field
  ["manual-lat", "manual-lon"].forEach(id => {
    $(id).addEventListener("keydown", e => {
      if (e.key === "Enter") $("set-latlon-btn").click();
    });
  });

  // ── Tab: GPS — Set current position as home ──
  $("sett-set-home-btn").addEventListener("click", () => {
    const fb = $("gps-home-result");
    if (state.gpsLat != null) {
      setHomeLocation(state.gpsLat, state.gpsLon,
        `${state.gpsLat.toFixed(5)}, ${state.gpsLon.toFixed(5)}`);
      fb.className = "loc-feedback ok";
      fb.textContent = `✅ Home set to ${state.gpsLat.toFixed(5)}, ${state.gpsLon.toFixed(5)}`;
    } else {
      fb.className = "loc-feedback error";
      fb.textContent = "❌ GPS not active yet — enable it first";
      showToast("📍 GPS not active yet — enable it first", "warning");
    }
  });

  // ── GPS enable button (tab + modal) ──
  $("sett-gps-btn").addEventListener("click", () => {
    localStorage.setItem("gps_ok", "1");
    startGPS();
    requestNotifPermission();
  });

  $("gps-allow-btn").addEventListener("click", () => {
    $("gps-modal").classList.add("hidden");
    localStorage.setItem("gps_ok", "1");
    startGPS();
    requestNotifPermission();
  });

  $("gps-skip-btn").addEventListener("click", () => {
    $("gps-modal").classList.add("hidden");
    showToast("📍 GPS is off. Enable it anytime in Settings → Garden Location.", "info");
  });

  // ── Notification permission ──
  $("sett-notif-btn").addEventListener("click", requestNotifPermission);

  // ── Restore GPS mode button ──
  $("restore-gps-mode-btn").addEventListener("click", restoreGpsMode);

  bootstrapAccount();
});
