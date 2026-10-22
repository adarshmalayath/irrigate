"""
Core irrigation decision engine for the Smart Irrigation System.
Analyses weather, soil moisture, tank level and user location to decide
when and where to irrigate.

Notifications fire only for:
  1. Water tank level drops beyond a limit  (critical <10%, low <25%)
  2. Sudden weather change                  (condition shift / big temp jump)
  3. Soil moisture too low (<25%) or too high (>85%) per zone

Each alert has a 30-minute cooldown so there is zero spam.
"""

import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple


# ────────────────────────────────────────────────────
#  Constants / Thresholds
# ────────────────────────────────────────────────────
GEOFENCE_RADIUS_MILES   = 50.0
SOIL_DRY_THRESHOLD      = 40.0    # auto-irrigate below this
SOIL_STOP_THRESHOLD     = 70.0    # auto-stop above this
SOIL_ALERT_LOW          = 25.0    # 🔔 alert: critically dry
SOIL_ALERT_HIGH         = 85.0    # 🔔 alert: waterlogged / overwatered
TANK_CRITICAL_PCT       = 10.0    # 🔔 alert + irrigation block
TANK_LOW_PCT            = 25.0    # 🔔 alert (warn, still irrigate)
HIGH_WIND_MS            = 7.0     # skip irrigation, no alert
HOT_TEMP_C              = 30.0    # skip mid-day irrigation, no alert
NIGHT_START_H           = 20
NIGHT_END_H             = 6

ALERT_COOLDOWN_SECS     = 1800    # 30 minutes between repeat alerts
WEATHER_TEMP_JUMP_C     = 5.0     # °C change that counts as "sudden"
RAIN_MANUAL_RUN_SECS    = 10.0    # Seconds a user-requested manual run may continue in rain


# ────────────────────────────────────────────────────
#  Data classes
# ────────────────────────────────────────────────────

@dataclass
class IrrigationDecision:
    should_irrigate:    bool
    zones_to_irrigate:  List[str]
    reason:             str
    mode:               str           # "auto" | "manual"
    priority:           str           # "high" | "medium" | "low" | "none"
    notifications:      List[dict] = field(default_factory=list)
    skip_reason:        Optional[str] = None


# ────────────────────────────────────────────────────
#  Controller
# ────────────────────────────────────────────────────

