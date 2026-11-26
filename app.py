"""
Flask + SocketIO backend for Smart Irrigation System.
Run: python app.py
"""

import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from dotenv import load_dotenv

from flask import Flask, render_template, jsonify, request, session
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import requests
from werkzeug.security import check_password_hash, generate_password_hash

from account_store import AccountStore, DEFAULT_ZONES
from sensors import SensorHub
from weather import WeatherService
from irrigation_controller import IrrigationController

load_dotenv()

# ─── App setup ──────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "irrigate-dev-secret")
allowed_origins = [
    origin.strip().rstrip("/")
    for origin in os.getenv(
        "ALLOWED_ORIGINS",
        "https://irrigate-v3dw.onrender.com,https://adarshmalayath.github.io,http://localhost:5000,http://127.0.0.1:5000",
    ).split(",")
    if origin.strip()
]
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="None" if os.getenv("RENDER") else "Lax",
    SESSION_COOKIE_SECURE=bool(os.getenv("RENDER")),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)
CORS(app, origins=allowed_origins, supports_credentials=True)
# The public Render service uses Socket.IO polling with a threaded Gunicorn
# worker. Avoid gevent's late SSL monkey-patching, which can recurse inside
# requests when the OpenWeather HTTPS endpoints are called.
socketio = SocketIO(
    app,
    cors_allowed_origins=allowed_origins,
    cors_credentials=True,
    async_mode="threading",
)

# ─── Service singletons ─────────────────────────────
weather_service = WeatherService()

# Zones used until a garden with its own zones is activated.
ZONES = [name for name, _ in DEFAULT_ZONES]
MAX_ZONES_PER_GARDEN = 8
MAX_ZONE_NAME_LENGTH = 40

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
_last_rain_observation = None

sensor_hub = SensorHub()
controller = IrrigationController(zones=ZONES)
account_store = AccountStore(os.getenv("DATABASE_PATH", os.path.join(app.instance_path, "irrigate.sqlite3")))

PLANT_TYPES = ("Vegetables", "Herbs", "Flowers", "Lawn", "Fruit trees", "Mixed garden", "Other")
SOIL_TYPES = ("Loamy", "Clay", "Sandy", "Silty", "Peaty", "Chalky", "Other")


def _logged_in_user_id():
    try:
        return int(session.get("user_id"))
    except (TypeError, ValueError):
        return None


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user_id = _logged_in_user_id()
        if user_id is None or account_store.get_user(user_id) is None:
            return jsonify({"ok": False, "error": "Please log in to continue.", "auth_required": True}), 401
        return fn(*args, **kwargs)
    return wrapped


def _user_profile(user_id):
    user = account_store.get_user(user_id)
    if user is None:
        return None
    gardens = account_store.list_gardens(user_id)
    active_id = session.get("active_garden_id")
    active = next((g for g in gardens if g["id"] == active_id), None)
    if active is None:
        active = next((g for g in gardens if g["is_primary"]), gardens[0] if gardens else None)
        if active:
            session["active_garden_id"] = active["id"]
    tank_settings = account_store.get_tank_settings(user_id) or sensor_hub.tank.get_settings()
    return {
        **user,
        "onboarding_complete": bool(user["onboarding_complete"]),
        "gardens": gardens,
        "active_garden_id": active["id"] if active else None,
        "tank_settings": tank_settings,
    }


def _activate_garden(garden):
    """Apply the selected account garden to the single connected irrigation controller."""
    global _home_lat, _home_lon, _home_label, _home_postcode
    with _lock:
        _home_lat = float(garden["latitude"])
        _home_lon = float(garden["longitude"])
        _home_label = garden["location_label"] or garden["name"]
        _home_postcode = garden.get("postcode", "")
    zones = garden.get("zones") or [{"name": n, "sheltered": s} for n, s in DEFAULT_ZONES]
    names = [zone["name"] for zone in zones]
    sensor_hub.set_zones(names)
    controller.set_zones(
        names,
        [zone["name"] for zone in zones if zone.get("sheltered")],
        {zone["name"]: (zone.get("plant_type"), zone.get("soil_type")) for zone in zones},
    )
    controller.set_home(_home_lat, _home_lon)


