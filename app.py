"""
Flask + SocketIO backend for Smart Irrigation System.
Run: python app.py
"""

import os
import threading
import time
from datetime import datetime, timezone
from dotenv import load_dotenv

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
from flask_cors import CORS

from sensors import SensorHub
from weather import WeatherService
from irrigation_controller import IrrigationController

load_dotenv()

# ─── App setup ──────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "irrigate-dev-secret")
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

# ─── Service singletons ─────────────────────────────
OPENWEATHER_KEY = os.getenv("OPENWEATHER_API_KEY", "")
weather_service = WeatherService(api_key=OPENWEATHER_KEY)

ZONES = ["Front Garden", "Back Garden", "Side Bed", "Greenhouse"]

# Default location — loaded from environment variables only (never hardcoded)
# Set HOME_LAT, HOME_LON, HOME_POSTCODE, HOME_LABEL in your .env or Render dashboard
_current_lat   = float(os.getenv("HOME_LAT", 0.0))
_current_lon   = float(os.getenv("HOME_LON", 0.0))
_home_postcode = os.getenv("HOME_POSTCODE", "")
_home_label    = os.getenv("HOME_LABEL", "My Garden")
_weather_cache: dict = {}
_lock = threading.Lock()

sensor_hub = SensorHub()
controller = IrrigationController(zones=ZONES)

# Pre-seed home location from .env so it's ready before any GPS fix arrives
controller.set_home(_current_lat, _current_lon)
print(f"   📍 Home: {_home_label} ({_home_postcode}) — {_current_lat}, {_current_lon}")


# ────────────────────────────────────────────────────
#  Background tick
# ────────────────────────────────────────────────────

def _background_tick():
    """Runs every 3 seconds to update sensors, evaluate irrigation, push state."""
    global _current_lat, _current_lon, _weather_cache

    while True:
        try:
            with _lock:
                lat, lon = _current_lat, _current_lon

            # 1. Fetch weather (cached 10 min)
            weather = weather_service.get_weather(lat, lon)
            temp = weather.get("temperature", 20)

            # 2. Update sensors
            active = controller.active_zones
            sensor_hub.update_all(temperature=temp, active_zones=active)
            snap = sensor_hub.get_snapshot()

            # 3. Evaluate irrigation
            decision = controller.evaluate(weather, snap)

            # 4. Compose and broadcast full state
            payload = {
                "weather": weather,
                "sensors": snap,
                "controller": controller.get_status(),
                "decision": {
                    "should_irrigate": decision.should_irrigate,
                    "zones": decision.zones_to_irrigate,
                    "reason": decision.reason,
                    "mode": decision.mode,
                    "priority": decision.priority,
                    "skip_reason": decision.skip_reason,
                },
                "notifications": decision.notifications,
                "server_time": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
            }

            socketio.emit("state_update", payload)

            # Push individual notifications as separate events (for toast / Web Push)
            for notif in decision.notifications:
                socketio.emit("notification", notif)

        except Exception as exc:
            print(f"[tick error] {exc}")

        time.sleep(3)


# ────────────────────────────────────────────────────
#  HTTP routes
# ────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    with _lock:
        lat, lon = _current_lat, _current_lon
    weather = weather_service.get_weather(lat, lon)
    snap = sensor_hub.get_snapshot()
    return jsonify({
        "weather": weather,
        "sensors": snap,
        "controller": controller.get_status(),
    })


@app.route("/api/mode", methods=["POST"])
def api_set_mode():
    data = request.json or {}
    mode = data.get("mode", "manual")
    use_gps = bool(data.get("use_gps", False))
    if use_gps:
        controller.unlock_mode()
    else:
        controller.set_mode(mode)
    return jsonify({"ok": True, "mode": controller.mode,
                    "mode_locked": controller._mode_locked})


@app.route("/api/zone/<zone_name>", methods=["POST"])
def api_zone(zone_name):
    data = request.json or {}
    state = bool(data.get("on", False))
    result = controller.toggle_zone(zone_name, state)
    return jsonify(result)


