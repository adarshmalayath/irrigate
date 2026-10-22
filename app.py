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
import requests

from sensors import SensorHub
from weather import WeatherService
from irrigation_controller import IrrigationController

load_dotenv()

# ─── App setup ──────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "irrigate-dev-secret")
CORS(app)
# The public Render service uses Socket.IO polling with a threaded Gunicorn
# worker. Avoid gevent's late SSL monkey-patching, which can recurse inside
# requests when the OpenWeather HTTPS endpoints are called.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ─── Service singletons ─────────────────────────────
weather_service = WeatherService()

ZONES = ["Front Garden", "Back Garden", "Side Bed", "Greenhouse"]

# Default location — loaded from environment variables only (never hardcoded)
# Set HOME_LAT, HOME_LON, HOME_POSTCODE, HOME_LABEL in your .env or Render dashboard
def _env_coordinate(name):
    value = os.getenv(name, "").strip()
    return float(value) if value else None


_home_lat      = _env_coordinate("HOME_LAT")
_home_lon      = _env_coordinate("HOME_LON")
_home_postcode = os.getenv("HOME_POSTCODE", "")
_home_label    = os.getenv("HOME_LABEL", "My Garden")
_lock = threading.Lock()
_tick_start_lock = threading.Lock()
_tick_started = False

sensor_hub = SensorHub()
controller = IrrigationController(zones=ZONES)


def _controller_status():
    status = controller.get_status()
    status["home_label"] = _garden_label(_home_lat, _home_lon)
    return status


def _garden_label(lat, lon):
    if _home_label and _home_label.strip().lower() not in {"garden", "my garden"}:
        return _home_label
    if lat is not None and lon is not None:
        return f"Garden GPS {lat:.5f}, {lon:.5f}"
    return "Garden location not set"


def _get_garden_weather(lat, lon):
    weather = weather_service.get_weather(lat, lon)
    if lat is not None and lon is not None:
        weather = weather.copy()
        weather["city"] = _garden_label(lat, lon)
    return weather

# Garden weather and geofencing use the configured garden point. Device GPS is
# tracked separately and only supplies the user's current position.
if _home_lat is not None and _home_lon is not None:
    controller.set_home(_home_lat, _home_lon)
    print(f"   📍 Garden: {_home_label} ({_home_postcode}) — {_home_lat}, {_home_lon}")
else:
    print("   📍 Garden location not set; choose a postcode or set current GPS as home.")


# ────────────────────────────────────────────────────
#  Background tick
# ────────────────────────────────────────────────────

def _background_tick():
    """Runs every 3 seconds to update sensors, evaluate irrigation, push state."""
    while True:
        try:
            with _lock:
                lat, lon = _home_lat, _home_lon

            # 1. Fetch weather (cached 10 min)
            weather = _get_garden_weather(lat, lon)
            temp = weather.get("temperature", 20)
            if temp is None:
                temp = 20  # Keep simulated sensors ticking while real weather is unavailable.

            # 2. Update sensors
            active = controller.active_zones
            sensor_hub.update_all(temperature=temp, active_zones=active)
            # The current tank is simulated; physical refill requires a connected
            # pump/valve controller. Refill the model once it reaches 20%.
            sensor_hub.tank.auto_refill_if_low()
            snap = sensor_hub.get_snapshot()

            # 3. Evaluate irrigation
            decision = controller.evaluate(weather, snap)

            # 4. Compose and broadcast full state
            payload = {
                "weather": weather,
                "sensors": snap,
                "controller": _controller_status(),
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


def _start_background_tick_once():
    """Start live updates once for this server process."""
    global _tick_started
    with _tick_start_lock:
        if not _tick_started:
            _tick_started = True
            socketio.start_background_task(_background_tick)


# ────────────────────────────────────────────────────
#  HTTP routes
# ────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    with _lock:
        lat, lon = _home_lat, _home_lon
    weather = _get_garden_weather(lat, lon)
    snap = sensor_hub.get_snapshot()
    return jsonify({
        "weather": weather,
        "sensors": snap,
        "controller": _controller_status(),
    })


@app.route("/api/mode", methods=["POST"])
def api_set_mode():
    data = request.json or {}
    mode = data.get("mode", "manual")
    use_gps = bool(data.get("use_gps", False))
    if use_gps:
        controller.unlock_mode(force=bool(data.get("force", False)))
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
    global _home_lat, _home_lon, _home_label
    data = request.json or {}
    lat = float(data.get("lat", _home_lat or 0))
    lon = float(data.get("lon", _home_lon or 0))
    label = data.get("label", f"{lat:.4f}, {lon:.4f}")
    with _lock:
        _home_lat, _home_lon, _home_label = lat, lon, label
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
        lat, lon = _home_lat, _home_lon
    weather = _get_garden_weather(lat, lon)
    snap = sensor_hub.get_snapshot()
    emit("state_update", {
        "weather": weather,
        "sensors": snap,
        "controller": _controller_status(),
        "server_time": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
    })
    # Gunicorn imports this module, so __main__ is not reached in production.
    # Start the periodic sensor/controller updates when the first client connects.
    _start_background_tick_once()


@socketio.on("disconnect")
def on_disconnect():
    print(f"[WS] Client disconnected: {request.sid}")


@socketio.on("location_update")
def on_location(data):
    lat = float(data["lat"])
    lon = float(data["lon"])
    controller.update_user_location(lat, lon)
    # Device GPS is only used by the geofence; garden weather stays anchored to
    # the separately configured home coordinates.


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
    global _home_lat, _home_lon, _home_label
    lat = float(data.get("lat", _home_lat or 0))
    lon = float(data.get("lon", _home_lon or 0))
    label = data.get("label", f"{lat:.4f}, {lon:.4f}")
    with _lock:
        _home_lat, _home_lon, _home_label = lat, lon, label
    controller.set_home(lat, lon)


@socketio.on("set_mode")
def on_set_mode(data):
    mode = data.get("mode", "manual")
    use_gps = bool(data.get("use_gps", False))
    if use_gps:
        controller.unlock_mode(force=bool(data.get("force", False)))
    else:
        controller.set_mode(mode)


# ────────────────────────────────────────────────────
#  Entry point
# ────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"\n🌿 Irrigate — Smart Garden Irrigation System")
    print(f"   Running at http://localhost:{port}")
    print("   Weather provider: OpenWeather (garden coordinates)\n")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