def _clean_zones(raw_zones, garden_name):
    """Validate a garden's zone list. Returns (zones, error_message)."""
    if not isinstance(raw_zones, list) or not 1 <= len(raw_zones) <= MAX_ZONES_PER_GARDEN:
        return None, f"Give {garden_name} between 1 and {MAX_ZONES_PER_GARDEN} zones."
    cleaned, seen = [], set()
    for zone in raw_zones:
        if not isinstance(zone, dict) or not isinstance(zone.get("name"), str):
            return None, f"Each zone in {garden_name} needs a name."
        name = " ".join(zone["name"].split())
        if not name or len(name) > MAX_ZONE_NAME_LENGTH:
            return None, f"Zone names in {garden_name} must be 1 to {MAX_ZONE_NAME_LENGTH} characters."
        if name.casefold() in seen:
            return None, f"{garden_name} has two zones called “{name}”. Use a different name for each."
        seen.add(name.casefold())
        plant = zone.get("plant_type") or None
        soil = zone.get("soil_type") or None
        if plant is not None and plant not in PLANT_TYPES:
            return None, f"Choose a valid plant type for “{name}”."
        if soil is not None and soil not in SOIL_TYPES:
            return None, f"Choose a valid soil type for “{name}”."
        cleaned.append({
            "name": name,
            "sheltered": bool(zone.get("sheltered", False)),
            "plant_type": plant,
            "soil_type": soil,
        })
    return cleaned, None


def _activate_session_garden(user_id):
    profile = _user_profile(user_id)
    if not profile or not profile["onboarding_complete"]:
        return None
    sensor_hub.tank.configure(profile.get("tank_settings", {}))
    garden = next((g for g in profile["gardens"] if g["id"] == profile["active_garden_id"]), None)
    if garden:
        _activate_garden(garden)
    return garden


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


def _collect_rainwater_from_weather(weather):
    """Estimate roof runoff once per new OpenWeather observation, not each socket tick."""
    global _last_rain_observation
    if not weather.get("available") or not weather.get("rainfall_available"):
        return 0.0
    try:
        observed_at = datetime.fromisoformat(str(weather["timestamp"]).replace("Z", "+00:00")).timestamp()
        rainfall_mm_1h = max(0.0, float(weather.get("rainfall_mm_1h") or 0))
    except (KeyError, TypeError, ValueError):
        return 0.0
    previous = _last_rain_observation
    _last_rain_observation = (observed_at, rainfall_mm_1h)
    if previous is None:
        return 0.0
    elapsed_seconds = min(1800.0, observed_at - previous[0])
    if elapsed_seconds <= 0 or not weather.get("is_raining_now", weather.get("is_rainy")) or rainfall_mm_1h <= 0:
        return 0.0
    # OpenWeather's current endpoint gives the measured rain total over the
    # previous hour. Treat that as an average hourly rate for the interval
    # between fresh observations; this is an estimate of roof runoff.
    rainfall_since_previous_mm = rainfall_mm_1h * elapsed_seconds / 3600
    return sensor_hub.tank.collect_rainwater(rainfall_since_previous_mm)

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
            _collect_rainwater_from_weather(weather)
            # When the weather feed confirms dry conditions, simulate a mains
            # top-up at 20%; while raining, let the roof catchment fill the tank.
            rain_status = (
                bool(weather.get("is_raining_now", weather.get("is_rainy")))
                if weather.get("available") and weather.get("rainfall_available")
                else None
            )
            sensor_hub.tank.auto_refill_if_low(rain_status)
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


@app.route("/api/auth/signup", methods=["POST"])
def api_signup():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    if not name or len(name) > 80:
        return jsonify({"ok": False, "error": "Enter your name (up to 80 characters)."}), 400
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return jsonify({"ok": False, "error": "Enter a valid email address."}), 400
    if len(password) < 8 or len(password) > 128:
        return jsonify({"ok": False, "error": "Use a password between 8 and 128 characters."}), 400
    try:
        user_id = account_store.create_user(email, name, generate_password_hash(password))
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "An account with that email already exists. Log in instead."}), 409
    session.clear()
    session.permanent = True
    session["user_id"] = user_id
    return jsonify({"ok": True, "user": _user_profile(user_id), "needs_onboarding": True})


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    user = account_store.get_user_by_email(email)
    if user is None or not check_password_hash(user["password_hash"], password):
        return jsonify({"ok": False, "error": "Email or password is incorrect."}), 401
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    profile = _user_profile(user["id"])
    _activate_session_garden(user["id"])
    return jsonify({"ok": True, "user": profile, "needs_onboarding": not profile["onboarding_complete"]})