@app.route("/api/zones/all", methods=["POST"])
def api_all_zones():
    data = request.json or {}
    state = bool(data.get("on", False))
    controller.toggle_all_zones(state)
    return jsonify({"ok": True})


@app.route("/api/tank/refill", methods=["POST"])
def api_refill():
    sensor_hub.tank.refill(100)
    return jsonify({"ok": True, "level": sensor_hub.tank.level})


@app.route("/api/set_home", methods=["POST"])
def api_set_home():
    data = request.json or {}
    lat = float(data.get("lat", _current_lat))
    lon = float(data.get("lon", _current_lon))
    controller.set_home(lat, lon)
    return jsonify({"ok": True, "home_lat": lat, "home_lon": lon})


@app.route("/api/postcode", methods=["GET"])
def api_postcode():
    """Resolve a UK postcode to lat/lon using postcodes.io (free, no key needed)."""
    pc = request.args.get("q", "").replace(" ", "").upper()
    if not pc:
        return jsonify({"ok": False, "error": "No postcode provided"}), 400
    try:
        r = requests.get(f"https://api.postcodes.io/postcodes/{pc}", timeout=8)
        d = r.json()
        if d.get("status") == 200:
            res = d["result"]
            return jsonify({
                "ok":       True,
                "postcode": res["postcode"],
                "lat":      res["latitude"],
                "lon":      res["longitude"],
                "label":    f"{res.get('admin_ward','')}, {res.get('admin_district','')}",
            })
        return jsonify({"ok": False, "error": d.get("error", "Invalid postcode")}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ────────────────────────────────────────────────────
#  WebSocket events
# ────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    print(f"[WS] Client connected: {request.sid}")
    # Send current status immediately
    with _lock:
        lat, lon = _current_lat, _current_lon
    weather = weather_service.get_weather(lat, lon)
    snap = sensor_hub.get_snapshot()
    emit("state_update", {
        "weather": weather,
        "sensors": snap,
        "controller": controller.get_status(),
        "server_time": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
    })


@socketio.on("disconnect")
def on_disconnect():
    print(f"[WS] Client disconnected: {request.sid}")


@socketio.on("location_update")
def on_location(data):
    global _current_lat, _current_lon
    lat = float(data.get("lat", _current_lat))
    lon = float(data.get("lon", _current_lon))
    with _lock:
        _current_lat = lat
        _current_lon = lon
    controller.update_user_location(lat, lon)
    # Invalidate weather cache so next tick fetches fresh data
    weather_service._cache = None


@socketio.on("zone_toggle")
def on_zone_toggle(data):
    zone = data.get("zone", "")
    state = bool(data.get("on", False))
    controller.toggle_zone(zone, state)


@socketio.on("all_zones_toggle")
def on_all_zones_toggle(data):
    state = bool(data.get("on", False))
    controller.toggle_all_zones(state)


@socketio.on("set_home")
def on_set_home(data):
    lat = float(data.get("lat", _current_lat))
    lon = float(data.get("lon", _current_lon))
    label = data.get("label", f"{lat:.4f}, {lon:.4f}")
    controller.set_home(lat, lon)
    # Invalidate weather cache for new location
    weather_service._cache = None


@socketio.on("set_mode")
def on_set_mode(data):
    mode = data.get("mode", "manual")
    use_gps = bool(data.get("use_gps", False))
    if use_gps:
        controller.unlock_mode()
    else:
        controller.set_mode(mode)


# ────────────────────────────────────────────────────
#  Entry point
# ────────────────────────────────────────────────────

if __name__ == "__main__":
    # Start background tick thread
    tick_thread = threading.Thread(target=_background_tick, daemon=True)
    tick_thread.start()

    port = int(os.getenv("PORT", 5000))
    print(f"\n🌿 Irrigate — Smart Garden Irrigation System")
    print(f"   Running at http://localhost:{port}")
    print(f"   OpenWeather key: {'✅ set' if OPENWEATHER_KEY else '⚠️  not set (using mock data)'}\n")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
