"""
Mock sensor module for Smart Irrigation System.
Simulates soil moisture and water tank level sensors.
"""

import random
import time
import math
import os
from datetime import datetime, timezone


class SoilMoistureSensor:
    """
    Mock soil moisture sensor. Returns percentage 0-100.
    0   = bone dry
    100 = waterlogged
    Ideal range: 40-70%
    """

    def __init__(self, zone_id: str, initial_moisture: float = None):
        self.zone_id = zone_id
        self._moisture = initial_moisture if initial_moisture is not None else random.uniform(30, 70)
        self._last_update = time.time()
        self._is_irrigating = False
        self._drain_rate = random.uniform(0.05, 0.15)   # % per second when dry/hot
        self._absorb_rate = random.uniform(0.8, 1.5)    # % per second when irrigating

    def update(self, temperature: float = 25.0, is_irrigating: bool = False):
        """Simulate moisture change over time based on conditions."""
        now = time.time()
        elapsed = now - self._last_update
        self._last_update = now
        self._is_irrigating = is_irrigating

        # Evaporation increases with temperature
        evap_factor = 1.0 + max(0, (temperature - 20) / 20)
        drain = self._drain_rate * evap_factor * elapsed

        if is_irrigating:
            self._moisture = min(95.0, self._moisture + self._absorb_rate * elapsed)
        else:
            self._moisture = max(5.0, self._moisture - drain)

        # Add small random noise to simulate real sensor jitter
        noise = random.uniform(-0.3, 0.3)
        self._moisture = max(5.0, min(95.0, self._moisture + noise))

    @property
    def moisture(self) -> float:
        return round(self._moisture, 1)

    @property
    def status(self) -> str:
        if self._moisture < 25:
            return "Critical — Dry"
        elif self._moisture < 40:
            return "Low"
        elif self._moisture < 70:
            return "Optimal"
        elif self._moisture < 85:
            return "Moist"
        else:
            return "Waterlogged"

    @property
    def needs_water(self) -> bool:
        return self._moisture < 40

    def to_dict(self) -> dict:
        return {
            "zone_id": self.zone_id,
            "moisture": self.moisture,
            "status": self.status,
            "needs_water": self.needs_water,
            "is_irrigating": self._is_irrigating,
        }