@app.route("/api/auth/me")
@login_required
def api_auth_me():
    user_id = _logged_in_user_id()
    profile = _user_profile(user_id)
    if profile["onboarding_complete"]:
        _activate_session_garden(user_id)
    return jsonify({"ok": True, "user": profile, "needs_onboarding": not profile["onboarding_complete"]})


@app.route("/api/auth/logout", methods=["POST"])
@login_required
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/onboarding", methods=["POST"])
@login_required
def api_complete_onboarding():
    data = request.get_json(silent=True) or {}
    plant_type = str(data.get("plant_type", "")).strip()
    soil_type = str(data.get("soil_type", "")).strip()
    gardens = data.get("gardens")
    if plant_type not in PLANT_TYPES:
        return jsonify({"ok": False, "error": "Choose a plant type."}), 400
    if soil_type not in SOIL_TYPES:
        return jsonify({"ok": False, "error": "Choose a soil type."}), 400
    if not isinstance(gardens, list) or not 1 <= len(gardens) <= 10:
        return jsonify({"ok": False, "error": "Add between 1 and 10 garden locations."}), 400
    cleaned = []
    for index, garden in enumerate(gardens):
        if not isinstance(garden, dict):
            return jsonify({"ok": False, "error": f"Garden {index + 1} is incomplete."}), 400
        name = str(garden.get("name", "")).strip()
        label = str(garden.get("location_label", "")).strip()
        postcode = str(garden.get("postcode", "")).strip().upper()[:16]
        try:
            lat = float(garden.get("latitude"))
            lon = float(garden.get("longitude"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": f"Find a location for {name or f'Garden {index + 1}'}."}), 400
        if not name or len(name) > 80 or not label or len(label) > 120:
            return jsonify({"ok": False, "error": f"Enter a name and location for Garden {index + 1}."}), 400
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return jsonify({"ok": False, "error": f"The coordinates for {name} are invalid."}), 400
        if garden.get("zones") is None:
            zones = [{"name": n, "sheltered": s} for n, s in DEFAULT_ZONES]
        else:
            zones, error = _clean_zones(garden.get("zones"), name)
            if error:
                return jsonify({"ok": False, "error": error}), 400
        cleaned.append({
            "zones": zones,
            "name": name,
            "location_label": label,
            "latitude": lat,
            "longitude": lon,
            "postcode": postcode,
        })
    user_id = _logged_in_user_id()
    saved_gardens = account_store.save_onboarding(user_id, plant_type, soil_type, cleaned)
    if saved_gardens:
        session["active_garden_id"] = saved_gardens[0]["id"]
        _activate_garden(saved_gardens[0])
    profile = _user_profile(user_id)
    return jsonify({"ok": True, "user": profile, "needs_onboarding": False})


@app.route("/api/gardens/active", methods=["POST"])
@login_required
def api_set_active_garden():
    data = request.get_json(silent=True) or {}
    try:
        garden_id = int(data.get("garden_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Choose a valid garden."}), 400
    user_id = _logged_in_user_id()
    garden = account_store.get_garden(user_id, garden_id)
    if garden is None:
        return jsonify({"ok": False, "error": "That garden was not found on this account."}), 404
    session["active_garden_id"] = garden_id
    _activate_garden(garden)
    return jsonify({"ok": True, "garden": garden, "user": _user_profile(user_id)})


@app.route("/api/status")
@login_required
def api_status():
    user_id = _logged_in_user_id()
    _activate_session_garden(user_id)
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
@login_required
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
@login_required
def api_zone(zone_name):
    data = request.json or {}
    state = bool(data.get("on", False))
    result = controller.toggle_zone(zone_name, state)
    return jsonify(result)


@app.route("/api/zones/all", methods=["POST"])
@login_required
def api_all_zones():
    data = request.json or {}
    state = bool(data.get("on", False))
    controller.toggle_all_zones(state)
    return jsonify({"ok": True})


@app.route("/api/gardens/<int:garden_id>/zones", methods=["POST"])
@login_required
def api_save_garden_zones(garden_id):
    data = request.get_json(silent=True) or {}
    user_id = _logged_in_user_id()
    garden = account_store.get_garden(user_id, garden_id)
    if garden is None:
        return jsonify({"ok": False, "error": "That garden was not found on this account."}), 404
    zones, error = _clean_zones(data.get("zones"), garden["name"])
    if error:
        return jsonify({"ok": False, "error": error}), 400
    garden = account_store.save_garden_zones(user_id, garden_id, zones)
    if session.get("active_garden_id") == garden_id:
        _activate_garden(garden)
    return jsonify({"ok": True, "garden": garden, "user": _user_profile(user_id)})


@app.route("/api/tank/refill", methods=["POST"])
@login_required
def api_refill():
    sensor_hub.tank.refill_from_mains(100)
    return jsonify({"ok": True, "tank": sensor_hub.tank.to_dict()})


@app.route("/api/tank/settings", methods=["POST"])
@login_required
def api_tank_settings():
    data = request.get_json(silent=True) or {}
    supplier = str(data.get("water_supplier", "")).strip()
    if len(supplier) > 80:
        return jsonify({"ok": False, "error": "Water supplier name must be 80 characters or fewer."}), 400
    try:
        catchment_area_m2 = float(data.get("catchment_area_m2"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Enter a valid roof catchment area."}), 400
    if not 1 <= catchment_area_m2 <= 1000:
        return jsonify({"ok": False, "error": "Catchment area must be between 1 and 1000 m²."}), 400

    raw_rate = data.get("water_tariff_gbp_per_m3")
    if raw_rate in (None, ""):
        tariff = None
    else:
        try:
            tariff = float(raw_rate)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Enter a valid water tariff in £ per m³."}), 400
        if not 0 <= tariff <= 100:
            return jsonify({"ok": False, "error": "Water tariff must be between £0 and £100 per m³."}), 400

    user_id = _logged_in_user_id()
    current = sensor_hub.tank.get_settings()
    settings = {
        "water_supplier": supplier,
        "catchment_area_m2": catchment_area_m2,
        "collection_efficiency": current["collection_efficiency"],
        "water_tariff_gbp_per_m3": tariff,
    }
    account_store.save_tank_settings(user_id, settings)
    sensor_hub.tank.configure(settings)
    return jsonify({"ok": True, "user": _user_profile(user_id), "tank": sensor_hub.tank.to_dict()})


@app.route("/api/set_home", methods=["POST"])
@login_required
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
@login_required
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
    user_id = _logged_in_user_id()
    user = account_store.get_user(user_id) if user_id is not None else None
    if user is None or not user["onboarding_complete"]:
        return False
    _activate_session_garden(user_id)
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
    if _logged_in_user_id() is None:
        return
    lat = float(data["lat"])
    lon = float(data["lon"])
    controller.update_user_location(lat, lon)
    # Device GPS is only used by the geofence; garden weather stays anchored to
    # the separately configured home coordinates.


@socketio.on("zone_toggle")
def on_zone_toggle(data):
    if _logged_in_user_id() is None:
        return
    zone = data.get("zone", "")
    state = bool(data.get("on", False))
    controller.toggle_zone(zone, state)


@socketio.on("all_zones_toggle")
def on_all_zones_toggle(data):
    if _logged_in_user_id() is None:
        return
    state = bool(data.get("on", False))
    controller.toggle_all_zones(state)


@socketio.on("set_home")
def on_set_home(data):
    if _logged_in_user_id() is None:
        return
    global _home_lat, _home_lon, _home_label
    lat = float(data.get("lat", _home_lat or 0))
    lon = float(data.get("lon", _home_lon or 0))
    label = data.get("label", f"{lat:.4f}, {lon:.4f}")
    with _lock:
        _home_lat, _home_lon, _home_label = lat, lon, label
    controller.set_home(lat, lon)


@socketio.on("set_mode")
def on_set_mode(data):
    if _logged_in_user_id() is None:
        return
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
