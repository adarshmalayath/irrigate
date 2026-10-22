/* ═══════════════════════════════════════════════
   Irrigate — app.js
═══════════════════════════════════════════════ */
"use strict";

// ─── State ───────────────────────────────────────
let savedGarden = null;
try {
  const parsed = JSON.parse(localStorage.getItem("garden_location") || "null");
  if (parsed && Number.isFinite(Number(parsed.lat)) && Number.isFinite(Number(parsed.lon))) {
    savedGarden = { lat: Number(parsed.lat), lon: Number(parsed.lon), label: parsed.label || "Garden" };
  }
} catch (_) {
  // Storage can be disabled by the browser; the server's configured garden remains usable.
}

const state = {
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
  homeLat:    savedGarden?.lat ?? null,
  homeLon:    savedGarden?.lon ?? null,
  homeLabel:  savedGarden?.label ?? "Garden location not set",
  modeLocked: false,
};

// ─── DOM helpers ─────────────────────────────────
const $  = id  => document.getElementById(id);
const $$ = sel => document.querySelectorAll(sel);

// ─── Socket ──────────────────────────────────────
const API_BASE = window.IRRIGATE_API_BASE || window.location.origin;
// Render currently starts this service with the gevent worker, which supports
// Socket.IO polling but not WebSocket upgrades. Stay on polling to avoid failed
// upgrade requests and keep live state updates connected.
const socket = io(API_BASE, { transports: ["polling"] });

socket.on("connect", () => {
  state.connected = true;
  updateConnDot(true);
  // GPS can arrive before the socket handshake finishes; replay the latest fix
  // so the backend geofence receives the browser's current position.
  if (state.gpsLat != null && state.gpsLon != null) {
    socket.emit("location_update", { lat: state.gpsLat, lon: state.gpsLon });
  }
  if (savedGarden) {
    socket.emit("set_home", savedGarden);
  }
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
    if (state.homeLat == null && data.controller.home_lat != null && data.controller.home_lon != null) {
      state.homeLat = data.controller.home_lat;
      state.homeLon = data.controller.home_lon;
      state.homeLabel = data.controller.home_label || `Garden ${state.homeLat.toFixed(4)}, ${state.homeLon.toFixed(4)}`;
    }
    // Sync mode-locked flag from server
    state.modeLocked = data.controller.mode_locked || false;
  }
  if (data.decision)   state.decision   = data.decision;
  if (data.notifications?.length) handleNotifications(data.notifications);
  renderAll();
});

socket.on("notification", n => handleNotifications([n]));

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

