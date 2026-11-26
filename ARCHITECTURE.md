# Irrigate — Architecture

Smart garden irrigation web app: a Flask + Socket.IO backend on Render, and a vanilla-JS dashboard (served by Flask or as a static copy on GitHub Pages).

```mermaid
flowchart LR
    %% ───────────── Clients ─────────────
    subgraph Clients["Clients"]
        Browser["Web browser<br/>templates/index.html + static/js/app.js<br/>(dashboard, settings, auth UI)"]
        DeviceAPIs["Browser / device APIs<br/>Geolocation · Notifications · localStorage"]
    end

    %% ───────────── Hosting ─────────────
    subgraph Hosting["Hosting"]
        Pages["GitHub Pages<br/>docs/ static build<br/>(scripts/build_pages.py injects IRRIGATE_API_BASE)"]
        CDN["socket.io CDN<br/>client library"]
    end

    %% ───────────── Backend ─────────────
    subgraph Render["Render web service (gunicorn, 1 worker, gthread)"]
        subgraph Flask["app.py — Flask + Flask-SocketIO + CORS"]
            REST["REST API<br/>/api/auth/* (signup, login, me, logout)<br/>/api/onboarding · /api/gardens/active<br/>/api/status · /api/mode · /api/zone/*<br/>/api/zones/* · /api/tank/* · /api/set_home<br/>/api/postcode"]
            WS["Socket.IO events<br/>in: location_update, zone_toggle,<br/>all_zones_toggle, set_home, set_mode<br/>out: state_update, notification"]
            Tick["Background tick loop<br/>reads sensors → weather → decisions<br/>→ broadcasts state"]
            Session["Session cookie auth<br/>(werkzeug password hashing)"]
        end
        Controller["irrigation_controller.py<br/>Decision engine + geofence<br/>(moisture, rain, wind, heat, tank rules;<br/>auto/manual by distance from home)"]
        Sensors["sensors.py — SensorHub<br/>Mock soil moisture (4 zones)<br/>+ water tank / rainwater collection"]
        Weather["weather.py — WeatherService<br/>cached weather + UV client"]
        Store["account_store.py — AccountStore"]
    end

    DB[("SQLite<br/>instance/irrigate.sqlite3<br/>users · gardens · zone_labels · tank_settings")]

    %% ───────────── External services ─────────────
    subgraph External["External APIs"]
        OWM["OpenWeather API<br/>current + forecast"]
        OM["Open-Meteo API<br/>UV index"]
        PC["postcodes.io<br/>postcode → lat/lon"]
    end

    %% ───────────── Flows ─────────────
    Pages -- "serves static UI" --> Browser
    Render -- "serves UI at /" --> Browser
    CDN -.-> Browser
    Browser <--> DeviceAPIs

    Browser -- "HTTPS fetch (credentials: include)" --> REST
    Browser <-- "Socket.IO (long-polling)" --> WS
    Browser -- "postcode lookup" --> PC

    REST --> Session
    REST --> Store
    REST --> Controller
    REST -- "/api/postcode" --> PC
    WS --> Controller
    Tick --> Sensors
    Tick --> Weather
    Tick --> Controller
    Tick -- "emit state_update / notification" --> WS
    Controller <--> Sensors
    Store --> DB
    Weather --> OWM
    Weather --> OM
```

## Key data flows

1. **Login / onboarding** — browser → `/api/auth/*`, `/api/onboarding` → `AccountStore` → SQLite. Session cookie (SameSite=None, Secure on Render) authorises later calls, including cross-origin from GitHub Pages.
2. **Live dashboard** — the background tick reads mock sensors, fetches cached garden weather (OpenWeather + Open-Meteo UV), runs the decision engine, then pushes `state_update` and `notification` over Socket.IO.
3. **Geofencing** — the browser's GPS sends `location_update`; the controller switches Auto (> 1 mi from home) / Manual (< 1 mi). Weather always uses the saved garden location.
4. **User control** — zone / mode / tank actions go over Socket.IO events or REST, update controller state, and are broadcast back to all clients.

## Configuration
Env vars: `OPENWEATHER_API_KEY`, `SECRET_KEY`, `HOME_LAT`, `HOME_LON`, `HOME_POSTCODE`, `HOME_LABEL`, `DATABASE_PATH`, `WATER_SUPPLIER`, allowed CORS origins.
