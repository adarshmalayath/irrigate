# 🌿 Irrigate — Smart Garden Irrigation System

A responsive Python web application for intelligent home garden irrigation.

## Features
- 🌡️ **Live weather** via OpenWeatherMap (temperature, wind speed, UV index)
- 📡 **Mock sensors** for soil moisture (4 zones) and water tank level
- 📍 **GPS geofencing** — auto-mode when >50 miles away, manual when home
- 🤖 **Smart decisions** — skips irrigation during rain, high wind, or midday heat
- 🔔 **Real-time push notifications** for every irrigation event
- 💧 **Per-zone control** with live animated dashboards
- 🌙 **Dark/Light theme** toggle

## Quick Start

```bash
# 1. Install dependencies
pip3 install -r requirements.txt

# 2. Add your OpenWeatherMap key to .env
echo "OPENWEATHER_API_KEY=your_key_here" > .env

# 3. Run
PORT=5001 python3 app.py
```

Open **http://localhost:5001** in your browser.

## Public site

The Render service hosts the Flask backend. GitHub Pages hosts a static copy of the dashboard and connects it to that backend:

- Render backend: <https://irrigate-v3dw.onrender.com>
- GitHub Pages site: <https://adarshmalayath.github.io/irrigate/>

The workflow in `.github/workflows/pages.yml` publishes the site whenever changes are pushed to `main`. It builds the static files from `templates/index.html` and `static/`, so the Flask app and Pages dashboard use the same UI source.

## Project Structure
```
Irrigate/
├── app.py                  # Flask + SocketIO server
├── sensors.py              # Mock soil & tank sensors
├── weather.py              # OpenWeatherMap API client
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
| User > 50 mi from home | Switch to Auto mode |
| User < 50 mi from home | Switch to Manual mode |
