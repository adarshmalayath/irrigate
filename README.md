# 🌿 Irrigate — Smart Garden Irrigation System

A responsive Python web application for intelligent home garden irrigation.

## Features
- 🌡️ **Live weather** from OpenWeather for the configured garden location
- 📡 **Mock sensors** for soil moisture (4 zones) and water tank level
- 📍 **GPS geofencing** — device GPS tracks distance from the garden; weather stays at the garden
- 🤖 **Smart decisions** — skips irrigation during rain, high wind, or midday heat
- 🔔 **Real-time push notifications** for every irrigation event
- 💧 **Per-zone control** with live animated dashboards
- 🌙 **Dark/Light theme** toggle

## Quick Start

```bash
# 1. Install dependencies
pip3 install -r requirements.txt

# 2. Add OPENWEATHER_API_KEY and your garden coordinates to .env

# 3. Run
PORT=5001 python3 app.py
```

Open **http://localhost:5001** in your browser.
Set the garden postcode or choose **Set Current as Home** in Settings. The browser's current GPS position is used for geofencing, while weather always follows the saved garden location.

## Public site

The Render service hosts the Flask backend. GitHub Pages hosts a static copy of the dashboard and connects it to that backend:

- Render backend: <https://irrigate-v3dw.onrender.com>
- GitHub Pages site: <https://adarshmalayath.github.io/irrigate/>

The ready-to-publish static files are in `docs/`. In the repository's **Settings → Pages**, choose **Deploy from a branch**, then select `main` and `/docs`. Rebuild `docs/` after UI changes with `python3 scripts/build_pages.py --output docs --api-base https://irrigate-v3dw.onrender.com`.

## Project Structure
```
Irrigate/
├── app.py                  # Flask + SocketIO server
├── sensors.py              # Mock soil & tank sensors
├── weather.py              # Coordinate-based OpenWeather client
├── irrigation_controller.py # Decision engine + geofence
├── requirements.txt
├── .env                    # API keys (never commit!)
├── templates/index.html    # Main UI
└── static/
    ├── css/style.css
    └── js/app.js
```

## Logic Rules
| Condition | Action |
|-----------|--------|
| Soil moisture < 40% | Start irrigation (auto mode) |
| Soil moisture > 70% | Stop irrigation |
| Rain detected | Pause all irrigation |
| Wind > 7 m/s | Skip (drift prevention) |
| Temp > 30°C & 10am–4pm | Defer to cooler hours |
| Tank < 10% | Emergency stop |
| User > 1 mi from home | Switch to Auto mode |
| User < 1 mi from home | Switch to Manual mode |
