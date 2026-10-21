"""
Mock sensor module for Smart Irrigation System.
Simulates soil moisture and water tank level sensors.
"""

import random
import time
import math
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

    def __init__(self, initial_level: float = None):
        self._level = initial_level if initial_level is not None else random.uniform(50, 90)
        self._last_update = time.time()
        self._active_zones = 0
        # litres per minute per active zone
        self._consumption_rate = 2.5

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

    def refill(self, amount_pct: float = 100.0):
        """Manually refill the tank."""
        self._level = min(100.0, self._level + amount_pct)

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