class IrrigationController:
    """Stateful controller that owns zone on/off states and makes decisions."""

    def __init__(self, zones: List[str]):
        self.zones = zones
        self._zone_states:   Dict[str, bool]  = {z: False for z in zones}
        self._manual_overrides: Dict[str, bool] = {}
        self._mode: str = "manual"
        self._mode_locked: bool = False   # True = user pinned the mode, GPS won't override it
        self._state_lock = threading.RLock()
        self._is_raining = False
        self._rain_manual_until: Optional[float] = None
        self._rain_manual_timer: Optional[threading.Timer] = None
        self._rain_manual_generation = 0

        # Location
        self._user_lat:  Optional[float] = None
        self._user_lon:  Optional[float] = None
        self._home_lat:  Optional[float] = None
        self._home_lon:  Optional[float] = None
        self._user_distance_miles: Optional[float] = None

        # Notification queue (flushed each tick)
        self._notifications: List[dict] = []

        # Cooldown tracker  {alert_tag: last_fired_timestamp}
        self._cooldowns: Dict[str, float] = {}

        # Previous weather snapshot for change-detection
        self._prev_weather: Optional[dict] = None

    # ──────────────── Location ────────────────

    def update_user_location(self, lat: float, lon: float):
        with self._state_lock:
            self._user_lat = lat
            self._user_lon = lon
            if self._home_lat is None or self._home_lon is None:
                self._user_distance_miles = None
                return

            dist = haversine_miles(lat, lon, self._home_lat, self._home_lon)
            self._user_distance_miles = dist

            # GPS only auto-switches mode when the user hasn't manually pinned it
            if not self._mode_locked:
                self._mode = "auto" if dist > GEOFENCE_RADIUS_MILES else "manual"
                if self._mode == "auto":
                    self._cancel_rain_manual_override()

    def set_mode(self, mode: str):
        """Manually pin the irrigation mode; GPS won't override until unlocked."""
        if mode not in ("auto", "manual"):
            return
        with self._state_lock:
            self._mode = mode
            self._mode_locked = True
            if mode != "manual":
                self._cancel_rain_manual_override()

    def unlock_mode(self):
        """Release the manual pin so GPS distance decides mode again."""
        with self._state_lock:
            self._mode_locked = False

    def set_home(self, lat: float, lon: float):
        with self._state_lock:
            self._home_lat = lat
            self._home_lon = lon
            if self._user_lat is not None and self._user_lon is not None:
                self._user_distance_miles = haversine_miles(
                    self._user_lat, self._user_lon, self._home_lat, self._home_lon
                )
                if not self._mode_locked:
                    self._mode = "auto" if self._user_distance_miles > GEOFENCE_RADIUS_MILES else "manual"
                    if self._mode == "auto":
                        self._cancel_rain_manual_override()

    # ──────────────── Manual controls ────────────────

    def toggle_zone(self, zone: str, on: bool) -> dict:
        with self._state_lock:
            if zone not in self.zones:
                return {"ok": False, "error": f"Zone '{zone}' not found"}
            self._manual_overrides[zone] = on
            self._zone_states[zone] = on
            if on and self._mode == "manual" and self._is_raining:
                self._start_rain_manual_override()
            elif on or not any(self._zone_states.values()):
                self._cancel_rain_manual_override()
            return {"ok": True, "zone": zone, "state": on}

    def toggle_all_zones(self, on: bool):
        with self._state_lock:
            for z in self.zones:
                self._zone_states[z] = on
                self._manual_overrides[z] = on
            if on and self._mode == "manual" and self._is_raining:
                self._start_rain_manual_override()
            else:
                self._cancel_rain_manual_override()

    def _cancel_rain_manual_override(self):
        with self._state_lock:
            self._rain_manual_generation += 1
            if self._rain_manual_timer is not None:
                self._rain_manual_timer.cancel()
            self._rain_manual_timer = None
            self._rain_manual_until = None

    def _start_rain_manual_override(self):
        with self._state_lock:
            self._cancel_rain_manual_override()
            generation = self._rain_manual_generation
            self._rain_manual_until = time.monotonic() + RAIN_MANUAL_RUN_SECS
            timer = threading.Timer(
                RAIN_MANUAL_RUN_SECS,
                self._finish_rain_manual_override,
                args=(generation,),
            )
            timer.daemon = True
            self._rain_manual_timer = timer
            timer.start()

    def _finish_rain_manual_override(self, generation: int):
        with self._state_lock:
            if generation != self._rain_manual_generation:
                return
            for zone in self.zones:
                self._zone_states[zone] = False
                self._manual_overrides[zone] = False
            self._rain_manual_timer = None
            self._rain_manual_until = None
            self._rain_manual_generation += 1

    # ──────────────── Decision engine ────────────────

    def evaluate(self, weather: dict, sensor_snapshot: dict) -> IrrigationDecision:
        """Serialize decisions with manual controls and the rain-stop timer."""
        with self._state_lock:
            return self._evaluate_locked(weather, sensor_snapshot)

    def _evaluate_locked(self, weather: dict, sensor_snapshot: dict) -> IrrigationDecision:
        """Main decision function — called every tick. Only raises the 3 alert types."""
        if weather.get("available") is not False:
            self._is_raining = bool(weather.get("is_rainy", False))

        zones_snapshot = sensor_snapshot.get("zones", {})
        tank           = sensor_snapshot.get("tank", {})
        tank_level     = tank.get("level", 100)

        # ── 1. TANK ALERTS ──────────────────────────────────────────────
        if tank_level < TANK_CRITICAL_PCT:
            self._alert(
                f"🚨 Water tank critically low ({tank_level:.0f}%) — irrigation suspended!",
                "tank_critical", "danger"
            )
            self._set_all_zones(False)
            return IrrigationDecision(
                should_irrigate=False, zones_to_irrigate=[],
                reason="Tank critically low", mode=self._mode,
                priority="none", skip_reason="Tank empty",
                notifications=self._flush_notifications(),
            )

        if tank_level < TANK_LOW_PCT:
            self._alert(
                f"⚠️ Water tank is low ({tank_level:.0f}%) — please refill soon.",
                "tank_low", "warning"
            )

        if weather.get("available") is False:
            # Do not make an automatic watering decision using placeholder data.
            if self._mode == "auto":
                self._set_all_zones(False)
            return IrrigationDecision(
                should_irrigate=False, zones_to_irrigate=[],
                reason="Weather data unavailable", mode=self._mode,
                priority="none", skip_reason="Waiting for real weather data",
                notifications=self._flush_notifications(),
            )

        if self._rain_manual_until is not None and time.monotonic() >= self._rain_manual_until:
            self._set_all_zones(False)

        # ── 2. WEATHER CHANGE ALERT ─────────────────────────────────────
        self._check_weather_change(weather)
        self._prev_weather = weather

        # ── 3. SOIL MOISTURE ALERTS (per zone) ──────────────────────────
        for zone, zdata in zones_snapshot.items():
            moisture = zdata.get("moisture", 50)
            if moisture < SOIL_ALERT_LOW:
                self._alert(
                    f"🌵 **{zone}** soil is critically dry ({moisture}%) — needs water!",
                    f"soil_dry_{zone}", "danger"
                )
            elif moisture > SOIL_ALERT_HIGH:
                self._alert(
                    f"💦 **{zone}** soil is waterlogged ({moisture}%) — stop irrigation!",
                    f"soil_wet_{zone}", "warning"
                )

        # ── Irrigation logic (silent — no extra alerts) ──────────────────
        temp     = weather.get("temperature", 20)
        wind_ms  = weather.get("wind_speed", 0)
        is_rainy = weather.get("is_rainy", False)
        rain_soon= weather.get("rain_next_24h", False)
        uvi      = weather.get("uvi", 0)
        hour     = datetime.now(timezone.utc).replace(tzinfo=None).hour

        skip_wind = wind_ms  > HIGH_WIND_MS
        skip_heat = temp     > HOT_TEMP_C and 10 <= hour <= 16

        rain_manual_run_active = (
            self._mode == "manual"
            and self._rain_manual_until is not None
            and time.monotonic() < self._rain_manual_until
            and bool(self.active_zones)
        )
        if is_rainy and not rain_manual_run_active:
            self._set_all_zones(False)
            return IrrigationDecision(
                should_irrigate=False, zones_to_irrigate=[],
                reason="Currently raining", mode=self._mode,
                priority="none", skip_reason="Rain detected",
                notifications=self._flush_notifications(),
            )

        if self._mode == "manual" and self._rain_manual_until is not None:
            active_zones = self.active_zones
            remaining = self._rain_manual_until - time.monotonic()
            if active_zones and remaining > 0:
                return IrrigationDecision(
                    should_irrigate=True, zones_to_irrigate=active_zones,
                    reason="Manual run with rain shutoff timer", mode=self._mode,
                    priority="medium",
                    skip_reason=f"Rain override: auto-stops in {math.ceil(remaining)}s",
                    notifications=self._flush_notifications(),
                )
            self._set_all_zones(False)

        zones_to_water = []
        for zone, zdata in zones_snapshot.items():
            moisture = zdata.get("moisture", 50)
            is_on    = self._zone_states.get(zone, False)

            if self._mode == "manual":
                # Manual: don't touch zone states
                continue

            # Auto mode
            if skip_wind or skip_heat:
                self._zone_states[zone] = False
                continue

            if rain_soon and moisture > 30:
                self._zone_states[zone] = False
                continue

            needs       = moisture < SOIL_DRY_THRESHOLD
            should_stop = moisture >= SOIL_STOP_THRESHOLD

            if needs and tank_level >= TANK_CRITICAL_PCT:
                self._zone_states[zone] = True
                zones_to_water.append(zone)
            elif should_stop and is_on:
                self._zone_states[zone] = False
            elif is_on and zone not in zones_to_water:
                zones_to_water.append(zone)

        any_critical = any(
            zd.get("moisture", 50) < SOIL_ALERT_LOW
            for zd in zones_snapshot.values()
        )
        priority = "high" if any_critical else ("medium" if zones_to_water else "none")

        return IrrigationDecision(
            should_irrigate=len(zones_to_water) > 0,
            zones_to_irrigate=zones_to_water,
            reason=self._build_reason(temp, wind_ms, uvi, is_rainy, rain_soon),
            mode=self._mode,
            priority=priority,
            notifications=self._flush_notifications(),
        )

    # ──────────────── Weather change detection ────────────────

    def _check_weather_change(self, weather: dict):
        if not self._prev_weather:
            return

        prev = self._prev_weather
        alerts = []

        # Condition change (e.g. Clear → Rain, Clouds → Thunderstorm)
        prev_main = prev.get("weather_main", "")
        curr_main = weather.get("weather_main", "")
        if prev_main and curr_main and prev_main != curr_main:
            alerts.append(
                f"🌦️ Weather changed: **{prev_main}** → **{curr_main}** "
                f"({weather.get('weather_desc', '')})"
            )

        # Sudden temperature jump
        prev_temp = prev.get("temperature", 0)
        curr_temp = weather.get("temperature", 0)
        delta = abs(curr_temp - prev_temp)
        if delta >= WEATHER_TEMP_JUMP_C:
            direction = "risen" if curr_temp > prev_temp else "dropped"
            alerts.append(
                f"🌡️ Temperature has {direction} sharply "
                f"({prev_temp}°C → {curr_temp}°C)"
            )

        # Rain incoming (wasn't expected before, now is)
        if not prev.get("rain_next_24h") and weather.get("rain_next_24h"):
            alerts.append("🌧️ Rain expected in the next 24 hours — irrigation may be skipped.")

        for msg in alerts:
            self._alert(msg, f"weather_{hash(msg) % 10000}", "warning")

    # ──────────────── Helpers ────────────────

    def _alert(self, message: str, tag: str, level: str = "info"):
        """Queue an alert only if its cooldown has expired."""
        now = time.time()
        last = self._cooldowns.get(tag, 0)
        if now - last < ALERT_COOLDOWN_SECS:
            return  # still in cooldown — silently skip
        self._cooldowns[tag] = now
        self._notifications.append({
            "message":   message,
            "tag":       tag,
            "level":     level,
            "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        })

    def _flush_notifications(self) -> List[dict]:
        n = self._notifications[:]
        self._notifications.clear()
        return n

    def _set_all_zones(self, state: bool):
        for z in self.zones:
            self._zone_states[z] = state
            if not state:
                self._manual_overrides[z] = False
        if not state:
            self._cancel_rain_manual_override()

    def _build_reason(self, temp, wind_ms, uvi, is_rainy, rain_soon) -> str:
        parts = []
        if is_rainy:             parts.append("raining")
        if rain_soon:            parts.append("rain expected in 24 h")
        if wind_ms > HIGH_WIND_MS: parts.append(f"wind {wind_ms:.1f} m/s")
        if temp > HOT_TEMP_C:    parts.append(f"heat {temp}°C")
        if uvi > 6:              parts.append(f"UV {uvi}")
        return "; ".join(parts) if parts else "Normal conditions"

    # ──────────────── Properties ────────────────

    @property
    def zone_states(self) -> Dict[str, bool]:
        with self._state_lock:
            return dict(self._zone_states)

    @property
    def active_zones(self) -> List[str]:
        with self._state_lock:
            return [z for z, on in self._zone_states.items() if on]

    @property
    def mode(self) -> str:
        with self._state_lock:
            return self._mode

    @property
    def user_distance_miles(self) -> Optional[float]:
        with self._state_lock:
            return self._user_distance_miles

    @property
    def home_location(self) -> Tuple[Optional[float], Optional[float]]:
        with self._state_lock:
            return (self._home_lat, self._home_lon)

    def get_status(self) -> dict:
        with self._state_lock:
            return {
                "mode":                  self._mode,
                "mode_locked":           self._mode_locked,
                "zone_states":           self.zone_states,
                "active_zones":          self.active_zones,
                "user_distance_miles":   self._user_distance_miles,
                "geofence_radius_miles": GEOFENCE_RADIUS_MILES,
                "home_lat":              self._home_lat,
                "home_lon":              self._home_lon,
            }


# ────────────────────────────────────────────────────
#  Utility
# ────────────────────────────────────────────────────

def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles."""
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi    = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