/* ── Zone grid (dashboard) ── */
function renderZoneCards() {
  const grid       = $("zones-grid");
  const zones      = state.sensors.zones;
  const zoneStates = state.controller.zone_states || {};
  grid.innerHTML   = "";

  for (const [name, data] of Object.entries(zones)) {
    const isOn  = zoneStates[name] || false;
    const m     = data.moisture;
    const mCls  = m < 25 ? "critical" : m < 40 ? "dry" : "";
    const arc   = buildArc(m);

    const card = document.createElement("div");
    card.className = ["zone-card", isOn ? "irrigating" : "", mCls].filter(Boolean).join(" ");
    card.dataset.zone = name;
    card.innerHTML = `
      <div class="zone-header">
        <div class="zone-name">${name}</div>
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

/* ── Irrigation banner ── */
function renderBanner() {
  const d         = state.decision;
  const indicator = $("banner-indicator");
  const active    = d?.zones || [];

  if (active.length) {
    indicator.className  = "banner-indicator irrigating";
    $("banner-title").textContent = `Irrigating ${active.length} zone${active.length > 1 ? "s" : ""}`;
    $("banner-sub").textContent   = [active.join(", "), d?.skip_reason].filter(Boolean).join(" · ");
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
    $("mode-label").textContent = locked ? "Auto (pinned)" : "Auto";
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

  if (dist != null) {
    $("geo-distance").textContent = `${dist.toFixed(1)} mi`;

    if (dist > 50) {
      $("geo-distance").style.color = "var(--info)";
      $("geo-sub").textContent = `Away — Auto mode active (${dist.toFixed(1)} mi from home)`;
    } else {
      $("geo-distance").style.color = "var(--accent)";
      $("geo-sub").textContent = `You're home — ${mode === "auto" ? "Auto" : "Manual"} mode active`;

      // ── KEY FIX: when GPS confirms you're home, automatically restore manual ──
      if (mode === "auto") {
        restoreGpsMode(true);   // clears lock → GPS sets mode to manual (silent)
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
  // GPS fields
  $("sett-gps-status").textContent = state.gpsActive ? "✅ Active" : "⏳ Waiting…";
  $("sett-gps-status").style.color = state.gpsActive ? "var(--success)" : "var(--text-muted)";
  $("sett-lat").textContent = state.gpsLat != null ? state.gpsLat.toFixed(6) : "--";
  $("sett-lon").textContent = state.gpsLon != null ? state.gpsLon.toFixed(6) : "--";
  $("sett-acc").textContent = state.gpsAcc != null ? `±${state.gpsAcc.toFixed(0)} m` : "--";
  $("sett-home").textContent = state.homeLabel;
  const dist = state.controller?.user_distance_miles;
  $("sett-distance").textContent = dist != null ? `${dist.toFixed(2)} mi` : "--";

  // Notification permission
  const perm = "Notification" in window ? Notification.permission : "unsupported";
  const permMap = { granted: "✅ Allowed", denied: "❌ Blocked", default: "⏳ Not asked", unsupported: "Not supported" };
  $("sett-notif-perm").textContent = permMap[perm] || perm;
  $("sett-notif-btn").style.display = perm === "granted" ? "none" : "inline-flex";
}

/* ── Sensor section ── */
function renderSensorSection() {
  const grid = $("sensor-grid");
  const zones = state.sensors.zones;
  const tank  = state.sensors.tank;
  grid.innerHTML = "";

  // Tank
  const tankCard = document.createElement("div");
  tankCard.className = "detail-card";
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
    <br/><button class="btn btn-sm btn-outline" onclick="refillTank()">↺ Refill Tank</button>`;
  grid.appendChild(tankCard);

  for (const [name, data] of Object.entries(zones)) {
    const cl = data.moisture < 25 ? "critical" : data.moisture < 40 ? "low" : "";
    const card = document.createElement("div");
    card.className = "detail-card";
    card.innerHTML = `
      <div class="detail-card-title">🌱 ${name}</div>
      <div class="progress-wrap" style="margin-bottom:6px">
        <div class="progress"><div class="progress-fill ${cl}" style="width:${data.moisture}%"></div></div>
        <div class="progress-pct">${data.moisture}%</div>
      </div>
      <div class="detail-row"><span class="detail-key">Status</span><span class="detail-val">${data.status}</span></div>
      <div class="detail-row"><span class="detail-key">Irrigating</span><span class="detail-val">${data.is_irrigating ? "✅ Yes" : "❌ No"}</span></div>
      <div class="detail-row"><span class="detail-key">Needs water</span><span class="detail-val">${data.needs_water ? "⚠️ Yes" : "✅ No"}</span></div>`;
    grid.appendChild(card);
  }
}

/* ── Zone detail section ── */
function renderZoneDetailSection() {
  const grid       = $("zones-detail-grid");
  const zones      = state.sensors.zones;
  const zoneStates = state.controller.zone_states || {};
  grid.innerHTML   = "";

  for (const [name, data] of Object.entries(zones)) {
    const isOn = zoneStates[name] || false;
    const cl   = data.moisture < 25 ? "critical" : data.moisture < 40 ? "low" : "";
    const card = document.createElement("div");
    card.className = "detail-card";
    card.innerHTML = `
      <div class="detail-card-title">🌿 ${name}</div>
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
  socket.emit("set_mode", { use_gps: true });
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
  fetch(`${API_BASE}/api/tank/refill`, { method: "POST" })
    .then(() => showToast("🚿 Tank refilled to 100%!", "success"));
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
  savedGarden = { lat, lon, label };
  try {
    localStorage.setItem("garden_location", JSON.stringify(savedGarden));
  } catch (_) {
    showToast("Garden location is set for this visit, but the browser could not save it.", "warning");
  }
  socket.emit("set_home", { lat, lon, label });
  showToast(`🏡 Home set to ${label}`, "success");
  $("sett-home").textContent = label;
}

// ─── GPS ─────────────────────────────────────────

let _gpsWatchId = null;

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

    // Send live position to server
    socket.emit("location_update", { lat, lon });

    // On first fix, also seed home if not already set by postcode
    if (!localStorage.getItem("home_set_by_postcode")) {
      // Keep env-configured home; don't overwrite with GPS
    }

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
  $$(".section").forEach(el =>
    el.classList.toggle("active", el.id === `section-${section}`)
  );
  const titles = {
    dashboard: "Dashboard", zones: "Garden Zones",
    weather: "Weather", sensors: "Sensors",
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

  // ── Postcode input (Settings) ──
  // Dynamically inject postcode row into settings location card
  const locBtnRow = $("loc-btn-row");
  if (locBtnRow) {
    const wrap = document.createElement("div");
    wrap.innerHTML = `
      <div style="display:flex;gap:8px;margin-top:4px;flex-wrap:wrap;">
        <input id="postcode-input" type="text" class="postcode-input"
               placeholder="Enter your postcode" maxlength="8"
               autocomplete="off" spellcheck="false"
               style="flex:1;min-width:120px;" />
        <button id="set-postcode-btn" class="btn btn-sm btn-outline">Set Garden 📍</button>
      </div>
      <div id="postcode-result" class="postcode-result" style="margin-top:4px"></div>`;
    locBtnRow.after(wrap);

    $("postcode-input").value = "";  // user enters their own postcode

    $("set-postcode-btn").addEventListener("click", () => {
      lookupPostcode($("postcode-input").value);
    });
    $("postcode-input").addEventListener("keydown", e => {
      if (e.key === "Enter") lookupPostcode($("postcode-input").value);
    });
    $("postcode-input").addEventListener("input", e => {
      e.target.value = e.target.value.toUpperCase();
    });
  }

  // ── Set Current as Home (GPS) ──
  $("sett-set-home-btn").addEventListener("click", () => {
    if (state.gpsLat != null) {
      setHomeLocation(state.gpsLat, state.gpsLon, `${state.gpsLat.toFixed(4)}, ${state.gpsLon.toFixed(4)}`);
      localStorage.setItem("home_set_by_postcode", "1");
    } else {
      showToast("📍 GPS not active yet — please enable location first", "warning");
    }
  });

  // ── GPS buttons ──
  $("sett-gps-btn").addEventListener("click", () => {
    $("gps-modal").classList.add("hidden");
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
    showToast("📍 GPS is off for this visit. Enable it in Settings when ready.", "info");
  });

  // ── Notification permission button (settings) ──
  $("sett-notif-btn").addEventListener("click", requestNotifPermission);

  // ── GPS modal on first load ──
  const gpsChoice = localStorage.getItem("gps_ok");
  if (gpsChoice === "1") {
    startGPS();
    requestNotifPermission();
  } else {
    // Older versions remembered "skip" forever. Ask again next visit so the
    // user can opt into real GPS weather without finding the settings first.
    setTimeout(() => $("gps-modal").classList.remove("hidden"), 700);
  }

  // Seed the home location from default postcode on first render
  $("sett-home").textContent = state.homeLabel;
});