class WaterTankSensor:
    """
    Mock water tank level sensor. Returns percentage 0-100.
    Simulates a 1000-litre tank.
    """
    CAPACITY_LITRES = 1000
    AUTO_REFILL_THRESHOLD_PCT = 20.0
    UK_AVERAGE_EFFECTIVE_RATE_GBP_PER_M3 = 5.42

    def __init__(self, initial_level: float = None):
        self._level = initial_level if initial_level is not None else random.uniform(50, 90)
        self._last_update = time.time()
        self._active_zones = 0
        # litres per minute per active zone
        self._consumption_rate = 2.5
        self._catchment_area_m2 = self._env_float("RAINWATER_CATCHMENT_AREA_M2", 20.0, minimum=1.0, maximum=1000.0)
        self._collection_efficiency = self._env_float("RAINWATER_COLLECTION_EFFICIENCY", 0.8, minimum=0.1, maximum=1.0)
        configured_rate = self._env_optional_float("WATER_TARIFF_GBP_PER_M3")
        self._water_tariff_gbp_per_m3 = (
            configured_rate if configured_rate is not None else self.UK_AVERAGE_EFFECTIVE_RATE_GBP_PER_M3
        )
        default_supplier = (
            "UK estimate (England & Wales) 2026/27"
            if configured_rate is None
            else "Configured meter rate"
        )
        configured_supplier = os.getenv("WATER_SUPPLIER", "").strip()
        self._water_supplier = (
            configured_supplier if configured_rate is not None and configured_supplier
            else default_supplier
        )[:80]
        self._last_fill_source = "Initial simulated level"
        self._last_fill_litres = 0.0
        self._last_fill_cost_gbp = None
        self._rainwater_collected_litres = 0.0
        self._mains_cost_gbp_total = 0.0
        self._rain_status = None

    @staticmethod
    def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
        try:
            value = float(os.getenv(name, default))
        except (TypeError, ValueError):
            return default
        return min(maximum, max(minimum, value)) if math.isfinite(value) else default

    @staticmethod
    def _env_optional_float(name: str):
        try:
            value = float(os.getenv(name, ""))
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value >= 0 else None

    def configure(self, settings: dict):
        """Set the rain catchment and metered water tariff for this tank."""
        if not isinstance(settings, dict):
            return
        try:
            area = float(settings.get("catchment_area_m2", self._catchment_area_m2))
            efficiency = float(settings.get("collection_efficiency", self._collection_efficiency))
        except (TypeError, ValueError):
            return
        if not (math.isfinite(area) and 1 <= area <= 1000 and math.isfinite(efficiency) and 0.1 <= efficiency <= 1):
            return
        self._catchment_area_m2 = area
        self._collection_efficiency = efficiency
        self._water_supplier = str(settings.get("water_supplier", "")).strip()[:80]
        raw_rate = settings.get("water_tariff_gbp_per_m3")
        if raw_rate is None or raw_rate == "":
            self._water_tariff_gbp_per_m3 = None
        else:
            try:
                rate = float(raw_rate)
            except (TypeError, ValueError):
                return
            if not math.isfinite(rate) or not 0 <= rate <= 100:
                return
            self._water_tariff_gbp_per_m3 = rate

    def get_settings(self) -> dict:
        return {
            "catchment_area_m2": self._catchment_area_m2,
            "collection_efficiency": self._collection_efficiency,
            "water_supplier": self._water_supplier,
            "water_tariff_gbp_per_m3": self._water_tariff_gbp_per_m3,
        }

    def update(self, active_zones: int = 0):
        """Simulate water consumption when irrigation is active."""
        now = time.time()
        elapsed = now - self._last_update
        self._last_update = now
        self._active_zones = active_zones

        if active_zones > 0:
            # Convert: rate(L/min) * zones * elapsed(s) / 60 / capacity(L) * 100 → %
            consumed_pct = (self._consumption_rate * active_zones * elapsed / 60) / self.CAPACITY_LITRES * 100
            self._level = max(0.0, self._level - consumed_pct)

        # Tiny random noise
        noise = random.uniform(-0.05, 0.05)
        self._level = max(0.0, min(100.0, self._level + noise))

    def collect_rainwater(self, rainfall_mm: float) -> float:
        """Add estimated roof runoff; 1 mm falling on 1 m² is approximately 1 litre."""
        try:
            rainfall_mm = float(rainfall_mm)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(rainfall_mm) or rainfall_mm <= 0:
            return 0.0
        headroom_litres = max(0.0, self.CAPACITY_LITRES - self.litres)
        captured_litres = min(
            headroom_litres,
            rainfall_mm * self._catchment_area_m2 * self._collection_efficiency,
        )
        if captured_litres <= 0:
            return 0.0
        self._level = min(100.0, self._level + captured_litres / self.CAPACITY_LITRES * 100)
        self._last_fill_source = "Rainwater"
        self._last_fill_litres = round(captured_litres, 1)
        self._last_fill_cost_gbp = 0.0
        self._rainwater_collected_litres += captured_litres
        return round(captured_litres, 1)

    def _fill_from_mains(self, target_level_pct: float):
        target = min(100.0, max(self._level, target_level_pct))
        added_litres = (target - self._level) / 100 * self.CAPACITY_LITRES
        if added_litres <= 0:
            return
        self._level = target
        self._last_fill_source = "Mains water"
        self._last_fill_litres = round(added_litres, 1)
        self._last_fill_cost_gbp = (
            round(added_litres / 1000 * self._water_tariff_gbp_per_m3, 4)
            if self._water_tariff_gbp_per_m3 is not None else None
        )
        if self._last_fill_cost_gbp is not None:
            self._mains_cost_gbp_total += self._last_fill_cost_gbp

    def refill_from_mains(self, amount_pct: float = 100.0):
        """Manually top up the tank from the mains and estimate its volumetric cost."""
        self._fill_from_mains(min(100.0, self._level + max(0.0, amount_pct)))

    def auto_refill_if_low(self, is_raining=None) -> bool:
        """Top up with mains only when the tank is low and current weather confirms no rain."""
        self._rain_status = is_raining
        if self._level <= self.AUTO_REFILL_THRESHOLD_PCT and is_raining is False:
            self._fill_from_mains(100.0)
            return True
        return False

    @property
    def level(self) -> float:
        return round(self._level, 1)

    @property
    def litres(self) -> float:
        return round(self._level / 100 * self.CAPACITY_LITRES, 1)

    @property
    def status(self) -> str:
        if self._level < 10:
            return "Critical — Empty"
        elif self._level < 25:
            return "Low"
        elif self._level < 60:
            return "Moderate"
        else:
            return "Full"

    @property
    def is_sufficient(self) -> bool:
        return self._level >= 15

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "litres": self.litres,
            "capacity": self.CAPACITY_LITRES,
            "status": self.status,
            "is_sufficient": self.is_sufficient,
            "active_zones": self._active_zones,
            "last_fill_source": self._last_fill_source,
            "last_fill_litres": self._last_fill_litres,
            "last_fill_cost_gbp": self._last_fill_cost_gbp,
            "mains_cost_gbp_total": round(self._mains_cost_gbp_total, 2),
            "rainwater_collected_litres": round(self._rainwater_collected_litres, 1),
            "catchment_area_m2": self._catchment_area_m2,
            "collection_efficiency": self._collection_efficiency,
            "water_supplier": self._water_supplier,
            "water_tariff_gbp_per_m3": self._water_tariff_gbp_per_m3,
            "is_raining": self._rain_status,
            "awaiting_rain": self._level <= self.AUTO_REFILL_THRESHOLD_PCT and self._rain_status is True,
        }


class SensorHub:
    """Aggregates all sensors for the garden."""

    ZONES = ["Front Garden", "Back Garden", "Side Bed", "Greenhouse"]

    def __init__(self):
        self.moisture_sensors = {
            zone: SoilMoistureSensor(zone_id=zone, initial_moisture=random.uniform(25, 75))
            for zone in self.ZONES
        }
        self.tank = WaterTankSensor()

    def set_zones(self, zone_names):
        """Match the simulated soil sensors to the active garden's zones.
        Sensors for zones that still exist keep their current reading."""
        self.moisture_sensors = {
            zone: self.moisture_sensors.get(zone)
            or SoilMoistureSensor(zone_id=zone, initial_moisture=random.uniform(25, 75))
            for zone in zone_names
        }

    def update_all(self, temperature: float = 25.0, active_zones: list = None):
        active_zones = active_zones or []
        for zone, sensor in self.moisture_sensors.items():
            sensor.update(temperature=temperature, is_irrigating=(zone in active_zones))
        active_count = len(active_zones)
        self.tank.update(active_zones=active_count)

    def get_snapshot(self) -> dict:
        return {
            "zones": {zone: s.to_dict() for zone, s in self.moisture_sensors.items()},
            "tank": self.tank.to_dict(),
            "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        }
